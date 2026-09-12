"""WhatsApp Cloud API client for Kaya.

Handles:
- Sending text, button, and list messages
- Downloading media (images, documents)
- Verifying webhook signatures (Meta's X-Hub-Signature-256)
"""

from __future__ import annotations

import hashlib
import hmac
import httpx
from typing import Any

from src.config import settings


class WhatsAppClient:
    """Thin wrapper around WhatsApp Cloud API (Meta)."""

    BASE_URL = "https://graph.facebook.com/v22.0"

    def __init__(self) -> None:
        self.access_token = settings.whatsapp_access_token
        self.phone_number_id = settings.whatsapp_phone_number_id
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
        )

    async def send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send any pre-built WhatsApp API payload.

        The payload should already have 'messaging_product', 'to', 'type', etc.
        """
        url = f"{self.BASE_URL}/{self.phone_number_id}/messages"
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()

    async def send_text(
        self,
        to_phone_number: str,
        body: str,
        *,
        preview_url: bool | None = None,
    ) -> dict[str, Any]:
        """Send a text message via WhatsApp Cloud API.

        Links are only rendered as tappable (and previewed) when preview_url
        is true, so auto-enable it whenever the body contains a URL.
        """
        if preview_url is None:
            preview_url = "http://" in body or "https://" in body
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone_number,
            "type": "text",
            "text": {"body": body, "preview_url": preview_url},
        }
        return await self.send_message(payload)

    async def send_image_by_url(
        self,
        to_phone_number: str,
        image_url: str,
        caption: str = "",
    ) -> dict[str, Any]:
        """Send an image fetched by Meta from a public URL (e.g. our /media
        endpoint), so the recipient sees the actual picture in the chat."""
        image: dict[str, Any] = {"link": image_url}
        if caption:
            image["caption"] = caption[:1024]
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone_number,
            "type": "image",
            "image": image,
        }
        return await self.send_message(payload)

    async def send_document_by_url(
        self,
        to_phone_number: str,
        document_url: str,
        filename: str = "document.pdf",
        caption: str = "",
    ) -> dict[str, Any]:
        """Send a document (PDF etc.) fetched by Meta from a public URL."""
        doc: dict[str, Any] = {"link": document_url, "filename": filename}
        if caption:
            doc["caption"] = caption[:1024]
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone_number,
            "type": "document",
            "document": doc,
        }
        return await self.send_message(payload)

    async def get_media_url(self, media_id: str) -> dict[str, Any]:
        """Get the download URL for a media item (image, document, etc.)."""
        url = f"{self.BASE_URL}/{media_id}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.json()

    async def download_media(self, media_url: str) -> bytes:
        """Download the raw bytes of a media item from its URL."""
        resp = await self._client.get(media_url)
        resp.raise_for_status()
        return resp.content

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def verify_webhook_signature(
        payload: bytes,
        signature_header: str,
        app_secret: str,
    ) -> bool:
        """Verify Meta's X-Hub-Signature-256 header."""
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected_sig = signature_header[7:]
        computed = hmac.new(
            app_secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed, expected_sig)


# Global instance (FastAPI dependency)
whatsapp_client = WhatsAppClient()
