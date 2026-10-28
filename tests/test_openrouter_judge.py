import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.openrouter_judge import (
    AIJudgeError,
    AIJudgeNotConfigured,
    assess_candidate_batch,
    ai_judge_status,
    sampling_fractions,
)


def _candidate(key: str) -> dict:
    return {
        "candidate_key": key,
        "images": [b"jpeg-frame-1", b"jpeg-frame-2", b"jpeg-frame-3"],
        "game": "Valorant",
        "source_title": "Local gameplay fixture",
        "start_seconds": 20.0,
        "end_seconds": 50.0,
        "event_time_seconds": 15.0,
        "signal_score": 0.72,
        "transcript_excerpt": "",
        "frame_times_seconds": [8.0, 15.0, 22.0],
    }


def _item(key: str, **overrides) -> dict:
    value = {
        "candidate_key": key,
        "gameplay_context": "First-person shooter gameplay.",
        "event_type": "skill_play",
        "visible_event": "The player appears to engage an opponent during active gameplay.",
        "is_actionable_highlight": True,
        "highlight_score": 0.81,
        "confidence": 0.77,
        "evidence": ["Frames show active first-person gameplay around the event."],
        "quality_issues": [],
    }
    value.update(overrides)
    return value


def test_openrouter_free_router_is_the_default_and_does_not_need_local_model(monkeypatch):
    monkeypatch.delenv("GCF_OPENROUTER_MODEL", raising=False)
    monkeypatch.setenv("GCF_ENABLE_AI_JUDGING", "true")
    monkeypatch.delenv("GCF_OPENROUTER_API_KEY", raising=False)
    status = ai_judge_status()
    assert status["provider"] == "OpenRouter"
    assert status["model"] == "openrouter/free"
    assert status["random_free_model_router"] is True
    assert status["ready"] is False
    assert status["sample_frames"] == 3
    assert status["batch_size"] == 4
    with pytest.raises(AIJudgeNotConfigured, match="API KEY"):
        assess_candidate_batch([_candidate("C1")])


def test_candidate_sampling_centers_on_detected_event_and_keeps_context(monkeypatch):
    monkeypatch.delenv("GCF_AI_JUDGE_SAMPLE_FRAMES", raising=False)
    assert sampling_fractions() == [0.2, 0.5, 0.8]
    assert sampling_fractions(0.5) == [0.34, 0.5, 0.66]
    assert sampling_fractions(0.05) == [0.05, 0.15, 0.3]
    monkeypatch.setenv("GCF_AI_JUDGE_SAMPLE_FRAMES", "4")
    assert len(sampling_fractions(0.5)) == 4


def test_one_openrouter_request_judges_multiple_candidates_and_records_actual_model(monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_AI_JUDGING", "true")
    monkeypatch.setenv("GCF_OPENROUTER_API_KEY", "test-api-key")
    monkeypatch.delenv("GCF_OPENROUTER_MODEL", raising=False)
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["body"] = kwargs["json"]
        captured["timeout"] = kwargs["timeout"]

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "model": "test-provider/free-vision-model",
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "candidates": [
                                    _item("C1"),
                                    _item("C2", event_type="funny_moment", highlight_score=0.62),
                                ]
                            })
                        }
                    }],
                }

        return Response()

    results = assess_candidate_batch(
        [_candidate("C1"), _candidate("C2")],
        http_post=fake_post,
    )
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-api-key"
    assert captured["body"]["model"] == "openrouter/free"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["temperature"] == 0
    content = captured["body"]["messages"][1]["content"]
    assert len([part for part in content if part.get("type") == "image_url"]) == 6
    assert results[0]["model_name"] == "test-provider/free-vision-model"
    assert results[0]["configured_model"] == "openrouter/free"
    assert results[0]["assessment_method"] == "openrouter_multimodal_highlight_judge"
    assert results[0]["score_calibrated"] is False
    assert results[1]["event_type"] == "funny_moment"


def test_openrouter_rejects_incomplete_or_invalid_candidate_judgment(monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_AI_JUDGING", "true")
    monkeypatch.setenv("GCF_OPENROUTER_API_KEY", "test-api-key")

    def fake_post(url, **kwargs):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "model": "test/free",
                    "choices": [{"message": {"content": json.dumps({
                        "candidates": [_item("C1", highlight_score=2.0)]
                    })}}],
                }
        return Response()

    with pytest.raises(AIJudgeError, match="highlight_score"):
        assess_candidate_batch([_candidate("C1")], http_post=fake_post)

    with pytest.raises(ValueError, match="unique"):
        assess_candidate_batch([_candidate("C1"), _candidate("C1")])


def test_openrouter_non_success_response_is_not_misreported_as_a_highlight(monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_AI_JUDGING", "true")
    monkeypatch.setenv("GCF_OPENROUTER_API_KEY", "test-api-key")

    import requests

    def failed_post(url, **kwargs):
        response = requests.Response()
        response.status_code = 429
        raise requests.HTTPError("rate limited", response=response)

    with pytest.raises(AIJudgeError, match="free-model limits"):
        assess_candidate_batch([_candidate("C1")], http_post=failed_post)
