"""One-time Google Calendar OAuth2 setup for Kaya.

Run this once to authorize the bot to act as the doctor's own Google account
(kayakalp.drlekha@gmail.com), which is what allows it to email calendar
invites to patients — a bare service account is blocked from doing this by
Google on a personal Gmail calendar.

Usage:
    1. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env
       (from Google Cloud Console -> APIs & Services -> Credentials ->
       OAuth client ID -> type "Desktop app").
    2. Run: python scripts/google_oauth_setup.py
    3. A URL will be printed. Open it, log in as kayakalp.drlekha@gmail.com,
       and click Allow.
    4. This script will capture the result automatically and print a
       GOOGLE_OAUTH_REFRESH_TOKEN value to paste into .env.
"""

from __future__ import annotations

import http.server
import threading
import urllib.parse
import webbrowser

import httpx

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings

REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/"
SCOPE = "https://www.googleapis.com/auth/calendar"

_auth_code: str | None = None
_done = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        global _auth_code
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        code = qs.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if code:
            _auth_code = code
            self.wfile.write(b"<html><body><h2>Authorized. You can close this tab and return to the terminal.</h2></body></html>")
        else:
            self.wfile.write(b"<html><body><h2>No authorization code received. Check the terminal.</h2></body></html>")
        _done.set()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # silence default request logging


def main() -> None:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        print("ERROR: set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env first.")
        return

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # forces a refresh_token even on repeat runs
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

    print("\nOpen this URL and log in as the doctor's Google account:\n")
    print(auth_url)
    print("\nWaiting for authorization...")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    _done.wait(timeout=300)
    server.shutdown()

    if not _auth_code:
        print("ERROR: no authorization code received (timed out or denied).")
        return

    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": _auth_code,
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    resp.raise_for_status()
    tokens = resp.json()
    refresh_token = tokens.get("refresh_token")

    if not refresh_token:
        print("ERROR: Google did not return a refresh_token. This usually means you've")
        print("already authorized this app before without revoking access. Go to")
        print("https://myaccount.google.com/permissions, remove this app's access, and")
        print("run this script again.")
        return

    print("\nSuccess! Add this line to your .env file:\n")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={refresh_token}")
    print()


if __name__ == "__main__":
    main()
