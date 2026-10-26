"""Twitch Helix metadata discovery. This module discovers URLs only; it never downloads media or grants reuse rights."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import os
import requests

class ProviderNotConfigured(RuntimeError):
    pass

class TwitchAPIError(RuntimeError):
    pass

class TwitchHelix:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None, timeout: tuple[float,float]=(5.0,20.0)):
        self.client_id = client_id or os.getenv("GCF_TWITCH_CLIENT_ID", "").strip()
        self.client_secret = client_secret or os.getenv("GCF_TWITCH_CLIENT_SECRET", "").strip()
        self.timeout = timeout
        self._token: str | None = None
        self._session = requests.Session()
        if not self.client_id or not self.client_secret:
            raise ProviderNotConfigured("Twitch discovery requires GCF_TWITCH_CLIENT_ID and GCF_TWITCH_CLIENT_SECRET.")

    def _access_token(self) -> str:
        if self._token:
            return self._token
        try:
            response = self._session.post(
                "https://id.twitch.tv/oauth2/token",
                data={"client_id":self.client_id,"client_secret":self.client_secret,"grant_type":"client_credentials"},
                timeout=self.timeout,
            )
            if response.status_code >= 400:
                raise TwitchAPIError(f"Twitch OAuth failed with HTTP {response.status_code}; check app credentials and developer-console status.")
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise TwitchAPIError("Twitch OAuth response did not include an access token.")
            self._token = token
            return token
        except requests.RequestException as exc:
            raise TwitchAPIError(f"Twitch OAuth request failed: {type(exc).__name__}") from exc

    def _get(self, endpoint: str, params: dict) -> dict:
        headers = {"Client-Id": self.client_id, "Authorization": "Bearer " + self._access_token()}
        for attempt in range(3):
            try:
                response = self._session.get("https://api.twitch.tv/helix/" + endpoint, headers=headers, params=params, timeout=self.timeout)
                if response.status_code == 401 and attempt == 0:
                    self._token = None
                    headers["Authorization"] = "Bearer " + self._access_token()
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        import time
                        retry_after = response.headers.get("Retry-After", "")
                        try: delay = min(8.0, max(0.5, float(retry_after)))
                        except ValueError: delay = 2 ** attempt
                        time.sleep(delay)
                        continue
                if response.status_code >= 400:
                    raise TwitchAPIError(f"Twitch Helix request {endpoint} failed with HTTP {response.status_code}.")
                body = response.json()
                if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                    raise TwitchAPIError(f"Twitch Helix {endpoint} returned an unexpected response shape.")
                return body
            except requests.RequestException as exc:
                if attempt == 2:
                    raise TwitchAPIError(f"Twitch Helix request {endpoint} failed: {type(exc).__name__}") from exc
                import time
                time.sleep(2 ** attempt)
        raise TwitchAPIError(f"Twitch Helix request {endpoint} exhausted retries.")

    def discover_game_clips(self, game_query: str, days: int = 7, max_results: int = 100) -> list[dict]:
        query = game_query.strip()
        if not query:
            return []
        categories = self._get("search/categories", {"query":query,"first":10}).get("data", [])
        if not categories:
            return []
        normalized = query.casefold()
        exact = next((x for x in categories if str(x.get("name","")).casefold() == normalized), categories[0])
        game_id = str(exact.get("id", ""))
        if not game_id:
            return []
        end = datetime.now(timezone.utc).replace(microsecond=0)
        start = end - timedelta(days=max(1,min(int(days),30)))
        first = min(100, max(1, int(max_results)))
        payload = self._get("clips", {
            "game_id": game_id,
            "started_at": start.isoformat().replace("+00:00","Z"),
            "ended_at": end.isoformat().replace("+00:00","Z"),
            "first": first,
        })
        output = []
        for clip in payload.get("data", []):
            url = str(clip.get("url", "")).strip()
            if not (url.startswith("https://www.twitch.tv/") or url.startswith("https://clips.twitch.tv/")):
                continue
            output.append({
                "url":url,
                "title":str(clip.get("title",""))[:300],
                "creator":str(clip.get("broadcaster_name",""))[:200],
                "creator_id":str(clip.get("broadcaster_id","")),
                "game":str(exact.get("name",query))[:120],
                "game_id":game_id,
                "provider_clip_id":str(clip.get("id","")),
                "created_at":clip.get("created_at"),
                "view_count":int(clip.get("view_count",0) or 0),
                "duration":float(clip.get("duration",0) or 0),
                "thumbnail_url":str(clip.get("thumbnail_url","")),
                "provider":"twitch_helix",
                "permission_status":"UNKNOWN",
            })
        return output
