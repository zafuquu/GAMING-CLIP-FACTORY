"""Local Twitch OAuth for official, authorized clip downloads.

Run from backend: python -m app.twitch_user_auth
The user token is kept in the OS keyring, never printed or committed.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from datetime import datetime, timedelta, timezone
import json
import os
import secrets
import threading
import time
import webbrowser

import keyring
import requests

from dotenv import load_dotenv

SERVICE_NAME = "gaming-clip-factory-ai"
TOKEN_KEY = "twitch-user-oauth-token"
REQUIRED_SCOPES = {"channel:manage:clips", "editor:manage:clips"}
DEFAULT_REDIRECT_URI = "http://localhost:8766/callback"


class TwitchUserNotConfigured(RuntimeError):
    retryable = False


def _client_credentials():
    client_id = os.getenv("GCF_TWITCH_CLIENT_ID", "").strip()
    client_secret = os.getenv("GCF_TWITCH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise TwitchUserNotConfigured(
            "Set GCF_TWITCH_CLIENT_ID and GCF_TWITCH_CLIENT_SECRET in .env, then register the local redirect URI in your Twitch developer app."
        )
    return client_id, client_secret


def _save_token(payload: dict, client_id: str, user_id: str):
    now = datetime.now(timezone.utc)
    safe = {
        "client_id": client_id,
        "access_token": str(payload.get("access_token", "")),
        "refresh_token": str(payload.get("refresh_token", "")),
        "expires_at": (now + timedelta(seconds=max(60, int(payload.get("expires_in", 0)) - 60))).isoformat(),
        "scope": list(payload.get("scope") or []),
        "user_id": user_id,
        "token_type": str(payload.get("token_type", "bearer")),
    }
    if not safe["access_token"] or not safe["refresh_token"] or not user_id:
        raise TwitchUserNotConfigured("Twitch OAuth returned incomplete credentials. Reconnect the Twitch account.")
    if not REQUIRED_SCOPES.intersection(safe["scope"]):
        raise TwitchUserNotConfigured("The Twitch account did not grant channel:manage:clips or editor:manage:clips.")
    keyring.set_password(SERVICE_NAME, TOKEN_KEY, json.dumps(safe))
    return safe


def load_user_token(session=None) -> dict:
    client_id, client_secret = _client_credentials()
    raw = keyring.get_password(SERVICE_NAME, TOKEN_KEY)
    if not raw:
        raise TwitchUserNotConfigured(
            "Twitch clip download is not connected. From backend, run: python -m app.twitch_user_auth"
        )
    try:
        token = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise TwitchUserNotConfigured("Stored Twitch authorization is unreadable; reconnect Twitch.") from exc
    if token.get("client_id") != client_id:
        raise TwitchUserNotConfigured("Twitch app credentials changed since authorization; reconnect Twitch.")
    if not REQUIRED_SCOPES.intersection(set(token.get("scope") or [])):
        raise TwitchUserNotConfigured("Twitch authorization is missing a clip-download scope; reconnect Twitch.")
    access = str(token.get("access_token") or "")
    refresh = str(token.get("refresh_token") or "")
    try:
        expires = datetime.fromisoformat(str(token.get("expires_at", "")).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except ValueError:
        expires = datetime.min.replace(tzinfo=timezone.utc)
    if not access or expires <= datetime.now(timezone.utc):
        if not refresh:
            raise TwitchUserNotConfigured("Twitch access token expired without a refresh token; reconnect Twitch.")
        own_session = session or requests.Session()
        try:
            response = own_session.post(
                "https://id.twitch.tv/oauth2/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
                timeout=(5.0, 20.0),
            )
            if response.status_code >= 400:
                raise TwitchUserNotConfigured(
                    f"Twitch token refresh failed with HTTP {response.status_code}; reconnect Twitch."
                )
            payload = response.json()
            payload.setdefault("scope", token.get("scope") or [])
            payload.setdefault("refresh_token", refresh)
            token = _save_token(payload, client_id, str(token.get("user_id") or ""))
        except requests.RequestException as exc:
            raise TwitchUserNotConfigured(f"Twitch token refresh failed: {type(exc).__name__}") from exc
    return token


def main():
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)
    client_id, client_secret = _client_credentials()
    redirect_uri = os.getenv("GCF_TWITCH_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip()
    parsed_redirect = urlparse(redirect_uri)
    if parsed_redirect.scheme != "http" or parsed_redirect.hostname not in {"localhost", "127.0.0.1"} or parsed_redirect.port != 8766 or parsed_redirect.path != "/callback":
        raise SystemExit("For local Twitch OAuth, GCF_TWITCH_REDIRECT_URI must be http://localhost:8766/callback (or 127.0.0.1 with the same port/path).")
    state = secrets.token_urlsafe(32)
    received = threading.Event()
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            if (query.get("state") or [""])[0] != state:
                result["error"] = "OAuth state mismatch; authorization response rejected."
            elif query.get("error"):
                result["error"] = "Twitch authorization was declined: " + (query.get("error_description") or query.get("error"))[0][:300]
            elif not (query.get("code") or [""])[0]:
                result["error"] = "Twitch authorization callback did not include a code."
            else:
                result["code"] = query["code"][0]
            body = (
                "<!doctype html><html><meta charset='utf-8'><title>Gaming Clip Factory</title>"
                "<body style='font:16px system-ui;max-width:680px;margin:10vh auto;padding:24px'>"
                "<h1>Twitch authorization response received</h1><p>You may close this tab and return to the terminal.</p></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            received.set()

        def log_message(self, format, *args):
            return

    try:
        server = HTTPServer(("127.0.0.1", 8766), Handler)
    except OSError as exc:
        raise SystemExit("Could not bind the Twitch OAuth callback on 127.0.0.1:8766. Stop the conflicting process and retry.") from exc
    server.timeout = 0.5
    authorization_url = "https://id.twitch.tv/oauth2/authorize?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(sorted(REQUIRED_SCOPES)),
        "state": state,
        "force_verify": "true",
    })
    print("This local setup uses Twitch's official authorization flow; it does not require a paid subscription.")
    print("Ensure your Twitch developer app has this exact OAuth redirect URI registered:")
    print("  " + redirect_uri)
    print("A browser will open so you can sign in to the broadcaster/editor account allowed to download the clips.")
    print("No access token or refresh token will be printed.")
    webbrowser.open(authorization_url)
    deadline = time.monotonic() + 300
    try:
        while not received.is_set() and time.monotonic() < deadline:
            server.handle_request()
        if not received.is_set():
            raise SystemExit("Twitch authorization timed out after 5 minutes. Run the command again.")
    finally:
        server.server_close()
    if result.get("error"):
        raise SystemExit(result["error"])
    try:
        response = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": result["code"],
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=(5.0, 20.0),
        )
        if response.status_code >= 400:
            raise SystemExit(f"Twitch authorization-code exchange failed with HTTP {response.status_code}. Check your app's client ID/secret and redirect URI.")
        token_payload = response.json()
        access = token_payload.get("access_token")
        if not access:
            raise SystemExit("Twitch OAuth response did not include an access token.")
        user_response = requests.get(
            "https://api.twitch.tv/helix/users",
            headers={"Client-Id": client_id, "Authorization": "Bearer " + access},
            timeout=(5.0, 20.0),
        )
        if user_response.status_code >= 400:
            raise SystemExit(f"Could not verify the authorized Twitch user (HTTP {user_response.status_code}).")
        users = user_response.json().get("data") or []
        if not users or not users[0].get("id"):
            raise SystemExit("Twitch did not return an authenticated user ID.")
        _save_token(token_payload, client_id, str(users[0]["id"]))
    except requests.RequestException as exc:
        raise SystemExit(f"Twitch authorization setup failed: {type(exc).__name__}.") from exc
    print("Twitch user authorization saved to the OS keyring.")
    print("The app can now request official clip-download URLs only for channels where this account is authorized.")
    print("Return to the app and refresh; Twitch download status should show connected.")


if __name__ == "__main__":
    main()
