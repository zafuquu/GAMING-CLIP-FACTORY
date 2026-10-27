"""Download authorized Twitch clips via Twitch's official Clips Download API.

Only the broadcaster or an authorized channel editor can download a source clip.
The API returns short-lived CDN URLs; this module validates the host and streams to disk.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
import re

import requests

from ..twitch_user_auth import load_user_token, TwitchUserNotConfigured

TWITCH_API = "https://api.twitch.tv/helix"
MAX_CLIP_BYTES = 512 * 1024 * 1024
CLIP_ID = re.compile(r"^[A-Za-z0-9_-]{3,120}$")


class TwitchClipDownloadError(RuntimeError):
    retryable = False


def clip_slug_from_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme != "https" or parsed.hostname not in {"twitch.tv", "www.twitch.tv", "clips.twitch.tv"}:
        raise ValueError("Only HTTPS Twitch clip URLs can be fetched with the official downloader.")
    parts = [segment for segment in parsed.path.split("/") if segment]
    slug = ""
    if parsed.hostname == "clips.twitch.tv":
        slug = parts[0] if parts else ""
    else:
        if len(parts) >= 3 and parts[1].lower() == "clip":
            slug = parts[2]
        elif len(parts) >= 2 and parts[0].lower() == "clip":
            slug = parts[1]
    if not CLIP_ID.fullmatch(slug):
        raise ValueError("The Twitch source URL does not contain a supported clip ID.")
    return slug


def _cdn_url(value: str) -> str:
    parsed = urlparse(str(value or ""))
    host = (parsed.hostname or "").lower()
    allowed = host.endswith(".twitch.tv") or host == "twitch.tv" or host.endswith(".twitchcdn.net") or host == "twitchcdn.net"
    if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
        raise TwitchClipDownloadError("Twitch returned an unexpected CDN URL; download stopped for safety.")
    return value


class TwitchClipDownloader:
    def __init__(self, session=None, max_bytes: int = MAX_CLIP_BYTES):
        self.session = session or requests.Session()
        self.max_bytes = max(1, min(int(max_bytes), MAX_CLIP_BYTES))

    def _api_get(self, token: dict, endpoint: str, params: dict) -> dict:
        client_id = token["client_id"]
        response = self.session.get(
            TWITCH_API + "/" + endpoint,
            headers={"Client-Id": client_id, "Authorization": "Bearer " + token["access_token"]},
            params=params,
            timeout=(5.0, 30.0),
            allow_redirects=False,
        )
        if response.status_code >= 400:
            if response.status_code in {401, 403}:
                raise TwitchUserNotConfigured(
                    "Twitch refused clip download authorization. Confirm that you are the broadcaster or a channel editor, then reconnect Twitch."
                )
            raise TwitchClipDownloadError(f"Twitch API {endpoint} returned HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TwitchClipDownloadError("Twitch API returned invalid JSON.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise TwitchClipDownloadError("Twitch API returned an unexpected response.")
        return payload

    def get_download_url(self, source_url: str) -> tuple[str, dict]:
        slug = clip_slug_from_url(source_url)
        token = load_user_token(self.session)
        clips = self._api_get(token, "clips", {"id": slug}).get("data", [])
        clip = next((row for row in clips if str(row.get("id", "")) == slug), None)
        if not clip:
            raise FileNotFoundError("Twitch could not find this clip. It may have expired or the URL may be incorrect.")
        broadcaster_id = str(clip.get("broadcaster_id") or "")
        if not broadcaster_id:
            raise TwitchClipDownloadError("Twitch clip metadata did not include a broadcaster ID.")
        user_response = self.session.get(
            TWITCH_API + "/users",
            headers={"Client-Id": token["client_id"], "Authorization": "Bearer " + token["access_token"]},
            timeout=(5.0, 20.0),
            allow_redirects=False,
        )
        if user_response.status_code >= 400:
            raise TwitchUserNotConfigured("Twitch user token is no longer valid. Reconnect Twitch.")
        users = user_response.json().get("data") or []
        user_id = str((users[0] if users else {}).get("id") or token.get("user_id") or "")
        if not user_id or user_id != str(token.get("user_id") or ""):
            raise TwitchUserNotConfigured("The connected Twitch user could not be verified. Reconnect Twitch.")
        downloads = self._api_get(token, "clips/downloads", {
            "broadcaster_id": broadcaster_id,
            "editor_id": user_id,
            "clip_id": slug,
        }).get("data", [])
        selected = next((row for row in downloads if str(row.get("clip_id", "")) == slug), None)
        if not selected:
            raise FileNotFoundError("Twitch did not provide a downloadable file for this clip.")
        url = selected.get("landscape_download_url") or selected.get("portrait_download_url")
        if not url:
            raise FileNotFoundError("Twitch reports no downloadable rendition for this clip.")
        return _cdn_url(str(url)), {
            "clip_id": slug,
            "broadcaster_id": broadcaster_id,
            "broadcaster_name": str(clip.get("broadcaster_name") or ""),
            "title": str(clip.get("title") or "")[:300],
            "duration": float(clip.get("duration") or 0),
            "game": str(clip.get("game_id") or ""),
            "download_layout": "landscape" if selected.get("landscape_download_url") else "portrait",
        }

    def download(self, source_url: str, destination: Path) -> dict:
        download_url, metadata = self.get_download_url(source_url)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        partial.unlink(missing_ok=True)
        try:
            response = self.session.get(
                download_url,
                stream=True,
                timeout=(10.0, 60.0),
                allow_redirects=False,
            )
            # Follow at most three same-provider HTTPS redirects, validating every hop.
            for _ in range(3):
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("Location")
                if not location:
                    raise TwitchClipDownloadError("Twitch CDN issued an invalid redirect.")
                next_url = _cdn_url(requests.compat.urljoin(download_url, location))
                response.close()
                response = self.session.get(next_url, stream=True, timeout=(10.0, 60.0), allow_redirects=False)
                download_url = next_url
            if response.status_code >= 400 or response.status_code in {301, 302, 303, 307, 308}:
                raise TwitchClipDownloadError(f"Twitch CDN returned HTTP {response.status_code}.")
            content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
            if content_type and content_type not in {"video/mp4", "application/octet-stream", "binary/octet-stream"}:
                raise TwitchClipDownloadError(f"Twitch CDN returned unexpected content type: {content_type}.")
            declared = response.headers.get("Content-Length")
            if declared:
                try:
                    if int(declared) > self.max_bytes:
                        raise TwitchClipDownloadError("Twitch clip exceeds the configured download size limit.")
                except ValueError:
                    raise TwitchClipDownloadError("Twitch CDN returned an invalid content length.")
            total = 0
            with partial.open("xb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise TwitchClipDownloadError("Twitch clip exceeded the configured download size limit.")
                    handle.write(chunk)
            if total <= 0:
                raise TwitchClipDownloadError("Twitch returned an empty clip file.")
            partial.replace(destination)
            return {**metadata, "downloaded_bytes": total, "file_path": str(destination)}
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        finally:
            try:
                response.close()
            except Exception:
                pass
