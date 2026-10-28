"""OpenRouter-backed multimodal gameplay highlight judge.

The cheap FFmpeg signal pass finds candidate windows locally. This module sends
only a small batch of sampled frames to OpenRouter's free-model router for
semantic judgment; the selected provider model is saved with every result.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re

import requests

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openrouter/free"
EVENT_TYPES = {
    "clutch", "elimination", "victory", "near_death", "team_fight",
    "skill_play", "funny_moment", "close_call", "movement",
    "menu_or_loading", "ordinary_gameplay", "unknown",
}


class AIJudgeNotConfigured(RuntimeError):
    pass


class AIJudgeError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def ai_judge_status() -> dict:
    enabled = os.getenv("GCF_ENABLE_AI_JUDGING", "true").strip().lower() == "true"
    key_present = bool(os.getenv("GCF_OPENROUTER_API_KEY", "").strip())
    model = os.getenv("GCF_OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    try:
        max_candidates = max(1, min(12, int(os.getenv("GCF_AI_JUDGE_MAX_CANDIDATES_PER_MEDIA", "4") or "4")))
    except (TypeError, ValueError):
        max_candidates = 4
    try:
        batch_size = max(1, min(4, int(os.getenv("GCF_AI_JUDGE_BATCH_SIZE", "4") or "4")))
    except (TypeError, ValueError):
        batch_size = 4
    try:
        sample_frames = int(os.getenv("GCF_AI_JUDGE_SAMPLE_FRAMES", "3") or "3")
    except (TypeError, ValueError):
        sample_frames = 3
    if sample_frames not in {3, 4}:
        sample_frames = 3
    return {
        "enabled": enabled,
        "provider": "OpenRouter",
        "model": model,
        "random_free_model_router": model == DEFAULT_MODEL,
        "api_key_configured": key_present,
        "ready": bool(enabled and key_present),
        "endpoint": ENDPOINT,
        "max_candidates_per_media": max_candidates,
        "batch_size": batch_size,
        "sample_frames": sample_frames,
        "requests_per_media_estimate": math.ceil(max_candidates / batch_size),
        "automatic_approval": False,
        "score_calibrated": False,
        "privacy_note": "Sampled gameplay frames and prompt context are sent to OpenRouter; the full source video stays local.",
    }


def sampling_fractions(event_fraction: float | None = None) -> list[float]:
    """Choose a few representative frames and include the detected event when known."""
    try:
        count = int(os.getenv("GCF_AI_JUDGE_SAMPLE_FRAMES", "3") or "3")
    except (TypeError, ValueError):
        count = 3
    if count not in {3, 4}:
        count = 3
    valid = (
        isinstance(event_fraction, (int, float))
        and not isinstance(event_fraction, bool)
        and math.isfinite(float(event_fraction))
    )
    if not valid:
        return [0.2, 0.5, 0.8] if count == 3 else [0.1, 0.4, 0.6, 0.9]
    focus = min(1.0, max(0.0, float(event_fraction)))
    if count == 3:
        if focus < 0.18:
            fractions = [focus, focus + 0.10, focus + 0.25]
        elif focus > 0.82:
            fractions = [focus - 0.25, focus - 0.10, focus]
        else:
            fractions = [focus - 0.16, focus, focus + 0.16]
    else:
        if focus < 0.18:
            fractions = [focus, focus + 0.07, focus + 0.16, 0.9]
        elif focus > 0.82:
            fractions = [0.1, focus - 0.16, focus - 0.07, focus]
        else:
            fractions = [0.1, focus - 0.12, focus + 0.12, 0.9]
    return sorted(round(min(1.0, max(0.0, value)), 4) for value in fractions)


def _settings() -> tuple[str, str, int]:
    status = ai_judge_status()
    if not status["enabled"]:
        raise AIJudgeNotConfigured("OpenRouter AI highlight judgment is disabled. Set GCF_ENABLE_AI_JUDGING=true.")
    key = os.getenv("GCF_OPENROUTER_API_KEY", "").strip()
    if not key:
        raise AIJudgeNotConfigured("OpenRouter AI highlight judgment needs GCF_OPENROUTER_API_KEY in the local .env file.")
    model = os.getenv("GCF_OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    try:
        timeout = max(15, min(240, int(os.getenv("GCF_OPENROUTER_TIMEOUT_SECONDS", "90") or "90")))
    except (TypeError, ValueError):
        timeout = 90
    return key, model, timeout


def _valid_result(item: object, expected_key: str, model_name: str, configured_model: str) -> dict:
    if not isinstance(item, dict) or item.get("candidate_key") != expected_key:
        raise AIJudgeError(f"OpenRouter response is missing the expected result for {expected_key}.")
    for name in ("gameplay_context", "event_type", "visible_event"):
        if not isinstance(item.get(name), str):
            raise AIJudgeError(f"OpenRouter result field {name} must be text.")
    event_type = item["event_type"] if item["event_type"] in EVENT_TYPES else "unknown"
    actionable = item.get("is_actionable_highlight")
    if not isinstance(actionable, bool):
        raise AIJudgeError("OpenRouter result is_actionable_highlight must be boolean.")
    normalized = {}
    for name in ("highlight_score", "confidence"):
        raw_value = item.get(name)
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)) or not math.isfinite(float(raw_value)) or not 0 <= float(raw_value) <= 1:
            raise AIJudgeError(f"OpenRouter result field {name} must be a finite number from 0 to 1.")
        normalized[name] = round(float(raw_value), 4)
    for name in ("evidence", "quality_issues"):
        value = item.get(name)
        if not isinstance(value, list) or any(not isinstance(part, str) for part in value):
            raise AIJudgeError(f"OpenRouter result field {name} must be a list of strings.")
        normalized[name] = [part[:400] for part in value[:8]]
    return {
        "candidate_key": expected_key,
        "model_name": model_name,
        "configured_model": configured_model,
        "gameplay_context": item["gameplay_context"][:1200],
        "event_type": event_type,
        "visible_event": item["visible_event"][:1200],
        "is_actionable_highlight": actionable,
        **normalized,
        "assessment_method": "openrouter_multimodal_highlight_judge",
        "score_calibrated": False,
    }


def assess_candidate_batch(candidates: list[dict], *, http_post=None) -> list[dict]:
    """Judge 1–4 candidate clips in one multimodal request to conserve free requests."""
    key, model, timeout = _settings()
    if not isinstance(candidates, list) or not candidates or len(candidates) > 4:
        raise ValueError("OpenRouter batch must contain between 1 and 4 candidate clips.")
    candidate_keys = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Every candidate must be an object.")
        candidate_key = candidate.get("candidate_key")
        images = candidate.get("images")
        if not isinstance(candidate_key, str) or not re.fullmatch(r"C[1-4]", candidate_key):
            raise ValueError("Candidate keys must be C1 through C4.")
        if candidate_key in candidate_keys:
            raise ValueError("Candidate keys must be unique within a batch.")
        if not isinstance(images, list) or not images or len(images) > 4 or any(not isinstance(img, bytes) or not img for img in images):
            raise ValueError("Each candidate requires 1–4 nonempty JPEG frame images.")
        times = candidate.get("frame_times_seconds", [])
        if not isinstance(times, list) or len(times) != len(images):
            raise ValueError("Frame timestamps must match each candidate's sampled images.")
        if any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(float(t)) or float(t) < 0 for t in times):
            raise ValueError("Frame timestamps must be finite, nonnegative seconds.")
        candidate_keys.append(candidate_key)

    system_prompt = (
        "You are a conservative gaming highlight editor. Judge the relative entertainment value "
        "of short gameplay candidates using only visible evidence and the provided local signal hints. "
        "The signals propose moments; they do not prove a highlight. Do not hallucinate kills, wins, "
        "clutches, player reactions, or events not established by frames. Quiet but clearly skilled "
        "plays can be valuable. Penalize menus, loading screens, prolonged downtime, black/frozen frames, "
        "and samples that show no distinguishable gameplay. Still frames cannot prove unseen motion. "
        "If an event is uncertain, use event_type=unknown, actionable=false, and conservative confidence. "
        "Use quality_issues only for a concrete visible concern or when the samples do not establish a "
        "specific worthwhile event; do not add a generic warning merely because samples are sparse. "
        "Return only one JSON object with a candidates array and one result for every supplied candidate_key."
    )
    user_parts = [{
        "type": "text",
        "text": (
            "Compare these candidate windows. Return valid JSON: "
            '{"candidates":[{"candidate_key":"C1","gameplay_context":"...","event_type":"skill_play",'
            '"visible_event":"...","is_actionable_highlight":true,"highlight_score":0.0,'
            '"confidence":0.0,"evidence":["..."],"quality_issues":[]}]} '
            "Use event_type from: " + ", ".join(sorted(EVENT_TYPES)) + ". "
            "Scores are subjective ordering aids, not calibrated probabilities. "
            "Give each candidate a separate score and evidence list. "
            "Signal scores only indicate audio/motion/scene activity, not semantic truth."
        ),
    }]
    for candidate in candidates:
        candidate_key = candidate["candidate_key"]
        start = float(candidate.get("start_seconds", 0))
        end = float(candidate.get("end_seconds", 0))
        signal_score = candidate.get("signal_score", 0)
        event_time = candidate.get("event_time_seconds")
        user_parts.append({
            "type": "text",
            "text": (
                f"\n--- {candidate_key} ---\n"
                f"Game: {str(candidate.get('game') or 'unknown')[:120]}\n"
                f"Source title: {str(candidate.get('source_title') or 'unknown')[:200]}\n"
                f"Candidate duration: {max(0, end-start):.2f}s; signal rank: {float(signal_score or 0):.3f}/1 (uncalibrated); "
                f"detected event time inside candidate: {event_time if event_time is not None else 'unknown'}s.\n"
                f"Optional transcript excerpt (may be inaccurate): {str(candidate.get('transcript_excerpt') or '[none]')[:600]}\n"
                "Frames immediately follow in chronological order; their candidate-relative seconds are: "
                + ", ".join(f"{float(t):.3f}" for t in candidate["frame_times_seconds"])
                + ". Refer to visible evidence from these frames, not imagined events."
            ),
        })
        for index, (image_bytes, frame_time) in enumerate(zip(candidate["images"], candidate["frame_times_seconds"]), start=1):
            encoded = base64.b64encode(image_bytes).decode("ascii")
            user_parts.append({"type": "text", "text": f"{candidate_key} frame {index}, {float(frame_time):.3f}s after candidate start:"})
            user_parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})

    body = {
        "model": model,
        "stream": False,
        "temperature": 0,
        "max_tokens": 1600 + 500 * len(candidates),
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_parts},
        ],
    }
    post = http_post or requests.post
    try:
        response = post(
            ENDPOINT,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "X-Title": "Gaming Clip Factory",
            },
            json=body,
            timeout=(5, timeout),
        )
        response.raise_for_status()
        raw = response.json()
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        retryable = status_code is None or status_code >= 500
        raise AIJudgeError(
            f"OpenRouter request failed ({type(exc).__name__}); check network, API key, free-model limits and model availability.",
            retryable=retryable,
        ) from exc
    except (ValueError, TypeError) as exc:
        raise AIJudgeError("OpenRouter returned an invalid JSON response.") from exc
    if not isinstance(raw, dict):
        raise AIJudgeError("OpenRouter response must be a JSON object.")
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AIJudgeError("OpenRouter response did not contain a completion choice.")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
    if not isinstance(content, str) or not content.strip():
        raise AIJudgeError("OpenRouter model returned no text content.")
    try:
        decoded = json.loads(content.strip())
    except ValueError:
        cleaned = content.strip().strip(chr(96)).strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        try:
            decoded = json.loads(cleaned)
        except ValueError as exc:
            raise AIJudgeError("OpenRouter did not return valid JSON for the highlight judgment.") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("candidates"), list):
        raise AIJudgeError("OpenRouter result must contain a candidates array.")
    actual_model = raw.get("model") if isinstance(raw.get("model"), str) and raw.get("model") else model
    by_key = {}
    for item in decoded["candidates"]:
        if not isinstance(item, dict):
            raise AIJudgeError("Every OpenRouter candidate result must be an object.")
        candidate_key = item.get("candidate_key")
        if candidate_key not in candidate_keys or candidate_key in by_key:
            raise AIJudgeError("OpenRouter returned an unknown or duplicate candidate key.")
        by_key[candidate_key] = _valid_result(item, candidate_key, actual_model, model)
    if set(by_key) != set(candidate_keys):
        missing = ", ".join(sorted(set(candidate_keys) - set(by_key)))
        raise AIJudgeError(f"OpenRouter omitted candidate results: {missing}.")
    return [by_key[candidate_key] for candidate_key in candidate_keys]
