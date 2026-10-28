"""Lightweight, local highlight-candidate ranking from FFmpeg signals.

Scores order candidate windows; they are uncalibrated heuristics, never predictions
of views or virality. The helper has no network, API, or model dependency.
"""
from __future__ import annotations

import math
import re
import statistics
from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Sequence

_TIMESTAMP = re.compile(r"pts_time:([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)")
_LOG_PREFIX = re.compile(r"^\[([^\]]+)\]")
_NUMBER = re.compile(r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)")


def parse_timed_metric(log_text: str, metric_key: str) -> list[tuple[float, float]]:
    """Extract (seconds, value) pairs from FFmpeg metadata=print logs.

    The timestamp and metric are normally on separate lines in the same frame
    block, so the most recently observed pts_time is paired with the metric.
    """
    if not isinstance(log_text, str) or not metric_key:
        return []
    samples: list[tuple[float, float]] = []
    current_time_by_context: dict[str, float | None] = {}
    marker = metric_key + "="
    for line in log_text.splitlines():
        prefix_match = _LOG_PREFIX.match(line)
        context = prefix_match.group(1) if prefix_match else "__default__"
        timestamp_match = _TIMESTAMP.search(line)
        if timestamp_match:
            try:
                timestamp = float(timestamp_match.group(1))
                current_time_by_context[context] = timestamp if math.isfinite(timestamp) and timestamp >= 0 else None
            except (ValueError, OverflowError):
                current_time_by_context[context] = None
        current_time = current_time_by_context.get(context)
        index = line.find(marker)
        if index < 0 or current_time is None:
            continue
        value_match = _NUMBER.match(line[index + len(marker):].strip())
        if not value_match:
            continue
        try:
            value = float(value_match.group(1))
        except (ValueError, OverflowError):
            continue
        if math.isfinite(value):
            samples.append((current_time, value))
    # Stable timestamp order; keep the last metric value if FFmpeg reports a duplicate.
    by_time: dict[float, float] = {}
    for timestamp, value in samples:
        by_time[timestamp] = value
    return sorted(by_time.items())


