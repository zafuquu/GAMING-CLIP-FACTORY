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
    assert captured["media_body"].mimetype()=="video/mp4"

def test_youtube_oauth_scopes_include_private_upload_and_video_update_permissions():
    from app.integrations.youtube import SCOPES
    assert "https://www.googleapis.com/auth/youtube.upload" in SCOPES
    assert "https://www.googleapis.com/auth/youtube.force-ssl" in SCOPES
    assert "https://www.googleapis.com/auth/yt-analytics.readonly" in SCOPES

def test_legacy_youtube_oauth_requires_reconnect_after_scope_change(monkeypatch):
    import app.integrations.youtube as yt
    def fake_get(service,key):
        if key==yt.CREDENTIALS_KEY: return '{"token":"legacy-token"}'
        if key==yt.CREDENTIALS_SCOPE_VERSION_KEY: return None
        return None
    monkeypatch.setattr(yt.keyring,"get_password",fake_get)
    with pytest.raises(yt.YouTubeNotConfigured,match="predates the current"):
        yt.load_credentials()


def test_custom_shorts_thumbnail_upload_uses_official_thumbnail_set(monkeypatch,tmp_path):
    import app.integrations.youtube as yt
    cover=tmp_path/"vertical-cover.jpg"
    cover.write_bytes(bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300") + b"fixture" + bytes.fromhex("ffd9"))
    captured={}
    class Upload:
        def mimetype(self): return "image/jpeg"
        def next_chunk(self): return None,{"items":[{"default":{"url":"https://example.invalid/thumb.jpg"}}]}
    class Thumbnails:
        def set(self,**kwargs):
            captured.update(kwargs)
            return Upload()
    class Service:
        def thumbnails(self): return Thumbnails()
    monkeypatch.setattr(yt,"youtube_service",lambda:Service())
    result=yt.set_custom_thumbnail("youtube-video-123",cover)
    assert result["uploaded"] is True
    assert result["video_id"]=="youtube-video-123"
    assert captured["videoId"]=="youtube-video-123"
    assert captured["media_body"].mimetype()=="image/jpeg"
    with pytest.raises(ValueError,match="ID"):
        yt.set_custom_thumbnail("not a valid ID",cover)

def test_custom_thumbnail_upload_blocks_non_jpeg(tmp_path):
    from app.integrations.youtube import set_custom_thumbnail
    image=tmp_path/"cover.png"
    image.write_bytes(b"not a jpeg")
    with pytest.raises(ValueError,match="JPEG"):
        set_custom_thumbnail("youtube-video-123",image)

def test_custom_thumbnail_art_is_vertical_title_led_and_hash_verified(tmp_path):
    from PIL import Image
    from app.thumbnail_art import generate_shorts_thumbnail
    poster=tmp_path/"poster.jpg"
    Image.new("RGB",(360,640),(45,90,130)).save(poster,"JPEG",quality=80)
    output=tmp_path/"cover.jpg"
    result=generate_shorts_thumbnail(poster,"A clean round wins","Valorant",output)
    assert output.is_file() and output.stat().st_size==result["file_size"]
    assert result["width"] in {1080,2160}
    assert result["height"] in {1920,3840}
    assert result["height"]==result["width"]*16//9
    assert result["mime_type"]=="image/jpeg"
    assert result["file_size"]<=result["max_bytes"]
    import hashlib
    assert hashlib.sha256(output.read_bytes()).hexdigest()==result["sha256"]


def test_local_narration_draft_is_evidence_linked_and_unreviewed(monkeypatch):
    import json
    import app.script_writer as writer
    monkeypatch.setenv("GCF_ENABLE_SCRIPT_DRAFTING","true")
    monkeypatch.setenv("GCF_SCRIPT_DRAFTER_BASE_URL","http://127.0.0.1:11434/api/chat")
    monkeypatch.setenv("GCF_SCRIPT_DRAFTER_MODEL","test-local-model")
    result={
        "script":"Watch this angle, then reset.",
        "hook":"Watch this angle.",
        "evidence_used":[{"ref":"transcript:4","quote":"Watch this angle"}],
        "unsupported_claims":[],
    }
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"message":{"content":json.dumps(result)}}
    captured={}
    def fake_post(url,**kwargs):
        captured["url"]=url
        captured["body"]=kwargs["json"]
        return Response()
    generated=writer.generate_narration_draft(
        game="Valorant",
        source_title="Ranked match",
        candidate_duration=13,
        transcript_segments=[{"segment_index":4,"start_seconds":2.1,"end_seconds":3.4,"text":"Watch this angle","confidence":0.9}],
        http_post=fake_post,
    )
    assert generated["script"]==result["script"]
    assert generated["status"]=="GENERATED_UNREVIEWED"
    assert generated["owner_review_required"] is True
    assert generated["automatic_synthesis"] is False
    assert generated["evidence_used"]==[{"ref":"transcript:4","quote":"Watch this angle"}]
    assert captured["body"]["stream"] is False
    assert captured["body"]["options"]["temperature"]==0

def test_local_narration_draft_rejects_model_invented_evidence_refs(monkeypatch):
    import json
    import app.script_writer as writer
    from app.script_writer import ScriptDraftError
    monkeypatch.setenv("GCF_ENABLE_SCRIPT_DRAFTING","true")
    monkeypatch.setenv("GCF_SCRIPT_DRAFTER_BASE_URL","http://127.0.0.1:11434/api/chat")
    payload={"script":"A guaranteed ace!","hook":"A guaranteed ace",
             "evidence_used":[{"ref":"transcript:99","quote":"ace"}],"unsupported_claims":[]}
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"message":{"content":json.dumps(payload)}}
    with pytest.raises(ScriptDraftError,match="not present"):
        writer.generate_narration_draft(
            game="Valorant",source_title="Match",candidate_duration=10,
            transcript_segments=[{"segment_index":1,"start_seconds":1,"end_seconds":2,"text":"Nice angle"}],
            http_post=lambda *args,**kwargs:Response(),
        )

def test_local_narration_draft_is_disabled_by_default(monkeypatch):
    import app.script_writer as writer
    from app.script_writer import ScriptDraftNotConfigured
    monkeypatch.delenv("GCF_ENABLE_SCRIPT_DRAFTING",raising=False)
    with pytest.raises(ScriptDraftNotConfigured,match="disabled"):
        writer.generate_narration_draft(
            game="Valorant",source_title="Match",candidate_duration=10,
            transcript_segments=[{"segment_index":1,"start_seconds":1,"end_seconds":2,"text":"Nice angle"}],
        )
