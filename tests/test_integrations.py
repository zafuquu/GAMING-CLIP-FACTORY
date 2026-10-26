from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from app.integrations.twitch import TwitchHelix
from app.integrations.youtube import YouTubeNotConfigured, upload_video
from app.content_director import plan_candidate

def test_twitch_metadata_discovery_never_assigns_reuse_rights(monkeypatch):
    client=TwitchHelix(client_id="test-client",client_secret="test-secret")
    def fake_get(endpoint,params):
        if endpoint=="search/categories":
            return {"data":[{"id":"123","name":"Valorant"}]}
        if endpoint=="clips":
            return {"data":[{"id":"clip-1","url":"https://www.twitch.tv/example/clip/abc","title":"Amazing moment","broadcaster_name":"Example","broadcaster_id":"99","view_count":42,"duration":18.5,"thumbnail_url":"https://example.invalid/thumb.jpg","created_at":"2026-10-25T00:00:00Z"}]}
        raise AssertionError(endpoint)
    monkeypatch.setattr(client,"_get",fake_get)
    clips=client.discover_game_clips("Valorant",days=7,max_results=100)
    assert len(clips)==1
    assert clips[0]["game"]=="Valorant"
    assert clips[0]["provider_clip_id"]=="clip-1"
    assert clips[0]["permission_status"]=="UNKNOWN"
    assert clips[0]["provider"]=="twitch_helix"

def test_twitch_empty_category_returns_no_results(monkeypatch):
    client=TwitchHelix(client_id="test-client",client_secret="test-secret")
    monkeypatch.setattr(client,"_get",lambda endpoint,params: {"data":[]} )
    assert client.discover_game_clips("No Such Game")==[]

def test_scheduled_public_upload_refuses_without_explicit_gate(tmp_path,monkeypatch):
    monkeypatch.delenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING",raising=False)
    from datetime import datetime, timedelta, timezone
    future=datetime.now(timezone.utc)+timedelta(days=1)
    with pytest.raises(YouTubeNotConfigured,match="disabled"):
        upload_video(tmp_path/"not-needed-before-gate.mp4","Title","",[],publish_at=future,allow_scheduled_public=False)

def test_twitch_accepts_clips_twitch_tv_urls(monkeypatch):
    client=TwitchHelix(client_id="test-client",client_secret="test-secret")
    def fake_get(endpoint,params):
        if endpoint=="search/categories": return {"data":[{"id":"123","name":"Minecraft"}]}
        return {"data":[{"id":"clip-2","url":"https://clips.twitch.tv/ExampleSlug","title":"Test","broadcaster_name":"Creator","broadcaster_id":"1","view_count":1,"duration":7.0}]}
    monkeypatch.setattr(client,"_get",fake_get)
    clips=client.discover_game_clips("Minecraft")
    assert clips[0]["url"]=="https://clips.twitch.tv/ExampleSlug"
    assert clips[0]["permission_status"]=="UNKNOWN"

def test_content_plan_is_transcript_grounded_and_non_claiming():
    transcript=[{"start_seconds":4.1,"end_seconds":5.3,"text":"Look at the angle","confidence":0.91}]
    plan=plan_candidate("Valorant","Ranked match",3.0,14.0,transcript)
    assert plan["chosen_format"]=="educational_breakdown"
    assert "Look at the angle" in plan["hook_text"]
    assert "Valorant" in plan["title"]
    assert plan["evidence"]["transcript_excerpt"]=="Look at the angle"
    assert plan["narration_text"]==""
    assert plan["ranking_components"]["transcript_available"] is True

def test_content_plan_without_transcript_avoids_invented_event_claims():
    plan=plan_candidate("Valorant","Ranked match",1.0,9.0,[])
    assert plan["chosen_format"]=="gameplay_highlight"
    assert plan["hook_text"]==""
    assert "Gameplay Highlight" in plan["title"]
    assert plan["evidence"]["transcript_excerpt"]==""

def test_youtube_public_search_returns_metadata_without_rights(monkeypatch):
    from types import SimpleNamespace
    import app.integrations.youtube as yt
    monkeypatch.setenv("GCF_YOUTUBE_API_KEY","test-api-key")
    expected={"items":[{"id":{"videoId":"video123"},"snippet":{"title":"A gameplay video","channelTitle":"Creator","channelId":"channel456","publishedAt":"2026-10-25T00:00:00Z","description":"Public metadata","thumbnails":{"medium":{"url":"https://example.invalid/thumb.jpg"}}}}]}
    captured={}
    class Request:
        def execute(self): return expected
    class Search:
        def list(self,**kwargs):
            captured.update(kwargs)
            return Request()
    class Service:
        def search(self): return Search()
    monkeypatch.setattr(yt,"build",lambda *a,**kw: Service())
    found=yt.search_public_videos("Valorant",days=7,max_results=50)
    assert len(found)==1
    assert found[0]["url"]=="https://www.youtube.com/watch?v=video123"
    assert found[0]["permission_status"]=="UNKNOWN"
    assert found[0]["provider"]=="youtube_data_api"
    assert captured["type"]=="video" and captured["q"]=="Valorant"
    assert captured["maxResults"]==50

def test_youtube_discovery_availability_never_needs_channel_oauth_for_api_key(monkeypatch):
    import app.integrations.youtube as yt
    monkeypatch.setenv("GCF_YOUTUBE_API_KEY","local-test-key")
    monkeypatch.setattr(yt.keyring,"get_password",lambda *args: None)
    assert yt.youtube_discovery_available() is True

def test_private_upload_uses_private_default_without_guessing_made_for_kids(tmp_path,monkeypatch):
    import app.integrations.youtube as yt
    video=tmp_path/"authorized-render.mp4"
    video.write_bytes(b"test video bytes")
    captured={}
    class UploadRequest:
        def next_chunk(self):
            return None,{"id":"uploaded-video-123"}
    class Videos:
        def insert(self,**kwargs):
            captured.update(kwargs)
            return UploadRequest()
    class Service:
        def videos(self): return Videos()
    monkeypatch.setattr(yt,"youtube_service",lambda:Service())
    result=yt.upload_video(video,"Test title","Test description",["gaming","test"])
    assert result["video_id"]=="uploaded-video-123"
    assert result["privacy_status"]=="private"
    assert captured["body"]["status"]["privacyStatus"]=="private"
    assert "selfDeclaredMadeForKids" not in captured["body"]["status"]
    assert captured["part"]=="snippet,status"
    assert captured["media_body"].mimetype=="video/mp4"

def test_youtube_oauth_scopes_include_private_upload_and_video_update_permissions():
    from app.integrations.youtube import SCOPES
    assert "https://www.googleapis.com/auth/youtube.upload" in SCOPES
    assert "https://www.googleapis.com/auth/youtube.force-ssl" in SCOPES
    assert "https://www.googleapis.com/auth/yt-analytics.readonly" in SCOPES
