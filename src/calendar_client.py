"""Google Calendar client for Kaya.

Handles:
- Fetching free/busy slots for a given date
- Creating calendar events with attendee invites
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from src.config import settings


class CalendarClient:
    """Google Calendar API wrapper using service account credentials."""

    BASE_URL = "https://www.googleapis.com/calendar/v3"
    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self) -> None:
        self.calendar_id = settings.google_calendar_id
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._access_token: str = ""
        self._token_expiry: float = 0.0

    async def _get_access_token(self) -> str:
        """Get or refresh an access token.

        Uses OAuth2 (the doctor's own logged-in account) when configured —
        required to email calendar invites to attendees. Falls back to the
        service account otherwise (events still get created, just without an
        automatic invite email — see create_event()'s fallback).
        """
        import time
        now = time.time()

        if self._access_token and now < self._token_expiry - 60:
            return self._access_token

        if settings.google_oauth_enabled:
            resp = await self._client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": settings.google_oauth_client_id,
                    "client_secret": settings.google_oauth_client_secret,
                    "refresh_token": settings.google_oauth_refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            resp.raise_for_status()
            token_data = resp.json()
            self._access_token = token_data["access_token"]
            self._token_expiry = now + token_data.get("expires_in", 3600)
            return self._access_token

        return await self._get_service_account_token(now)

    async def _get_service_account_token(self, now: float) -> str:
        credentials_path = settings.google_credentials_path
        if not credentials_path or not os.path.exists(credentials_path):
            raise RuntimeError(
                f"Google credentials file not found at {credentials_path}. "
                "Set GOOGLE_CREDENTIALS_PATH in your .env file."
            )

        with open(credentials_path, "r") as f:
            creds = json.load(f)

        # Build JWT for service account
        import base64
        now_ts = int(now)
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
        ).rstrip(b"=").decode()

        claim_set = {
            "iss": creds["client_email"],
            "scope": " ".join(self.SCOPES),
            "aud": "https://oauth2.googleapis.com/token",
            "iat": now_ts,
            "exp": now_ts + 3600,
        }
        payload = base64.urlsafe_b64encode(
            json.dumps(claim_set).encode()
        ).rstrip(b"=").decode()

        # Sign with private key
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        private_key = load_pem_private_key(
            creds["private_key"].encode(), password=None
        )
        signature = private_key.sign(
            f"{header}.{payload}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()

        jwt_token = f"{header}.{payload}.{sig_b64}"

        # Exchange JWT for access token
        resp = await self._client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": jwt_token,
            },
        )
        resp.raise_for_status()
        token_data = resp.json()

        self._access_token = token_data["access_token"]
        self._token_expiry = now + token_data.get("expires_in", 3600)
        return self._access_token

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Make an authenticated request to the Calendar API."""
        token = await self._get_access_token()
        url = f"{self.BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {token}"}
        resp = await self._client.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def get_busy_slots(
        self,
        date_str: str,
        slot_starts: list[int],
        slot_duration_minutes: int = 30,
        ist_offset_hours: float = 5.5,
    ) -> set[int]:
        """Return the subset of `slot_starts` (HHMM ints, e.g. 1030 = 10:30)
        that overlap an existing calendar event on the given date
        (DD-MM-YYYY format). Checks real start/end overlap, not just
        whichever hour an event happens to start in — a 15-minute event
        at 10:00 only blocks the 10:00 slot, not 10:30 as well.
        """
        parts = date_str.split("-")
        iso_date = f"{parts[2]}-{parts[1]}-{parts[0]}"
        time_min = f"{iso_date}T00:00:00+05:30"
        time_max = f"{iso_date}T23:59:59+05:30"

        try:
            data = await self._request(
                "GET",
                f"/calendars/{self.calendar_id}/events",
                params={
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": "100",
                },
            )
        except Exception as e:
            print(f"[Calendar] Error fetching events: {e}")
            return set()

        ist = timezone(timedelta(hours=ist_offset_hours))

        # Collect real (start, end) busy intervals first
        intervals: list[tuple[datetime, datetime]] = []
        for event in data.get("items", []):
            start = event.get("start", {})
            end = event.get("end", {})
            start_str = start.get("dateTime", "")
            end_str = end.get("dateTime", "")
            if not start_str:
                continue
            try:
                start_ist = datetime.fromisoformat(start_str).astimezone(ist)
                # All-day events / missing end time: assume one slot's worth
                end_ist = (
                    datetime.fromisoformat(end_str).astimezone(ist)
                    if end_str
                    else start_ist + timedelta(minutes=slot_duration_minutes)
                )
                intervals.append((start_ist, end_ist))
            except Exception:
                continue

        if not intervals:
            return set()

        # A candidate slot is busy if it overlaps ANY event interval at all —
        # not just if it shares the same starting hour.
        year, month, day = int(parts[2]), int(parts[1]), int(parts[0])
        busy: set[int] = set()
        for hhmm in slot_starts:
            h, m = divmod(hhmm, 100)
            slot_start = datetime(year, month, day, h, m, tzinfo=ist)
            slot_end = slot_start + timedelta(minutes=slot_duration_minutes)
            for ev_start, ev_end in intervals:
                if slot_start < ev_end and slot_end > ev_start:
                    busy.add(hhmm)
                    break

        return busy

    async def create_event(
        self,
        summary: str,
        start_iso: str,
        end_iso: str,
        description: str = "",
        attendee_email: str = "",
    ) -> dict[str, Any]:
        """Create a calendar event, with an attendee invite if possible.

        A bare service account (no Domain-Wide Delegation — which requires a
        paid Google Workspace domain, not available on a personal @gmail.com
        calendar) is categorically blocked by Google from inviting attendees:
        POST fails with 403 "forbiddenForServiceAccounts". If that happens,
        retry once without the attendee so the event still gets created and
        the doctor's calendar stays accurate — the patient still gets the
        appointment details via WhatsApp, just not a native Calendar invite
        email for now.
        """
        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start_iso, "timeZone": "Asia/Kolkata"},
            "end": {"dateTime": end_iso, "timeZone": "Asia/Kolkata"},
        }
        if description:
            body["description"] = description
        if attendee_email:
            body["attendees"] = [{"email": attendee_email}]

        try:
            return await self._request(
                "POST",
                f"/calendars/{self.calendar_id}/events",
                json=body,
                params={"sendUpdates": "all" if attendee_email else "none"},
            )
        except httpx.HTTPStatusError as e:
            if (
                attendee_email
                and e.response.status_code == 403
                and "forbiddenForServiceAccounts" in e.response.text
            ):
                body.pop("attendees", None)
                return await self._request(
                    "POST",
                    f"/calendars/{self.calendar_id}/events",
                    json=body,
                    params={"sendUpdates": "none"},
                )
            raise

    async def close(self) -> None:
        await self._client.aclose()


# Global instance
calendar_client = CalendarClient()