def _quantile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * min(1.0, max(0.0, fraction))
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def audio_activity_scores(samples: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Normalize relative RMS loudness spikes to 0..1 using a robust local baseline.

    FFmpeg RMS values are typically negative dBFS. This does not compare absolute
    loudness between videos; it looks for sudden increases inside the same source.
    """
    cleaned = [
        (float(timestamp), float(value))
        for timestamp, value in samples
        if isinstance(timestamp, (int, float))
        and isinstance(value, (int, float))
        and not isinstance(timestamp, bool)
        and not isinstance(value, bool)
        and math.isfinite(float(timestamp))
        and math.isfinite(float(value))
        and float(timestamp) >= 0
    ]
    cleaned.sort(key=lambda item: item[0])
    if len(cleaned) < 3:
        return [(timestamp, 0.0) for timestamp, _ in cleaned]
    values = [value for _, value in cleaned]
    baseline = statistics.median(values)
    deviations = [abs(value - baseline) for value in values]
    mad = statistics.median(deviations)
    robust_spread = max(6.0, _quantile(values, 0.95) - baseline, 3.0 * mad)
    output = []
    for timestamp, value in cleaned:
        score = (value - baseline - 1.5) / robust_spread
        output.append((timestamp, round(min(1.0, max(0.0, score)), 4)))
    return output


def video_motion_activity_scores(samples: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Normalize frame-difference bursts to 0..1 using a robust source-local baseline.

    The input is FFmpeg signalstats.YAVG from low-resolution, half-second frame
    differences. It is a cheap motion proxy, not semantic action recognition:
    camera panning and scene cuts can also produce high values.
    """
    cleaned = [
        (float(timestamp), float(value))
        for timestamp, value in samples
        if isinstance(timestamp, (int, float))
        and isinstance(value, (int, float))
        and not isinstance(timestamp, bool)
        and not isinstance(value, bool)
        and math.isfinite(float(timestamp))
        and math.isfinite(float(value))
        and float(timestamp) >= 0
        and float(value) >= 0
    ]
    by_time: dict[float, float] = {}
    for timestamp, value in sorted(cleaned, key=lambda item: item[0]):
        by_time[timestamp] = value
    cleaned = sorted(by_time.items())
    if len(cleaned) < 3:
        return [(timestamp, 0.0) for timestamp, _ in cleaned]

    values = [value for _, value in cleaned]
    baseline = statistics.median(values)
    deviations = [abs(value - baseline) for value in values]
    mad = statistics.median(deviations)
    robust_spread = max(2.0, _quantile(values, 0.95) - baseline, 3.0 * mad)
    minimum_delta = max(1.0, baseline * 0.20)
    return [
        (timestamp, round(min(1.0, max(0.0, (value - baseline - minimum_delta) / robust_spread)), 4))
        for timestamp, value in cleaned
    ]


def _local_peak_timestamps(
    samples: Sequence[tuple[float, float]], *, radius_seconds: float, threshold: float
) -> list[float]:
    """Find deterministic local maxima in O(n) time using a monotonic deque."""
    if not samples:
        return []
    times = [timestamp for timestamp, _ in samples]
    values = [score for _, score in samples]
    peak_window: deque[int] = deque()
    right = 0
    peaks: list[float] = []
    for index, (timestamp, activity) in enumerate(samples):
        while right < len(samples) and times[right] <= timestamp + radius_seconds:
            while peak_window and values[peak_window[-1]] < values[right] - 1e-9:
                peak_window.pop()
            peak_window.append(right)
            right += 1
        while peak_window and times[peak_window[0]] < timestamp - radius_seconds:
            peak_window.popleft()
        # Ties keep the earliest sample to make plateaus deterministic.
        if activity >= threshold and peak_window and peak_window[0] == index:
            peaks.append(timestamp)
    return peaks


def rank_highlight_windows(
    duration_seconds: float,
    scene_change_seconds: Sequence[float],
    audio_rms_samples: Sequence[tuple[float, float]],
    *,
    video_motion_samples: Sequence[tuple[float, float]] = (),
    target_seconds: float = 15.0,
    max_candidates: int = 25,
) -> list[dict]:
    """Propose diverse Shorts-ready windows around scene changes/audio peaks.

    Window size defaults to 15 seconds, with 40% of the window before the event
    and 60% after it. The score is a heuristic for relative review priority,
    not a calibrated probability of a highlight or a prediction of reach.
    """
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
        raise ValueError("duration_seconds must be a positive finite number.")
    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration_seconds must be a positive finite number.")
    if isinstance(target_seconds, bool) or not isinstance(target_seconds, (int, float)):
        raise ValueError("target_seconds must be a positive finite number.")
    target = float(target_seconds)
    if not math.isfinite(target) or target <= 0:
        raise ValueError("target_seconds must be a positive finite number.")
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates < 1:
        raise ValueError("max_candidates must be a positive integer.")
    max_candidates = min(100, max_candidates)
    target = min(target, duration)

    scenes = sorted({
        float(value) for value in scene_change_seconds
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0 <= float(value) < duration
    })
    raw_audio = sorted([
        (float(timestamp), float(value))
        for timestamp, value in audio_rms_samples
        if isinstance(timestamp, (int, float))
        and isinstance(value, (int, float))
        and not isinstance(timestamp, bool)
        and not isinstance(value, bool)
        and math.isfinite(float(timestamp))
        and math.isfinite(float(value))
        and 0 <= float(timestamp) < duration
    ])
    audio_scores = audio_activity_scores(raw_audio)
    motion_scores = video_motion_activity_scores(video_motion_samples)
    has_audio_samples = len(audio_scores) >= 3
    has_motion_samples = len(motion_scores) >= 3
    event_times: set[float] = set(scenes)
    event_times.update(_local_peak_timestamps(audio_scores, radius_seconds=2.0, threshold=0.42))
    event_times.update(_local_peak_timestamps(motion_scores, radius_seconds=1.0, threshold=0.45))

    sample_times = [timestamp for timestamp, _ in audio_scores]
    sample_values = [score for _, score in audio_scores]
    motion_times = [timestamp for timestamp, _ in motion_scores]
    motion_values = [score for _, score in motion_scores]

    if not event_times:
        return []

    def nearby_audio_activity(timestamp: float) -> float:
        left = bisect_left(sample_times, timestamp - 2.0)
        right_index = bisect_right(sample_times, timestamp + 2.0)
        return max(sample_values[left:right_index], default=0.0)

    def nearby_visual_motion(timestamp: float) -> float:
        left = bisect_left(motion_times, timestamp - 1.0)
        right_index = bisect_right(motion_times, timestamp + 1.0)
        return max(motion_values[left:right_index], default=0.0)

    def scene_proximity(timestamp: float) -> float:
        if not scenes:
            return 0.0
        index = bisect_left(scenes, timestamp)
        distances = []
        if index < len(scenes):
            distances.append(abs(scenes[index] - timestamp))
        if index > 0:
            distances.append(abs(scenes[index - 1] - timestamp))
        distance = min(distances, default=math.inf)
        return round(max(0.0, 1.0 - distance / 4.0), 4)

    proposals = []
    for event_time in event_times:
        audio_score = nearby_audio_activity(event_time)
        motion_score = nearby_visual_motion(event_time)
        scene_score = scene_proximity(event_time)

        # A candidate must have an actual audio or motion event when those
        # measurements exist; scene cuts alone remain weak evidence.
        if has_audio_samples and has_motion_samples:
            if audio_score < 0.42 and motion_score < 0.45:
                continue
            # Audio is intentionally weighted slightly more than generic motion:
            # camera panning is common in games and is not necessarily exciting.
            score = 0.10 + 0.60 * audio_score + 0.25 * motion_score + 0.15 * scene_score
        elif has_audio_samples:
            if audio_score < 0.42:
                continue
            score = 0.15 + 0.65 * audio_score + 0.20 * scene_score
        elif has_motion_samples:
            if motion_score < 0.45:
                continue
            score = 0.15 + 0.65 * motion_score + 0.20 * scene_score
        else:
            # Scene changes alone are weak evidence, so keep their rank low.
            score = 0.15 + 0.35 * scene_score
        score = round(min(1.0, max(0.0, score)), 4)

        start = max(0.0, min(event_time - target * 0.40, duration - target))
        end = min(duration, start + target)
        if end - start < min(3.0, duration):
            continue

        if audio_score >= 0.42 and motion_score >= 0.45:
            event_type = "audio_motion_peak"
        elif motion_score >= 0.45 and scene_score >= 0.75:
            event_type = "visual_motion_peak_near_scene_change"
        elif motion_score >= 0.45:
            event_type = "visual_motion_peak"
        elif scene_score >= 0.75 and audio_score >= 0.42:
            event_type = "audio_peak_near_scene_change"
        elif audio_score >= 0.42:
            event_type = "audio_energy_peak"
        else:
            event_type = "scene_change_only"
        method = "local_audio_motion_scene_v2" if has_motion_samples else "local_audio_scene_v1"
        proposals.append({
            "start_seconds": round(start, 3),
            "end_seconds": round(end, 3),
            "event_time_seconds": round(event_time, 3),
            "ranking_score": score,
            "event_type": event_type,
            "ranking_components": {
                "method": method,
                "event_type": event_type,
                "audio_activity": round(audio_score, 4),
                "visual_motion_activity": round(motion_score, 4),
                "scene_proximity": round(scene_score, 4),
                "audio_samples_available": has_audio_samples,
                "visual_motion_samples_available": has_motion_samples,
                "score_calibrated": False,
                "score_meaning": "relative review priority only; not a view or virality prediction",
            },
        })

    # Prefer the strongest evidence, then remove overlapping/near-duplicate windows.
    proposals.sort(key=lambda item: (-item["ranking_score"], item["event_time_seconds"]))
    selected: list[dict] = []
    minimum_event_separation = max(6.0, target * 0.50)
    for proposal in proposals:
        event_time = proposal["event_time_seconds"]
        if any(abs(event_time - item["event_time_seconds"]) < minimum_event_separation for item in selected):
            continue
        selected.append(proposal)
        if len(selected) >= max_candidates:
            break
    return sorted(selected, key=lambda item: (-item["ranking_score"], item["start_seconds"]))


VISION_RENDER_MIN_SCORE = 0.75
VISION_RENDER_MIN_CONFIDENCE = 0.75
VISION_RENDER_ALLOWED_EVENTS = frozenset({"clutch", "elimination", "victory", "near_death", "team_fight", "skill_play"})


def is_render_worthy_vision_assessment(assessment: dict) -> bool:
    """Fail-closed semantic gate for unattended rendering; not a view prediction."""
    if not isinstance(assessment, dict) or assessment.get("status", "COMPLETED") != "COMPLETED":
        return False
    if assessment.get("is_actionable_highlight") not in (True, 1):
        return False
    if assessment.get("event_type") not in VISION_RENDER_ALLOWED_EVENTS:
        return False
    for field, threshold in (("highlight_score", VISION_RENDER_MIN_SCORE), ("confidence", VISION_RENDER_MIN_CONFIDENCE)):
        value = assessment.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < threshold:
            return False
    issues = assessment.get("quality_issues", [])
    if not isinstance(issues, list) or issues:
        return False
    evidence = assessment.get("evidence", [])
    return isinstance(evidence, list) and any(isinstance(item, str) and item.strip() for item in evidence)
