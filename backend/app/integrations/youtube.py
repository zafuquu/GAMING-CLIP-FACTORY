"""YouTube Data/Analytics API adapters. Credentials live in the OS keyring, not this repository."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json, os, time
import httplib2
from google_auth_httplib2 import AuthorizedHttp
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import keyring

SERVICE_NAME = "gaming-clip-factory-ai"
CREDENTIALS_KEY = "youtube-oauth-credentials"
CREDENTIALS_SCOPE_VERSION_KEY = "youtube-oauth-scope-version"
CREDENTIALS_SCOPE_VERSION = "2"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
class YouTubeNotConfigured(RuntimeError): pass
class UploadReconciliationRequired(RuntimeError): pass

def load_credentials():
    raw = keyring.get_password(SERVICE_NAME, CREDENTIALS_KEY)
    if not raw:
        raise YouTubeNotConfigured("YouTube is not connected. Configure GCF_YOUTUBE_CLIENT_SECRETS and run the local OAuth command.")
    scope_version=keyring.get_password(SERVICE_NAME, CREDENTIALS_SCOPE_VERSION_KEY)
    if scope_version!=CREDENTIALS_SCOPE_VERSION:
        raise YouTubeNotConfigured("Stored YouTube authorization predates the current upload/processing scopes. Reconnect the channel by running the local OAuth command again.")
    try:
        data = json.loads(raw)
        credentials = Credentials.from_authorized_user_info(data, scopes=SCOPES)
    except Exception as exc:
        raise YouTubeNotConfigured("Stored YouTube credentials are unreadable; reconnect through the local OAuth command.") from exc
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            keyring.set_password(SERVICE_NAME, CREDENTIALS_KEY, credentials.to_json())
        except Exception as exc:
            raise YouTubeNotConfigured("YouTube token refresh failed; reconnect the channel in the local OAuth command.") from exc
    if not credentials.valid:
        raise YouTubeNotConfigured("YouTube authorization is no longer valid; reconnect the channel.")
    return credentials

def _api_timeout() -> float:
    try:
        return float(max(5, min(120, int(os.getenv("GCF_GOOGLE_API_TIMEOUT_SECONDS", "30")))))
    except (TypeError, ValueError):
        return 30.0

def youtube_service():
    credentials=load_credentials()
    http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=_api_timeout()))
    return build("youtube", "v3", http=http, cache_discovery=False)

def analytics_service():
    credentials=load_credentials()
    http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=_api_timeout()))
    return build("youtubeAnalytics", "v2", http=http, cache_discovery=False)

def connected_status():
    try:
        service = youtube_service()
        response = service.channels().list(part="snippet", mine=True, maxResults=1).execute()
        items = response.get("items", [])
        return {"configured":True,"connected":bool(items),"channel_title":items[0]["snippet"]["title"] if items else None,"reason":None if items else "OAuth returned no accessible YouTube channel."}
    except YouTubeNotConfigured as exc:
        return {"configured":False,"connected":False,"channel_title":None,"reason":str(exc)}
    except Exception as exc:
        return {"configured":True,"connected":False,"channel_title":None,"reason":f"YouTube connection check failed: {type(exc).__name__}"}

def upload_video(path: Path, title: str, description: str, tags: list[str], publish_at: datetime | None = None, allow_scheduled_public: bool = False) -> dict:
    if publish_at is not None:
        if not allow_scheduled_public:
            raise YouTubeNotConfigured("Scheduled public publishing is disabled. Set GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING=true only after supervised testing.")
        if publish_at.tzinfo is None or publish_at <= datetime.now(timezone.utc):
            raise ValueError("Scheduled publish time must be timezone-aware and in the future.")
    if not path.is_file() or path.stat().st_size <= 0:
        raise FileNotFoundError("Rendered output is missing or empty.")
    body = {
        "snippet":{"title":title[:100],"description":description[:5000],"tags":[x[:100] for x in tags[:30]],"categoryId":"20"},
        "status":{"privacyStatus":"private"},
    }
    if publish_at is not None:
        body["status"]["publishAt"] = publish_at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
    service = youtube_service()
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=8*1024*1024)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    try:
        result = None
        try: total_timeout=max(60,min(21600,int(os.getenv("GCF_YOUTUBE_UPLOAD_TIMEOUT_SECONDS","7200"))))
        except (TypeError,ValueError): total_timeout=7200
        deadline=time.monotonic()+total_timeout
        while result is None:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"YouTube upload exceeded configured total timeout of {total_timeout} seconds.")
            _status, result = request.next_chunk()
        video_id = result.get("id") if isinstance(result, dict) else None
        if not video_id:
            raise UploadReconciliationRequired("YouTube upload returned no video ID; do not retry until reconciled.")
        return {"video_id":video_id,"privacy_status":"private","publish_at":body["status"].get("publishAt"),"url":f"https://www.youtube.com/watch?v={video_id}"}
    except UploadReconciliationRequired:
        raise
    except Exception as exc:
        raise UploadReconciliationRequired(f"Upload outcome is uncertain ({type(exc).__name__}). Do not retry automatically; reconcile it first.") from exc

def fetch_video_analytics(start_date: str, end_date: str, max_results: int = 200) -> list[dict]:
    service = analytics_service()
    response = service.reports().query(
        ids="channel==MINE",
        startDate=start_date,
        endDate=end_date,
        metrics="views,likes,comments,shares,subscribersGained,subscribersLost,estimatedMinutesWatched,averageViewDuration,averageViewPercentage",
        dimensions="video",
        sort="-views",
        maxResults=min(200,max(1,int(max_results))),
    ).execute()
    headers = [x.get("name") for x in response.get("columnHeaders",[])]
    output=[]
    for row in response.get("rows",[]) or []:
        item=dict(zip(headers,row))
        if item.get("video"):
            output.append(item)
    return output

def youtube_discovery_available() -> bool:
    if os.getenv("GCF_YOUTUBE_API_KEY", "").strip():
        return True
    try:
        return bool(keyring.get_password(SERVICE_NAME, CREDENTIALS_KEY))
    except Exception:
        return False

def youtube_oauth_available() -> bool:
    """True only when a local YouTube token is present and marked as having current required scopes."""
    try:
        return bool(
            keyring.get_password(SERVICE_NAME, CREDENTIALS_KEY)
            and keyring.get_password(SERVICE_NAME, CREDENTIALS_SCOPE_VERSION_KEY) == CREDENTIALS_SCOPE_VERSION
        )
    except Exception:
        return False

def search_public_videos(query: str, days: int = 7, max_results: int = 50) -> list[dict]:
    """Search public video metadata only; results always remain UNKNOWN for reuse rights."""
    query=query.strip()
    if not query:
        return []
    api_key=os.getenv("GCF_YOUTUBE_API_KEY","").strip()
    if api_key:
        service=build("youtube","v3",developerKey=api_key,http=httplib2.Http(timeout=_api_timeout()),cache_discovery=False)
    else:
        service=youtube_service()
    published_after=(datetime.now(timezone.utc)-timedelta(days=max(1,min(int(days),30)))).isoformat(timespec="seconds").replace("+00:00","Z")
    response=service.search().list(
        part="snippet",
        q=query,
        type="video",
        order="viewCount",
        publishedAfter=published_after,
        maxResults=min(50,max(1,int(max_results))),
    ).execute()
    output=[]
    for item in response.get("items",[]) or []:
        video_id=str((item.get("id") or {}).get("videoId",""))
        snippet=item.get("snippet") or {}
        if not video_id:
            continue
        output.append({
            "url":"https://www.youtube.com/watch?v="+video_id,
            "title":str(snippet.get("title",""))[:300],
            "creator":str(snippet.get("channelTitle",""))[:200],
            "creator_id":str(snippet.get("channelId","")),
            "game":query[:120],
            "provider_video_id":video_id,
            "published_at":snippet.get("publishedAt"),
            "description":str(snippet.get("description",""))[:1000],
            "thumbnail_url":str(((snippet.get("thumbnails") or {}).get("medium") or {}).get("url","")),
            "provider":"youtube_data_api",
            "permission_status":"UNKNOWN",
        })
    return output
