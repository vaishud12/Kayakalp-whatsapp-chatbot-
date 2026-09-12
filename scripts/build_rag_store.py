#!/usr/bin/env python3
"""
Build the RAG vector store from the FAQ xlsx.

Step A: convert xlsx -> JSON (canonical, human-readable).
Step B: embed each FAQ row with Gemini (gemini-embedding-001, 3072-dim).
Step C: store vectors in ChromaDB (local, persistent, free).

Run:  python scripts/build_rag_store.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import chromadb
import openpyxl
from google import genai

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import settings

STORE_DIR = PROJECT_ROOT / "src" / "rag_store"
FAQ_JSON = PROJECT_ROOT / "src" / "faq_data.json"
EMBED_MODEL = "gemini-embedding-001"
COLLECTION_NAME = "kaya_faq"
BATCH_SIZE = 50


def _embed_batch(client: genai.Client, texts: list[str]) -> list[list[float]]:
    """Batch-embed texts with Gemini. Retries on rate-limit (429) with backoff."""
    for attempt in range(5):
        try:
            result = client.models.embed_content(model=EMBED_MODEL, contents=texts)
            return [e.values for e in result.embeddings]
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait = min(15.0 * (2 ** attempt), 120.0)
                print(f"[RAG] Rate-limited, waiting {wait:.1f}s (attempt {attempt + 1}/5)")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"Gemini embedding failed after 5 retries for {len(texts)} texts")


def build() -> None:
    xlsx_path = settings.faq_xlsx_path
    if not os.path.exists(xlsx_path):
        raise SystemExit(f"FAQ xlsx not found: {xlsx_path}")

    # --- Step A: xlsx -> JSON ---
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = [
        str(h).strip().lower().replace(" ", "_").replace("?", "").replace("'", "")
        if h else f"col{i}"
        for i, h in enumerate(next(rows_iter))
    ]
    records = []
    for raw_row in rows_iter:
        row = {}
        for i, val in enumerate(raw_row):
            if i < len(headers):
                row[headers[i]] = str(val).strip() if val is not None else ""
        q = row.get("question", "")
        a = row.get("answer", "")
        if q or a:
            if "id" in row and "external_id" not in row:
                row["external_id"] = row["id"]
            records.append(row)
    wb.close()

    # Merge with any manually-added rows in the JSON file (so edits to
    # faq_data.json survive rebuilds). Deduplicate by external_id, with
    # xlsx rows winning on conflict. Already-merged entries are not
    # added again, so re-running is safe.
    if FAQ_JSON.exists():
        try:
            with FAQ_JSON.open("r", encoding="utf-8") as f:
                existing = json.load(f)
            xlsx_ids = {
                str(r.get("external_id") or r.get("id") or "") for r in records
            }
            merged_ids = set(xlsx_ids)
            merged = list(records)
            for r in existing:
                eid = str(r.get("external_id") or r.get("id") or "")
                if eid and eid not in merged_ids:
                    merged.append(r)
                    merged_ids.add(eid)
            if len(merged) > len(records):
                print(
                    f"[RAG] Merged {len(merged) - len(records)} manually-added "
                    f"rows from {FAQ_JSON.name}"
                )
            records = merged
        except Exception as e:
            print(f"[RAG] WARNING: could not merge {FAQ_JSON.name}: {e}")

    FAQ_JSON.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[RAG] Wrote {len(records)} FAQ rows -> {FAQ_JSON}")

    # --- Step B + C: embed and store in ChromaDB ---
    api_key = settings.google_api_key
    if not api_key:
        raise SystemExit("GOOGLE_API_KEY not set in .env")

    client = genai.Client(api_key=api_key)

    chroma_client = chromadb.PersistentClient(path=str(STORE_DIR))
    # Wipe existing collection for idempotent rebuild
    try:
        chroma_client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = chroma_client.create_collection(COLLECTION_NAME, metadata={"hnsw:space": "cosine"})

    # Build embedding texts: question + answer + category.
    # Embedding Q+A together ensures the semantic search captures what the answer
    # actually covers — e.g. "Do you treat acne?" embeds with its session-answer
    # content, while "Which plan should I choose?" embeds with its plan-options
    # content. This way "acne treatment plans" maps to the plans answer, not the
    # sessions answer.
    embed_texts = [
        f"{r.get('question', '')} {r.get('answer', '')} {r.get('category', '')}"
        for r in records
    ]

    ids = []
    metadatas = []
    documents = []
    embeddings = []

    for i in range(0, len(records), BATCH_SIZE):
        batch_texts = embed_texts[i:i + BATCH_SIZE]
        vecs = _embed_batch(client, batch_texts)

        for j, vec in enumerate(vecs):
            idx = i + j
            r = records[idx]
            ids.append(str(r.get("id") or r.get("external_id") or idx))
            metadatas.append({
                "category": str(r.get("category", "")),
                "question": str(r.get("question", "")),
                "answer": str(r.get("answer", "")),
                "external_id": str(r.get("external_id", "")),
            })
            documents.append(embed_texts[idx])
            embeddings.append(vec)

        print(f"[RAG] embedded {min(i + BATCH_SIZE, len(records))}/{len(records)}")

    collection.add(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents)
    print(f"[RAG] ChromaDB collection '{COLLECTION_NAME}' built: {collection.count()} docs at {STORE_DIR}")


if __name__ == "__main__":
    build()