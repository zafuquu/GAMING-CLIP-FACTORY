from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from app.integrations.twitch import TwitchHelix
from app.integrations.youtube import YouTubeNotConfigured, upload_video

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
