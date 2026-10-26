"""Deterministic render profiles that preserve gameplay content unless explicitly configured otherwise."""
from __future__ import annotations

PROFILE_FIT_PAD = "fit_pad"
PROFILE_BLUR_FILL = "blur_fill"
PROFILES = {PROFILE_FIT_PAD, PROFILE_BLUR_FILL}

FIT_PAD_FILTER = (
    "scale=1080:1920:force_original_aspect_ratio=decrease,"
    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black"
)

def build_video_filter(profile: str, subtitle_filter: str | None = None) -> dict:
    """Return FFmpeg args for a known profile. Never silently substitutes a crop profile."""
    if profile not in PROFILES:
        raise ValueError(f"Unsupported render profile: {profile}")
    if profile == PROFILE_FIT_PAD:
        video_filter = FIT_PAD_FILTER
        if subtitle_filter:
            video_filter += "," + subtitle_filter
        return {"mode":"vf","filter":video_filter,"video_map":"0:v:0","profile":profile}
    graph = (
        "[0:v]split=2[bgsrc][fgsrc];"
        "[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=24:4,eq=brightness=-0.18:saturation=0.80[bg];"
        "[fgsrc]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
    )
    if subtitle_filter:
        graph += ";" + "[v]" + subtitle_filter + "[outv]"
    else:
        graph += ";[v]null[outv]"
    return {"mode":"filter_complex","filter":graph,"video_map":"[outv]","profile":profile}

def profile_warning(profile: str) -> list[str]:
    if profile == PROFILE_FIT_PAD:
        return ["fit_pad: full source frame preserved; empty canvas areas are black."]
    if profile == PROFILE_BLUR_FILL:
        return ["blur_fill: full source frame preserved over a blurred, darkened gameplay background."]
    raise ValueError(f"Unsupported render profile: {profile}")
