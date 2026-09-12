"""Google Sheets client for Kaya — reads the enrollment payments sheet.

Used by the weight-loss enrollment flow: before sending the form/payment
steps, the patient's WhatsApp number is looked up in the clinic's payments
Google Sheet. If a row matches their phone AND its payment status is
"Paid", the patient is enrolled instantly (no form, no payment).

Setup (one-time):
  1. ENROLLMENT_SHEET_ID / ENROLLMENT_SHEET_GID in .env point at the sheet.
  2. Share the sheet with the service account email found in
     GOOGLE_CREDENTIALS_PATH (Viewer access is enough).

Matching is tolerant of column naming ("Phone", "WhatsApp Number",
"Payment Status", ...) and of +91/0/spacing in phone numbers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
from typing import Any

import httpx

from src.config import settings

log = logging.getLogger("kaya.sheets")


def _digits(value: str) -> str:
    return "".join(c for c in (value or "") if c.isdigit())


def _phone_tail(phone_raw: str) -> str:
    digits = _digits(str(phone_raw or ""))
    return digits[-10:] if len(digits) >= 10 else digits


class SheetsClient:
    """Read-only Google Sheets API v4 wrapper using service-account JWT auth."""

    BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets"
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    CACHE_TTL_SECONDS = 30.0

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._access_token: str = ""
        self._token_expiry: float = 0.0
        self._rows_cache: dict[str, tuple[list[list[str]], float]] = {}

    # ------------------------------------------------------------------
    # Auth (same JWT service-account flow as calendar_client.py)
    # ------------------------------------------------------------------
    async def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expiry - 60:
            return self._access_token

        credentials_path = settings.google_credentials_path
        if not credentials_path or not os.path.exists(credentials_path):
            raise RuntimeError(
                f"Google credentials file not found at {credentials_path}. "
                "Set GOOGLE_CREDENTIALS_PATH in your .env file."
            )
        with open(credentials_path, "r") as f:
            creds = json.load(f)

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

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        private_key = load_pem_private_key(creds["private_key"].encode(), password=None)
        signature = private_key.sign(
            f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        jwt_token = f"{header}.{payload}.{sig_b64}"

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

    async def _get(self, path: str, params: dict | None = None) -> Any:
        token = await self._get_access_token()
        resp = await self._client.get(
            f"{self.BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    async def _put(self, path: str, json_body: dict, params: dict | None = None) -> Any:
        token = await self._get_access_token()
        resp = await self._client.put(
            f"{self.BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Sheet reading
    # ------------------------------------------------------------------
    async def _resolve_tab_title(self, meta: dict, gid: str | None = None) -> str:
        """Map a sheet GID to the tab title; default first tab."""
        gid = gid if gid is not None else settings.enrollment_sheet_gid
        sheets = meta.get("sheets", [])
        sheet_label = meta.get("spreadsheetId", settings.enrollment_sheet_id)
        if gid:
            for s in sheets:
                props = s.get("properties", {})
                if str(props.get("sheetId", "")) == str(gid):
                    return props.get("title", "")
            raise RuntimeError(
                f"gid {gid} not found in sheet {sheet_label}"
            )
        if sheets:
            return sheets[0].get("properties", {}).get("title", "")
        raise RuntimeError("Spreadsheet has no tabs")

    async def get_rows(
        self,
        force_refresh: bool = False,
        sheet_id: str | None = None,
        gid: str | None = None,
    ) -> list[list[str]]:
        """All rows of the configured enrollment sheet (cached briefly per
        sheet). Pass sheet_id/gid to read a different spreadsheet/tab."""
        sheet_id = sheet_id or settings.enrollment_sheet_id
        if not sheet_id:
            return []

        cache_key = f"{sheet_id}:{gid or ''}"
        cached = self._rows_cache.get(cache_key)
        if not force_refresh and cached and time.time() < cached[1]:
            return cached[0]

        meta = await self._get(f"/{sheet_id}")
        title = await self._resolve_tab_title(meta, gid)
        data = await self._get(
            f"/{sheet_id}/values/{urllib.parse.quote(title)}",
            params={"majorDimension": "ROWS"},
        )
        rows = [
            ["" if v is None else str(v).strip() for v in row]
            for row in data.get("values", [])
        ]
        self._rows_cache[cache_key] = (rows, time.time() + self.CACHE_TTL_SECONDS)
        return rows

    # ------------------------------------------------------------------
    # Enrollment verification
    # ------------------------------------------------------------------
    @staticmethod
    def _status_is_paid(value: str) -> bool:
        v = str(value or "").strip().lower()
        if re.search(r"\bunpaid\b|\bnot\s*paid\b|\bpending\b|\bdue\b|\bfailed\b|\brefunded\b", v):
            return False
        return bool(re.search(r"\bpaid\b|\bcompleted\b|\bsuccess\b", v))

    @staticmethod
    def _find_status_col(headers: list[str]) -> int | None:
        """Locate the Payment-status column.

        Prefers the actual payment column: search for "payment" / "paid"
        first (e.g. "Payment status"), and only fall back to a generic
        "status" match afterwards. This avoids grabbing unrelated headers
        like "What is your current GLP-1 treatment status?"."""
        for pattern in (r"payment|paid", r"status"):
            idx = next(
                (i for i, h in enumerate(headers) if re.search(pattern, h)),
                None,
            )
            if idx is not None:
                return idx
        return None

    async def find_paid_enrollment(self, phone_raw: str) -> dict[str, str] | None:
        """Return the matched row as {header: value} when `phone_raw` appears
        in the sheet with payment status Paid; None otherwise (not found or
        unpaid). Raises on API errors — callers should treat that as 'no
        match' and fall back to the normal flow."""
        if not settings.enrollment_sheet_id:
            return None

        tail = _phone_tail(phone_raw)
        if not tail:
            return None

        rows = await self.get_rows()
        if len(rows) < 2:
            return None

        headers = [h.strip().lower() for h in rows[0]]
        status_col = self._find_status_col(headers)

        def row_to_dict(row: list[str]) -> dict[str, str]:
            out: dict[str, str] = {}
            for i, val in enumerate(row):
                key = headers[i] if i < len(headers) and headers[i] else f"col{i}"
                out[key] = val.strip()
            return out

        matches: list[tuple[list[str], int]] = []
        for idx, row in enumerate(rows[1:], start=2):
            if any(_phone_tail(cell) == tail and cell for cell in row):
                matches.append((row, idx))
                if len(matches) > 50:
                    break

        for row, _idx in matches:
            if status_col is not None and status_col < len(row):
                if self._status_is_paid(row[status_col]):
                    return row_to_dict(row)
            else:
                # No recognizable status column: accept any explicit "Paid"
                if any(self._status_is_paid(c) for c in row):
                    return row_to_dict(row)

        if matches:
            # Phone present but no paid row — log which rows we saw.
            for row, idx in matches[:3]:
                status_val = row[status_col] if status_col is not None and status_col < len(row) else ""
                log.info(f"Phone {tail} found at row {idx} but status='{status_val}' is not Paid")
        return None

    async def find_patient_row(
        self, email_raw: str = "", phone_raw: str = ""
    ) -> tuple[dict[str, str], bool] | None:
        """Look up a patient in the payments sheet by email first, then by
        phone tail — regardless of payment status. Returns
        (row_as_dict, is_paid) for the MOST RECENT matching row (the sheet
        grows chronologically), or None when nothing matches.

        Used before the screening form: a returning patient whose details are
        already on file should not fill the form again — they go straight to
        payment (unpaid) or get told their screening call already exists (paid).
        Raises on API errors — callers treat that as 'no match' and fall back
        to the standard flow."""
        if not settings.enrollment_sheet_id:
            return None

        email = (email_raw or "").strip().lower()
        tail = _phone_tail(phone_raw)
        if not email and not tail:
            return None

        rows = await self.get_rows()
        if len(rows) < 2:
            return None

        headers = [h.strip().lower() for h in rows[0]]
        status_col = self._find_status_col(headers)
        email_col = next(
            (i for i, h in enumerate(headers) if "mail" in h),
            None,
        )

        def row_to_dict(row: list[str]) -> dict[str, str]:
            out: dict[str, str] = {}
            for i, val in enumerate(row):
                key = headers[i] if i < len(headers) and headers[i] else f"col{i}"
                out[key] = val.strip()
            return out

        matched: list[list[str]] = []
        for row in rows[1:]:
            hit = False
            if email and email_col is not None and email_col < len(row):
                hit = row[email_col].strip().lower() == email
            if not hit and tail:
                hit = any(_phone_tail(cell) == tail and cell for cell in row)
            if hit:
                matched.append(row)

        if not matched:
            return None

        # Latest matching row wins — it reflects the patient's current state.
        row = matched[-1]
        if status_col is not None and status_col < len(row):
            paid = self._status_is_paid(row[status_col])
        else:
            # No recognizable status column: accept any explicit "Paid"
            paid = any(self._status_is_paid(c) for c in row)
        return row_to_dict(row), paid

    async def find_enrollment_row(self, phone_raw: str) -> tuple[dict[str, str], bool] | None:
        """Look up a patient in the enrollment sheet by phone. Returns
        (row_as_dict, is_paid) for the MOST RECENT matching row, or None
        when nothing matches.

        Used by the enrollment approval flow: if the patient already exists
        in the enrollment sheet with status "Paid", they can skip the
        form + payment. If they exist but are "Unpaid", the form step is
        skipped and the payment request goes out directly.

        Raises on API errors — callers treat that as 'no match'."""
        if not settings.enrollment_sheet_id:
            return None

        tail = _phone_tail(phone_raw)
        if not tail:
            return None

        rows = await self.get_rows()
        if len(rows) < 2:
            return None

        headers = [h.strip().lower() for h in rows[0]]
        status_col = self._find_status_col(headers)

        def row_to_dict(row: list[str]) -> dict[str, str]:
            out: dict[str, str] = {}
            for i, val in enumerate(row):
                key = headers[i] if i < len(headers) and headers[i] else f"col{i}"
                out[key] = val.strip()
            return out

        matched: list[list[str]] = []
        for row in rows[1:]:
            if any(_phone_tail(cell) == tail and cell for cell in row):
                matched.append(row)

        if not matched:
            return None

        row = matched[-1]
        if status_col is not None and status_col < len(row):
            paid = self._status_is_paid(row[status_col])
        else:
            paid = any(self._status_is_paid(c) for c in row)
        return row_to_dict(row), paid

    async def find_enrollment_program_row(
        self, phone_raw: str, plan_name: str
    ) -> tuple[dict[str, str], str] | None:
        """Look up a patient in the ENROLLMENT PROGRAM-TIER sheet.

        Returns (row_as_dict, program_tier) for the MOST RECENT row where
        BOTH the phone number AND the selected program tier match, and whose
        payment status is "Paid". Returns None otherwise (no match, wrong
        program, not paid, or sheet error).

        This is the check used on program selection: the patient can only be
        "already registered" when the SAME phone appears for the SAME program
        with a Paid status. Same phone on a different program does not block —
        the form/payment flow proceeds for the new program."""
        program_sheet_id = settings.enrollment_program_sheet_id
        if not program_sheet_id:
            return None

        tail = _phone_tail(phone_raw)
        if not tail or not plan_name:
            return None

        rows = await self.get_rows(
            sheet_id=program_sheet_id,
            gid=settings.enrollment_program_sheet_gid,
        )
        if len(rows) < 2:
            return None

        headers = [h.strip().lower() for h in rows[0]]
        status_col = self._find_status_col(headers)
        phone_col = next(
            (i for i, h in enumerate(headers) if re.search(r"mobile|phone|whatsapp", h)),
            None,
        )
        program_col = next(
            (i for i, h in enumerate(headers) if "program" in h or "tier" in h),
            None,
        )

        def row_to_dict(row: list[str]) -> dict[str, str]:
            out: dict[str, str] = {}
            for i, val in enumerate(row):
                key = headers[i] if i < len(headers) and headers[i] else f"col{i}"
                out[key] = val.strip()
            return out

        plan_norm = (plan_name or "").strip().lower()

        for row in reversed(rows[1:]):
            def cell(i: int) -> str:
                return row[i].strip() if i is not None and i < len(row) else ""

            phone_val = cell(phone_col)
            if not phone_val or _phone_tail(phone_val) != tail:
                continue

            program_val = cell(program_col)
            if not program_val or plan_norm not in program_val.strip().lower():
                continue

            if status_col is not None:
                if not self._status_is_paid(cell(status_col)):
                    continue
            else:
                # No recognizable status column: accept any explicit "Paid"
                if not any(self._status_is_paid(c) for c in row):
                    continue
            return row_to_dict(row), program_val.strip()

        return None

    async def is_enrollment_paid(self, phone_raw: str) -> bool:
        """True if the patient's phone is in the enrollment sheet with
        payment status Paid.  Shorthand around find_enrollment_row."""
        try:
            result = await self.find_enrollment_row(phone_raw)
            return result is not None and result[1]
        except Exception:
            return False

    async def is_screening_paid(self, phone_raw: str) -> bool:
        """True if the patient's phone appears in the SCREENING sheet
        (ENROLLMENT_SHEET_ID) with payment status "Paid".

        Used as a gate BEFORE weight-loss program selection: only patients
        who have completed AND paid for their screening call may pick a
        program. Fails closed (False) on any error."""
        try:
            result = await self.find_patient_row("", phone_raw)
            return result is not None and result[1]
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Payment status write
    # ------------------------------------------------------------------
    async def update_payment_status(
        self,
        phone: str,
        email: str,
        status: str,
        sheet_id: str | None = None,
        gid: str | None = None,
        plan_name: str = "",
    ) -> bool:
        """Find the patient's row by phone (or email) and write *status*
        ("Pending" / "Paid") into the Payment status column.
        Pass sheet_id/gid to write into a different spreadsheet/tab
        (e.g. the enrollment program-tier sheet), and plan_name to also
        require the row's program column to contain that plan name.

        Returns True on success, False on any error (logged, never raised).
        """
        sheet_id = sheet_id or settings.enrollment_sheet_id
        if not sheet_id:
            return False

        tail = _phone_tail(phone)
        email_clean = (email or "").strip().lower()
        if not tail and not email_clean:
            return False

        try:
            rows = await self.get_rows(force_refresh=True, sheet_id=sheet_id, gid=gid)
        except Exception as e:
            log.error(f"update_payment_status: failed to read rows: {e}")
            return False

        if len(rows) < 2:
            return False

        headers = [h.strip().lower() for h in rows[0]]

        # Locate the payment-status column
        status_col = self._find_status_col(headers)
        if status_col is None:
            log.warning("update_payment_status: no payment/status column found in headers")
            return False

        # Locate email column for fallback matching
        email_col = next(
            (i for i, h in enumerate(headers) if "mail" in h),
            None,
        )

        # Locate program column when a plan filter is requested
        program_col = None
        plan_norm = (plan_name or "").strip().lower()
        if plan_norm:
            program_col = next(
                (i for i, h in enumerate(headers) if "program" in h or "tier" in h),
                None,
            )

        # Find the matching row (last match wins — most recent)
        matched_row_idx: int | None = None
        for idx, row in enumerate(rows[1:], start=2):
            hit = False
            if tail:
                hit = any(_phone_tail(cell) == tail and cell for cell in row)
            if not hit and email_clean and email_col is not None and email_col < len(row):
                hit = row[email_col].strip().lower() == email_clean
            if hit and plan_norm and program_col is not None and program_col < len(row):
                hit = plan_norm in row[program_col].strip().lower()
            if hit:
                matched_row_idx = idx

        if matched_row_idx is None:
            log.info(
                f"update_payment_status: no row found for phone={tail} email={email_clean}"
            )
            return False

        # Convert column index to letter (0 -> A, 1 -> B, ... 18 -> S)
        col_letter = ""
        c = status_col
        while True:
            col_letter = chr(ord("A") + c % 26) + col_letter
            c = c // 26 - 1
            if c < 0:
                break

        # Resolve sheet tab title for the range
        try:
            meta = await self._get(f"/{sheet_id}")
            tab_title = await self._resolve_tab_title(meta, gid)
        except Exception as e:
            log.error(f"update_payment_status: failed to resolve tab title: {e}")
            return False

        range_str = f"{tab_title}!{col_letter}{matched_row_idx}"

        try:
            await self._put(
                f"/{sheet_id}/values/{urllib.parse.quote(range_str)}",
                json_body={"values": [[status]]},
                params={"valueInputOption": "USER_ENTERED"},
            )
            log.info(
                f"update_payment_status: set '{status}' at {range_str} for phone={tail}"
            )
            return True
        except Exception as e:
            log.error(f"update_payment_status: PUT failed for range {range_str}: {e}")
            return False

    async def check_access(self) -> bool:
        """True if the sheet can be read right now (startup sanity check)."""
        try:
            await self.get_rows(force_refresh=True)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()


# Global instance
sheets_client = SheetsClient()
