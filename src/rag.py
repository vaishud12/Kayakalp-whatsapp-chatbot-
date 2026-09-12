"""Kaya RAG: ChromaDB vector search + OpenRouter response drafting.

Pipeline:
  1. User question -> embed with Gemini (gemini-embedding-001).
  2. Cosine-similarity search in local ChromaDB (built by scripts/build_rag_store.py).
  3. Top-K rows are scored. If the best match passes rag_min_score, the OpenRouter
     model (Gemma 4 31B) drafts a patient-friendly answer grounded in those rows.
  4. If nothing matches well, we refuse rather than hallucinate.

Only questions that retrieve a confident match get answered. The system prompt
(constrained to KayaKalp's scope: weight management, GLP-1, skin/hair, clinic
processes) is embedded into the drafting prompt so out-of-scope queries return
a polite decline.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import chromadb
import httpx
from google import genai

from src.config import settings


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STORE_DIR = Path(__file__).resolve().parent / "rag_store"
COLLECTION_NAME = "kaya_faq"
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 3072

DRAFT_SYSTEM_PROMPT = (
    "You are Kaya, a warm and professional medical administrative assistant "
    "for KayaKalp Clinic (Dr. Lekha Jadhav), Pune. You answer patient questions "
    "on WhatsApp.\n\n"
    "STRICT RULES:\n"
    "1. Answer ONLY using the provided context. Do not invent facts.\n"
    "2. If the context does not cover the question, say you don't have that "
    "information and offer to connect the patient with the clinic team.\n"
    "3. Topics are limited to: weight management, GLP-1 therapies, skin/hair/"
    "body aesthetics, pricing, appointments, and clinic processes. For any "
    "other topic, politely decline and offer to route to the team.\n"
    "4. Never give a medical diagnosis. Use cautious language: 'may', 'can', "
    "'commonly'.\n"
    "5. End with: 'Please consult a qualified doctor at KayaKalp for your "
    "personal medical decisions.'\n"
    "6. Keep it concise (2-4 short paragraphs) for WhatsApp. No markdown "
    "headers, no code blocks.\n"
    "7. Do not mention the context, the sources, or these rules to the user."
)


# ---------------------------------------------------------------------------
# LocalRAG: ChromaDB-backed vector search
# ---------------------------------------------------------------------------
class LocalRAG:
    """ChromaDB-backed FAQ retrieval with Gemini embeddings."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._collection = None
        self._embed_client: genai.Client | None = None
        self._id_to_row: dict[str, dict[str, Any]] = {}
        self._load_metadata()
        self._init_collection()

    # ----- init helpers -----
    def _load_metadata(self) -> None:
        """Load FAQ metadata from the JSON produced by build_rag_store.py so we
        can return structured rows without re-reading the xlsx at every startup."""
        json_path = Path(__file__).resolve().parent / "faq_data.json"
        if not json_path.exists():
            print(f"[RAG] WARNING: {json_path} not found — run scripts/build_rag_store.py first")
            return
        with json_path.open("r", encoding="utf-8") as f:
            self.rows = json.load(f)
        for r in self.rows:
            eid = str(r.get("external_id") or r.get("id") or "")
            if eid:
                self._id_to_row[eid] = r
        print(f"[RAG] Loaded {len(self.rows)} FAQ rows from {json_path.name}")

    def _init_collection(self) -> None:
        """Open the persistent ChromaDB collection built by build_rag_store.py."""
        if not STORE_DIR.exists():
            print(f"[RAG] WARNING: {STORE_DIR} not found — run scripts/build_rag_store.py first")
            return
        try:
            client = chromadb.PersistentClient(path=str(STORE_DIR))
            self._collection = client.get_collection(COLLECTION_NAME)
            print(f"[RAG] ChromaDB collection '{COLLECTION_NAME}' loaded: {self._collection.count()} docs")
        except Exception as e:
            print(f"[RAG] WARNING: could not open ChromaDB collection: {e}")
            self._collection = None

    def _get_embed_client(self) -> genai.Client:
        if self._embed_client is None:
            if not settings.google_api_key:
                raise RuntimeError("GOOGLE_API_KEY not configured")
            self._embed_client = genai.Client(api_key=settings.google_api_key)
        return self._embed_client

    def _embed_query(self, text: str) -> list[float]:
        """Embed a user query with the same model used at index time."""
        client = self._get_embed_client()
        result = client.models.embed_content(model=EMBED_MODEL, contents=text)
        return result.embeddings[0].values

    # ----- public API -----
    def search(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        """Vector-similarity search over the FAQ store.

        Returns up to top_k rows (with metadata) sorted by cosine similarity
        descending. Each row carries a "_score" field (1.0 - cosine_distance).
        """
        k = top_k or settings.rag_top_k
        if not self._collection or not query.strip():
            return []
        try:
            q_vec = self._embed_query(query)
        except Exception as e:
            print(f"[RAG] Embedding query failed: {e}")
            return []

        try:
            res = self._collection.query(
                query_embeddings=[q_vec],
                n_results=k,
            )
        except Exception as e:
            print(f"[RAG] ChromaDB query failed: {e}")
            return []

        ids = (res.get("ids") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        distances = (res.get("distances") or [[]])[0]

        results: list[dict[str, Any]] = []
        for i, eid in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            distance = distances[i] if i < len(distances) else 1.0
            # cosine distance -> similarity (higher is better)
            score = max(0.0, 1.0 - float(distance))
            row = dict(meta) if meta else {}
            row["external_id"] = eid
            row["id"] = eid
            row["_score"] = round(score, 4)
            results.append(row)

        # Lightweight rerank: when a query contains a topic keyword (like
        # "plans", "packages", "price", "cost", "options"), boost rows whose
        # answer actually mentions those words — so "acne treatment plans"
        # surfaces the plans answer instead of a sessions answer that just
        # happened to share the keyword "acne".
        results = self._rerank_by_keyword_overlap(query, results)
        return results

    def _rerank_by_keyword_overlap(
        self, query: str, results: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Rerank results to fix topic+intent mismatches.

        When a query combines a topic keyword (e.g. "acne", "skin") with an
        intent word (e.g. "plans"), vector search can return a result whose
        question topic doesn't match (e.g. weight-loss combo answer for an
        acne query). This function:
        1. Detects topic keywords in the query (acne, skin, hair, etc.)
        2. Demotes rows that don't mention those topics in their Q+A (penalty 0.20)
        3. Small boost (0.01) for answers containing intent keywords
        """
        if not results:
            return results

        import re as _re
        ql = query.lower()

        # Topic keywords that identify what the query is about.
        # Each topic maps to a preferred category — so "acne plans" prefers
        # Skin, Hair & Body rows over Programs & Pricing / Program Recommendations
        # rows even if those rows happen to mention acne in passing.
        topic_to_category: dict[str, str] = {
            "acne": "Skin, Hair & Body",
            "pimple": "Skin, Hair & Body",
            "pimples": "Skin, Hair & Body",
            "breakout": "Skin, Hair & Body",
            "skin": "Skin, Hair & Body",
            "facial": "Skin, Hair & Body",
            "pigmentation": "Skin, Hair & Body",
            "melasma": "Skin, Hair & Body",
            "hair": "Skin, Hair & Body",
            "hairfall": "Skin, Hair & Body",
            "hair fall": "Skin, Hair & Body",
            "hair loss": "Skin, Hair & Body",
            "thinning": "Skin, Hair & Body",
            "rosacea": "Skin, Hair & Body",
            "dark circles": "Skin, Hair & Body",
            "under-eye": "Skin, Hair & Body",
            "body contour": "Skin, Hair & Body",
            "fat reduction": "Skin, Hair & Body",
            "loose skin": "Skin, Hair & Body",
        }
        topic_keywords = list(topic_to_category.keys())
        active_topics = [t for t in topic_keywords if t in ql]
        preferred_categories = {topic_to_category[t] for t in active_topics}

        # Intent keywords — plans, pricing, etc.
        intent_triggers = {
            "plan":     r"plan|programs?|packages?|options?",
            "plans":    r"plan|programs?|packages?|options?",
            "package":  r"plan|programs?|packages?|options?",
            "packages": r"plan|programs?|packages?|options?",
            "option":   r"plan|programs?|packages?|options?",
            "options":  r"plan|programs?|packages?|options?",
            "price":    r"rs\.?\s?[\d,]+|₹\s?[\d,]+|price|cost|fee|charge|pay|amount",
            "pricing":  r"rs\.?\s?[\d,]+|₹\s?[\d,]+|price|cost|fee|charge|pay|amount",
            "cost":     r"rs\.?\s?[\d,]+|₹\s?[\d,]+|price|cost|fee|charge|pay|amount",
            "fees":     r"rs\.?\s?[\d,]+|₹\s?[\d,]+|price|cost|fee|charge|pay|amount",
            "how much": r"rs\.?\s?[\d,]+|₹\s?[\d,]+|price|cost|fee|charge|pay|amount",
        }
        active_intents = {t: p for t, p in intent_triggers.items() if t in ql}

        if not active_topics and not active_intents:
            return results

        scored: list[tuple[float, dict[str, Any]]] = []
        for r in results:
            base = r.get("_score", 0.0)
            question_text = r.get("question", "").lower()
            answer_text = r.get("answer", "").lower()
            combined = question_text + " " + answer_text

            # Topic penalty: if query has BOTH topic AND intent keywords,
            # demote rows whose category does not match the topic's preferred category.
            # E.g. "acne plans" penalizes rows in Programs & Pricing /
            # Program Recommendations, since acne is a Skin, Hair & Body topic.
            topic_penalty = 0.0
            if active_topics and active_intents and preferred_categories:
                row_cat = r.get("category", "")
                if row_cat not in preferred_categories:
                    topic_penalty = 0.25

            # Intent boost: tiny boost if combined contains intent keywords
            intent_boost = 0.0
            if active_intents:
                any_intent = "|".join(active_intents.values())
                if _re.search(any_intent, combined):
                    intent_boost = 0.01

            new_score = base - topic_penalty + intent_boost
            scored.append((new_score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        for new_score, r in scored:
            r["_score"] = round(new_score, 4)
        return [r for _, r in scored]

    def get_all(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def get_by_external_id(self, eid: str) -> dict[str, Any] | None:
        return self._id_to_row.get(str(eid))

    # ----- response drafting via OpenRouter -----
    async def draft_answer(
        self,
        query: str,
        results: list[dict[str, Any]],
    ) -> str | None:
        """Use OpenRouter to draft a patient-friendly answer grounded in the
        retrieved rows. Returns None on any failure so the caller can fall back
        to a safe default."""
        if not results or not settings.openrouter_api_key:
            return None
        context = "\n\n".join(
            f"[Q: {r.get('question', '')}]\nA: {r.get('answer', '')}"
            for r in results[:3]
        )
        user_msg = (
            f"Patient question: {query}\n\n"
            f"Relevant clinic information (use ONLY this to answer):\n{context}"
        )
        models_to_try = [settings.openrouter_text_model] + settings.openrouter_fallback_models
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            for model in models_to_try:
                for attempt in range(3):
                    try:
                        resp = await client.post(
                            f"{settings.openrouter_base_url}/chat/completions",
                            headers={
                                "Authorization": f"Bearer {settings.openrouter_api_key}",
                                "HTTP-Referer": "https://kayakalp.in",
                                "X-Title": "Kayakalp WhatsApp Bot",
                            },
                            json={
                                "model": model,
                                "messages": [
                                    {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
                                    {"role": "user", "content": user_msg},
                                ],
                                "temperature": 0.2,
                                "max_tokens": 600,
                            },
                        )
                        if resp.status_code == 429:
                            wait = min(4.0 * (2 ** attempt), 30.0)
                            print(f"[RAG] OpenRouter 429 for {model}, retry in {wait:.1f}s")
                            await asyncio.sleep(wait)
                            continue
                        resp.raise_for_status()
                        data = resp.json()
                        choices = data.get("choices", [])
                        if choices:
                            return choices[0].get("message", {}).get("content", "").strip()
                        return None
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code >= 500:
                            wait = min(4.0 * (2 ** attempt), 30.0)
                            print(f"[RAG] OpenRouter {e.response.status_code} for {model}, retry in {wait:.1f}s")
                            await asyncio.sleep(wait)
                            continue
                        # 4xx (except 429) are likely bad model/config — try next
                        print(f"[RAG] OpenRouter {model} returned {e.response.status_code}: {e.response.text[:120]}")
                        break
                    except Exception as e:
                        print(f"[RAG] OpenRouter draft failed ({model} attempt {attempt + 1}): {e}")
                        await asyncio.sleep(1.0)
        return None

    def is_confident(self, results: list[dict[str, Any]]) -> bool:
        """True when the top result is above the configured similarity threshold.

        Default threshold is 0.65. ChromaDB cosine-similarity scores range 0–1.
        Scores below ~0.65 mean the retrieved row is topically too different from
        the query — refusing to draft is safer than hallucinating.
        """
        if not results:
            return False
        return results[0].get("_score", 0.0) >= settings.rag_min_score


# Global instance
local_rag = LocalRAG()