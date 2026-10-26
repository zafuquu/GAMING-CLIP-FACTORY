import json
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from app.vision import assess_candidate_images, VisionNotConfigured, VisionAnalysisError, SCHEMA

def _valid_result():
    return {
        "gameplay_context":"First-person shooter with a visible round HUD.",
        "event_type":"skill_play",
        "visible_event":"Player crosshair appears centered near an opponent.",
        "is_actionable_highlight":True,
        "highlight_score":0.73,
        "confidence":0.58,
        "evidence":["A player silhouette is visible near the crosshair."],
        "quality_issues":["The sampled frames do not establish the full event sequence."]
    }

def test_vision_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GCF_ENABLE_VISION_ANALYSIS",raising=False)
    with pytest.raises(VisionNotConfigured,match="disabled"):
        assess_candidate_images([b"frame"],"Valorant","match")

def test_vision_requires_loopback_endpoint_by_default(monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_VISION_ANALYSIS","true")
    monkeypatch.setenv("GCF_VISION_BASE_URL","https://remote.example/api/chat")
    monkeypatch.delenv("GCF_VISION_ALLOW_REMOTE",raising=False)
    with pytest.raises(VisionNotConfigured,match="HTTP"):
        assess_candidate_images([b"frame"],"Valorant","match")

def test_vision_rejects_remote_hosts_without_explicit_privacy_override(monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_VISION_ANALYSIS","true")
    monkeypatch.setenv("GCF_VISION_BASE_URL","http://remote.example:11434/api/chat")
    monkeypatch.delenv("GCF_VISION_ALLOW_REMOTE",raising=False)
    with pytest.raises(VisionNotConfigured,match="Remote"):
        assess_candidate_images([b"frame"],"Valorant","match")

def test_vision_returns_validated_evidence_and_uses_structured_output(monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_VISION_ANALYSIS","true")
    monkeypatch.setenv("GCF_VISION_BASE_URL","http://127.0.0.1:11434/api/chat")
    captured={}
    def fake_post(url,**kwargs):
        captured["url"]=url
        captured["body"]=kwargs["json"]
        captured["timeout"]=kwargs["timeout"]
        class Response:
            def raise_for_status(self): pass
            def json(self): return {"message":{"content":json.dumps(_valid_result())}}
        return Response()
    result=assess_candidate_images([b"frame1",b"frame2"],"Valorant","ranked match","Nice shot!",http_post=fake_post)
    assert result["event_type"]=="skill_play"
    assert result["score_calibrated"] is False
    assert result["model_name"]=="llama3.2-vision"
    assert len(captured["body"]["messages"][1]["images"])==2
    assert captured["body"]["format"]==SCHEMA
    assert captured["timeout"][0]==5

def test_vision_fails_closed_on_invalid_model_score(monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_VISION_ANALYSIS","true")
    monkeypatch.setenv("GCF_VISION_BASE_URL","http://127.0.0.1:11434/api/chat")
    body=_valid_result();body["highlight_score"]=9.5
    def fake_post(url,**kwargs):
        class Response:
            def raise_for_status(self): pass
            def json(self): return {"message":{"content":json.dumps(body)}}
        return Response()
    with pytest.raises(VisionAnalysisError,match="highlight_score"):
        assess_candidate_images([b"frame"],"Valorant","match",http_post=fake_post)
