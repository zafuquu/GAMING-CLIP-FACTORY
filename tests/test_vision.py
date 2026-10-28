import json
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from app.vision import assess_candidate_images, VisionNotConfigured, VisionAnalysisError, SCHEMA, vision_sampling_fractions, vision_status

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
    result=assess_candidate_images([b"frame1",b"frame2"],"Valorant","ranked match","Nice shot!",http_post=fake_post,frame_times_seconds=[0.3,1.7])
    assert result["event_type"]=="skill_play"
    assert result["score_calibrated"] is False
    assert result["model_name"]=="qwen3-vl:2b"
    assert len(captured["body"]["messages"][1]["images"])==2
    assert captured["body"]["format"]==SCHEMA
    assert "image 1 ~= 0.300s after candidate start" in captured["body"]["messages"][1]["content"]
    assert "image 2 ~= 1.700s after candidate start" in captured["body"]["messages"][1]["content"]
    assert result["sample_frame_times_seconds"]==[0.3,1.7]
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


def test_vision_sampling_defaults_to_three_frames_and_allows_five(monkeypatch):
    monkeypatch.delenv("GCF_VISION_SAMPLE_FRAMES",raising=False)
    assert vision_sampling_fractions()==[0.2,0.5,0.8]
    monkeypatch.setenv("GCF_VISION_SAMPLE_FRAMES","5")
    assert vision_sampling_fractions()==[0.1,0.3,0.5,0.7,0.9]
    monkeypatch.setenv("GCF_VISION_SAMPLE_FRAMES","4")
    assert vision_sampling_fractions()==[0.2,0.5,0.8]


def test_vision_defaults_are_lightweight_and_bounded(monkeypatch):
    for key in ("GCF_VISION_MODEL","GCF_VISION_MAX_CANDIDATES_PER_MEDIA","GCF_VISION_SAMPLE_FRAMES"):
        monkeypatch.delenv(key,raising=False)
    status=vision_status()
    assert status["model"]=="qwen3-vl:2b"
    assert status["max_candidates_per_media"]==8
    assert status["sample_frames"]==3

def test_event_centered_sampling_keeps_context_and_focus_frames(monkeypatch):
    monkeypatch.setenv("GCF_VISION_SAMPLE_FRAMES","5")
    assert vision_sampling_fractions(0.5)==[0.1,0.42,0.5,0.58,0.9]
    assert vision_sampling_fractions(0.05)==[0.05,0.1,0.18,0.55,0.9]
    assert vision_sampling_fractions(0.95)==[0.1,0.45,0.82,0.9,0.95]

    monkeypatch.setenv("GCF_VISION_SAMPLE_FRAMES","3")
    assert vision_sampling_fractions(0.5)==[0.38,0.5,0.62]
    assert vision_sampling_fractions(0.05)==[0.05,0.13,0.23]
    assert vision_sampling_fractions(0.95)==[0.77,0.87,0.95]

    # Invalid/legacy event offsets preserve lightweight uniform sampling.
    assert vision_sampling_fractions(float("nan"))==[0.2,0.5,0.8]
    assert vision_sampling_fractions(True)==[0.2,0.5,0.8]


def test_vision_rejects_timestamps_that_do_not_match_frames(monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_VISION_ANALYSIS","true")
    monkeypatch.setenv("GCF_VISION_BASE_URL","http://127.0.0.1:11434/api/chat")
    with pytest.raises(ValueError,match="match the number"):
        assess_candidate_images([b"frame1",b"frame2"],"Valorant","match",frame_times_seconds=[0.5])
    with pytest.raises(ValueError,match="finite, nonnegative"):
        assess_candidate_images([b"frame1"],"Valorant","match",frame_times_seconds=[float("nan")])
