"""Cashfree Payment Links client.

Generates a hosted payment link (Cashfree PG Links API) for a patient after
they submit the screening form, and verifies the HMAC signature on incoming
payment webhooks.

Docs: https://www.cashfree.com/docs/payments/online/links
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any

import httpx

from src.config import settings

log = logging.getLogger("kaya.cashfree")

API_VERSION = "2023-08-01"


def _api_base() -> str:
    return (
        "https://api.cashfree.com/pg"
        if settings.cashfree_env == "production"
        else "https://sandbox.cashfree.com/pg"
    )


def _headers() -> dict[str, str]:
    return {
        "x-client-id": settings.cashfree_app_id,
        "x-client-secret": settings.cashfree_secret_key,
        "x-api-version": API_VERSION,
        "Content-Type": "application/json",
    }


async def create_payment_link(
    link_id: str,
    amount: float,
    customer_phone: str,
    customer_name: str = "",
    customer_email: str = "",
    purpose: str = "Screening call payment",
    expiry_iso: str | None = None,
) -> dict[str, Any] | None:
    """Create a Cashfree hosted payment link. Returns {"link_url": ..., "link_id": ...} or None on failure."""
    if not settings.cashfree_enabled:
        log.error("Cashfree is not configured (CASHFREE_APP_ID / CASHFREE_SECRET_KEY missing)")
        return None

    digits = "".join(c for c in customer_phone if c.isdigit())[-10:]
    body: dict[str, Any] = {
        "link_id": link_id,
        "link_amount": round(float(amount), 2),
        "link_currency": "INR",
        "link_purpose": purpose,
        "customer_details": {
            "customer_phone": digits or "9999999999",
            "customer_name": customer_name or "Patient",
        },
        "link_notify": {"send_sms": False, "send_email": False},
        "link_partial_payments": False,
    }
    if customer_email:
        body["customer_details"]["customer_email"] = customer_email
    if expiry_iso:
        body["link_expiry_time"] = expiry_iso

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{_api_base()}/links", headers=_headers(), json=body)
            resp.raise_for_status()
            data = resp.json()
            return {"link_url": data.get("link_url", ""), "link_id": data.get("link_id", link_id)}
    except Exception as e:
        log.error(f"Cashfree create_payment_link failed: {e}")
        return None


def verify_webhook_signature(raw_body: bytes, signature: str, timestamp: str) -> bool:
    """Cashfree webhook signature = base64(HMAC-SHA256(timestamp + raw_body, secret))."""
    secret = settings.cashfree_webhook_secret or settings.cashfree_secret_key
    if not secret or not signature or not timestamp:
        return False
    payload = (timestamp + raw_body.decode("utf-8")).encode("utf-8")
    computed = base64.b64encode(
        hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(computed, signature)


def extract_link_status(payload: dict[str, Any]) -> tuple[str, str]:
    """Returns (link_id, link_status) from a Cashfree Payment Links webhook payload.

    Handles both the top-level "link_status" shape and the nested
    data.link_details shape depending on API version.
    """
    data = payload.get("data", {}) or {}
    link_details = data.get("link_details", data)
    link_id = str(link_details.get("link_id", "") or "")
    link_status = str(link_details.get("link_status", "") or "").upper()
    return link_id, link_status
