"""Image / Document analysis using Google's Gemini 3.1 Flash Lite directly.

All image and PDF analysis goes straight to the Google Gemini API using the
GOOGLE_API_KEY configured in .env. It uses Gemini 3.1 Flash Lite
(gemini-3.1-flash-lite) — Google's low-latency, low-cost multimodal model
that accepts text, image, and PDF inputs — via the google-genai SDK.

Handles the 'image' and 'document' routes from the WhatsApp webhook:
  1. Receive media ID from WhatsApp
  2. Get the media download URL
  3. Download the binary content
  4. Send to Google Gemini 3.1 Flash Lite for analysis
  5. Return the analysis text
"""

from __future__ import annotations

import asyncio
import logging

from google import genai
from google.genai import types

from src.config import settings


_LOG = logging.getLogger("kaya.image_analyzer")

# Google Gemini model used for image/PDF analysis (direct Google API).
IMAGE_MODEL = "gemini-3.1-flash-lite"

SYSTEM_PROMPT = (
    "You are a warm, professional medical administrative assistant for "
    "Kayakalp Clinic (Dr. Lekha Jadhav), Pune, India. A patient has shared "
    "a document or image.\n\n"
    "RULES:\n"
    "1. Never give a medical diagnosis. Use cautious language ('may', 'can').\n"
    "2. Summarize key findings in simple, patient-friendly language.\n"
    "3. If the content is not health-related, politely say you can only help "
    "with health and Kayakalp topics.\n"
    "4. End every response with this exact disclaimer:\n"
    '"Please consult a qualified doctor at Kayakalp for your personal medical decisions."\n'
    "5. Keep responses concise (2-4 short paragraphs) for WhatsApp. No markdown."
)


class ImageAnalyzer:
    """Google Gemini 3.1 Flash Lite analysis for images and documents."""

    def __init__(self) -> None:
        self._client: genai.Client | None = None

    def _get_client(self) -> genai.Client:
        if self._client is None:
            if not settings.google_api_key:
                raise RuntimeError("GOOGLE_API_KEY not configured")
            self._client = genai.Client(
                api_key=settings.google_api_key,
                http_options=types.HttpOptions(timeout=120_000),
            )
        return self._client

    async def _generate_with_retry(
        self,
        *,
        prompt: str,
        payload: bytes,
        mime_type: str,
    ) -> str:
        """Call Google Gemini 3.1 Flash Lite with exponential-backoff retry."""
        client = self._get_client()
        last_exc: Exception | None = None

        for attempt in range(3):
            try:
                response = await client.aio.models.generate_content(
                    model=IMAGE_MODEL,
                    contents=[
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_text(text=prompt),
                                types.Part.from_bytes(data=payload, mime_type=mime_type),
                            ],
                        )
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=0.2,
                        max_output_tokens=1000,
                    ),
                )
                text = (response.text or "").strip()
                if text:
                    return text
                last_exc = RuntimeError("empty response from Gemini")
            except Exception as e:
                last_exc = e
                _LOG.warning(
                    f"Gemini image analysis failed attempt {attempt + 1}/3: "
                    f"{type(e).__name__}: {str(e)[:120]}"
                )
                await asyncio.sleep(min(2.0 * (2 ** attempt), 10.0))

        _LOG.error(f"Gemini image analysis failed after retries: {last_exc}")
        return _fallback_error()

    async def analyze_image(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
        """Analyze a medical image (report, skin photo, prescription)."""
        prompt = (
            "This image was shared by a patient and may be a medical report, "
            "a prescription, a lab result, or a skin/health photo. "
            "Analyze it and give a concise, patient-friendly summary."
        )
        return await self._generate_with_retry(
            prompt=prompt,
            payload=image_bytes,
            mime_type=mime_type,
        )

    async def analyze_document(self, doc_bytes: bytes, mime_type: str = "application/pdf") -> str:
        """Analyze a medical document (lab result, prescription, discharge summary)."""
        prompt = (
            "This document was shared by a patient and may be a medical report, "
            "a lab result, a prescription, or a discharge summary. "
            "Analyze it and give a concise, patient-friendly summary."
        )
        return await self._generate_with_retry(
            prompt=prompt,
            payload=doc_bytes,
            mime_type=mime_type,
        )

    async def close(self) -> None:
        """Close the underlying Google client."""
        if self._client is not None:
            self._client.close()
            self._client = None


def _fallback_error() -> str:
    return (
        "I'm sorry, I'm unable to analyze this file right now. "
        "Please reach out to the Kayakalp team for assistance — they'll be happy to help you!"
    )


# Global instance
image_analyzer = ImageAnalyzer()