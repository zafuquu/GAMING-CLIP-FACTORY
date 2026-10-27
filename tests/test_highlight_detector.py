import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.highlight_detector import audio_activity_scores, parse_timed_metric, rank_highlight_windows


def test_parse_timed_metric_pairs_ffmpeg_metadata_with_current_frame_time():
    log = """
[Parsed_ametadata_3 @ 0x1] frame:0 pts:0 pts_time:0
[Parsed_ametadata_3 @ 0x1] lavfi.astats.Overall.RMS_level=-80.000000
[Parsed_ametadata_3 @ 0x1] frame:1 pts:48000 pts_time:1
[Parsed_ametadata_3 @ 0x1] lavfi.astats.Overall.RMS_level=-78.000000
[Parsed_ametadata_3 @ 0x1] frame:2 pts:96000 pts_time:2
[Parsed_ametadata_3 @ 0x1] lavfi.astats.Overall.RMS_level=-12.000000
"""
    assert parse_timed_metric(log, "lavfi.astats.Overall.RMS_level") == [
        (0.0, -80.0), (1.0, -78.0), (2.0, -12.0)
    ]


def test_audio_activity_prioritizes_a_relative_loudness_spike():
    samples = [(float(t), -20.0) for t in range(60)]
    samples[30] = (30.0, -4.0)
    scores = dict(audio_activity_scores(samples))
    assert scores[30.0] == 1.0
    assert scores[10.0] == 0.0


def test_ranker_creates_ranked_short_window_around_audio_peak():
    samples = [(float(t), -20.0) for t in range(60)]
    samples[28] = (28.0, -5.0)
    samples[29] = (29.0, -3.0)
    samples[30] = (30.0, -4.0)
    candidates = rank_highlight_windows(60.0, [], samples, target_seconds=24.0)
    assert candidates
    top = candidates[0]
    assert top["start_seconds"] <= 29.0 <= top["end_seconds"]
    assert 20.0 <= top["end_seconds"] - top["start_seconds"] <= 24.0
    assert top["ranking_score"] > 0.7
    assert top["ranking_components"]["score_calibrated"] is False
    assert "virality" in top["ranking_components"]["score_meaning"]


def test_scene_only_fallback_is_low_confidence_and_stays_review_only():
    candidates = rank_highlight_windows(90.0, [25.0, 27.0, 68.0], [], max_candidates=10)
    assert candidates
    assert len(candidates) == 2
    assert all(item["event_type"] == "scene_change_only" for item in candidates)
    assert all(item["ranking_score"] <= 0.5 for item in candidates)
    assert all(item["ranking_components"]["score_calibrated"] is False for item in candidates)


def test_short_source_is_bounded_and_has_no_negative_timestamps():
    candidates = rank_highlight_windows(5.0, [1.0], [], target_seconds=24.0)
    assert len(candidates) == 1
    assert candidates[0]["start_seconds"] == 0.0
    assert candidates[0]["end_seconds"] == 5.0


def test_invalid_duration_and_candidate_limits_are_rejected():
    import pytest

    with pytest.raises(ValueError):
        rank_highlight_windows(0.0, [], [])
    with pytest.raises(ValueError):
        rank_highlight_windows(10.0, [], [], max_candidates=0)
