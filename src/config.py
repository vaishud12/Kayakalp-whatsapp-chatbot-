"""Configuration loading for Kaya.

Loads settings from environment variables (via .env) and from
`config/settings.json` (non-secret application settings).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _read_settings() -> dict:
    settings_path = PROJECT_ROOT / "config" / "settings.json"
    if settings_path.exists():
        with settings_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


class Settings:
    """All configuration for the app, merged from env + settings.json."""

    @staticmethod
    def _resolve_credentials_path(path: str) -> str:
        """Fall back to Render's /etc/secrets mount if the path doesn't exist.

        Render mounts Secret Files (e.g. the service-account JSON) at
        /etc/secrets/<filename>. If GOOGLE_CREDENTIALS_PATH points at a
        basename that isn't present on disk, look for it there instead.
        """
        if os.path.exists(path):
            return path
        candidate = Path("/etc/secrets") / Path(path).name
        return str(candidate) if candidate.is_file() else path

    def __init__(self) -> None:
        file_settings = _read_settings()
        self.settings_file = file_settings

        # --- Google Gemini (FAQ embeddings + image/PDF analysis) ---
        self.google_api_key: str = os.getenv("GOOGLE_API_KEY", "").strip()
        # Note: image/PDF analysis uses Gemini 3.1 Flash Lite
        # (gemini-3.1-flash-lite) directly via the Google Gemini API.

        # --- OpenRouter (for RAG answer drafting only — Gemma 4 31B free) ---
        self.openrouter_api_key: str = os.getenv(
            "OPENROUTER_API_KEY",
            "",
        ).strip()
        self.openrouter_base_url: str = os.getenv(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        ).strip()
        self.openrouter_fallback_models: list[str] = [
            m.strip()
            for m in os.getenv(
                "OPENROUTER_FALLBACK_MODELS",
                "google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3-super-120b-a12b:free,poolside/laguna-xs-2.1:free",
            ).split(",")
            if m.strip()
        ]
        # --- WhatsApp Cloud API ---
        self.whatsapp_access_token: str = os.getenv(
            "WHATSAPP_ACCESS_TOKEN", ""
        ).strip()
        self.whatsapp_phone_number_id: str = os.getenv(
            "WHATSAPP_PHONE_NUMBER_ID", ""
        ).strip()
        self.whatsapp_verify_token: str = os.getenv(
            "WHATSAPP_VERIFY_TOKEN", ""
        ).strip()
        self.whatsapp_webhook_secret: str = os.getenv(
            "WHATSAPP_WEBHOOK_SECRET", ""
        ).strip()

        # --- Google Calendar ---
        self.google_calendar_id: str = os.getenv(
            "GOOGLE_CALENDAR_ID",
            file_settings.get("google_calendar_id", ""),
        ).strip()
        self.google_credentials_path: str = self._resolve_credentials_path(
            os.getenv(
                "GOOGLE_CREDENTIALS_PATH",
                str(PROJECT_ROOT / "credentials" / "service-account.json"),
            ).strip()
        )

        # --- Google Calendar OAuth2 (user login — required to email attendees) ---
        # A bare service account cannot invite attendees on a personal Gmail
        # calendar (Google blocks it without Domain-Wide Delegation). OAuth2
        # authenticates as the real doctor account instead, which can. Set
        # these to enable native calendar invite emails; leave blank to keep
        # using the service account (events still get created, just without
        # an automatic email invite — see calendar_client.py's fallback).
        self.google_oauth_client_id: str = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        self.google_oauth_client_secret: str = os.getenv(
            "GOOGLE_OAUTH_CLIENT_SECRET", ""
        ).strip()
        self.google_oauth_refresh_token: str = os.getenv(
            "GOOGLE_OAUTH_REFRESH_TOKEN", ""
        ).strip()

        # --- Google Apps Script (booking website submit) ---
        self.google_apps_script_url: str = os.getenv(
            "GOOGLE_APPS_SCRIPT_URL", ""
        ).strip()

        # --- App ---
        self.app_host: str = os.getenv("APP_HOST", "0.0.0.0").strip()
        self.app_port: int = int(os.getenv("APP_PORT", "8000"))
        self.app_base_url: str = os.getenv("APP_BASE_URL", "").strip()

        # --- Clinic website ---
        self.website_url: str = os.getenv(
            "WEBSITE_URL",
            file_settings.get("website_url", ""),
        ).strip()

        # --- Doctor approval ---
        self.approval_timeout_minutes: int = int(
            os.getenv("APPROVAL_TIMEOUT_MINUTES", "10")
        )

        # --- Weight-loss program enrollment ---
        self.enrollment_form_url: str = os.getenv(
            "ENROLLMENT_FORM_URL",
            file_settings.get(
                "enrollment_form_url", "https://forms.gle/KFwguS13cwEbrSFn8"
            ),
        ).strip()

        # --- Enrollment payments Google Sheet (pre-paid verification) ---
        # When a patient picks a weight-loss plan, their WhatsApp number is
        # looked up here first: a row with payment status "Paid" enrolls them
        # instantly (no form, no payment steps). Leave the ID blank to disable.
        self.enrollment_sheet_id: str = os.getenv(
            "ENROLLMENT_SHEET_ID",
            file_settings.get("enrollment_sheet_id", ""),
        ).strip()
        self.enrollment_sheet_gid: str = os.getenv(
            "ENROLLMENT_SHEET_GID",
            file_settings.get("enrollment_sheet_gid", ""),
        ).strip()

        # --- Enrollment program-tier Google Sheet (pre-paid program check) ---
        # When a patient picks a weight-loss plan, their WhatsApp number AND
        # the selected program are looked up here: a row matching BOTH the
        # phone and the "Enrolled Program Tier" with payment status "Paid"
        # means they are already registered for that same program (no form).
        # This is separate from ENROLLMENT_SHEET_ID (the screening-call sheet).
        self.enrollment_program_sheet_id: str = os.getenv(
            "ENROLLMENT_PROGRAM_SHEET_ID",
            file_settings.get("enrollment_program_sheet_id", ""),
        ).strip()
        self.enrollment_program_sheet_gid: str = os.getenv(
            "ENROLLMENT_PROGRAM_SHEET_GID",
            file_settings.get("enrollment_program_sheet_gid", ""),
        ).strip()

        # --- Post-approval payment flow ---
        self.screening_form_url: str = os.getenv(
            "SCREENING_FORM_URL",
            file_settings.get("screening_form_url", ""),
        ).strip()
        self.upi_id: str = os.getenv("UPI_ID", "").strip()
        self.upi_payee_name: str = os.getenv("UPI_PAYEE_NAME", "").strip()
        self.payment_amount: str = os.getenv("PAYMENT_AMOUNT", "").strip()
        self.skin_general_payment_amount: str = os.getenv(
            "SKIN_GENERAL_PAYMENT_AMOUNT", "400"
        ).strip()
        self.pay_reminder_1_hours: float = float(
            os.getenv("PAY_REMINDER_1_HOURS", "24")
        )
        self.pay_reminder_2_days: float = float(os.getenv("PAY_REMINDER_2_DAYS", "3"))
        self.pay_expiry_days: float = float(os.getenv("PAY_EXPIRY_DAYS", "7"))

        # --- PDF documents sent to patients after payment ---
        self.program_detail_pdf_url: str = os.getenv(
            "PROGRAM_DETAIL_PDF_URL",
            "https://drive.google.com/uc?export=download&id=1ofZtY07QXMSeKg3BBsC7WA7m6DJpd2IZ",
        )
        self.welcome_pdf_url: str = os.getenv(
            "WELCOME_PDF_URL",
            "https://drive.google.com/uc?export=download&id=12Djb2yLKYpub04sZx0z4ufClbYqTiK5W",
        )

        # --- Cashfree Payment Links (auto-verified payment; optional) ---
        # When configured, patients get a Cashfree hosted payment link instead
        # of raw UPI instructions, and payment is auto-confirmed via webhook
        # instead of the doctor manually reviewing a screenshot.
        self.cashfree_app_id: str = os.getenv("CASHFREE_APP_ID", "").strip()
        self.cashfree_secret_key: str = os.getenv("CASHFREE_SECRET_KEY", "").strip()
        self.cashfree_env: str = os.getenv("CASHFREE_ENV", "sandbox").strip().lower()
        self.cashfree_webhook_secret: str = os.getenv(
            "CASHFREE_WEBHOOK_SECRET", ""
        ).strip()

        # --- Clinic contact ---
        self.clinic_hotline: str = os.getenv(
            "CLINIC_HOTLINE",
            file_settings.get("clinic_hotline", "+917666320828"),
        ).strip()

        # --- RAG (local FAQ search) ---
        self.faq_xlsx_path: str = str(
            file_settings.get(
                "faq_xlsx_path",
                str(
                    PROJECT_ROOT
                    / "src"
                    / "Docs"
                    / "KayaKalp_WhatsApp_FAQ_RAG_EXPANDED (1).xlsx"
                ),
            )
        )
        self.rag_top_k: int = int(file_settings.get("rag_top_k", 3))
        # Minimum cosine-similarity score (0–1) to accept a RAG result. ChromaDB
        # cosine-similarity: 0.65 is a safe default — out-of-scope queries typically
        # score 0.50–0.62; in-scope queries score 0.70+. Increase to be more
        # conservative (fewer but more accurate answers), decrease to accept more.
        self.rag_min_score: float = float(file_settings.get("rag_min_score", 0.65))
        # OpenRouter model for drafting RAG answers (text-only, no vision needed).
        # Falls back to openrouter_fallback_models on errors.
        self.openrouter_text_model: str = os.getenv(
            "OPENROUTER_TEXT_MODEL",
            "google/gemma-4-31b-it:free",
        ).strip()

    @property
    def cashfree_enabled(self) -> bool:
        return bool(self.cashfree_app_id and self.cashfree_secret_key)

    @property
    def google_oauth_enabled(self) -> bool:
        return bool(
            self.google_oauth_client_id
            and self.google_oauth_client_secret
            and self.google_oauth_refresh_token
        )

    @property
    def is_complete(self) -> bool:
        """True when all required secrets are present."""
        required = [
            self.whatsapp_access_token,
            self.whatsapp_phone_number_id,
        ]
        return all(required)


settings = Settings()
