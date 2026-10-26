from pathlib import Path
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))

from app.render_profiles import build_video_filter, profile_warning, PROFILE_FIT_PAD, PROFILE_BLUR_FILL

def test_fit_pad_preserves_full_original_game_frame():
    result=build_video_filter(PROFILE_FIT_PAD)
    assert result["mode"]=="vf"
    assert result["video_map"]=="0:v:0"
    assert "force_original_aspect_ratio=decrease" in result["filter"]
    assert "pad=1080:1920" in result["filter"]
    assert result["profile"]=="fit_pad"

def test_blurred_backdrop_profile_preserves_fitted_foreground():
    result=build_video_filter(PROFILE_BLUR_FILL)
    assert result["mode"]=="filter_complex"
    assert result["video_map"]=="[outv]"
    assert "[0:v]split=2" in result["filter"]
    assert "boxblur=24:4" in result["filter"]
    assert "force_original_aspect_ratio=decrease[fg]" in result["filter"]
    assert "overlay=(W-w)/2:(H-h)/2" in result["filter"]
    assert "crop=1080:1920" in result["filter"]  # Only the blurred background is cropped.
    foreground=result["filter"].rsplit("[fgsrc]",1)[1].split("[fg]")[0]
    assert "crop=1080:1920" not in foreground

def test_subtitles_are_applied_after_composition():
    subtitle="subtitles=filename='captions.srt':force_style='FontSize=24'"
    fit=build_video_filter(PROFILE_FIT_PAD,subtitle)
    blur=build_video_filter(PROFILE_BLUR_FILL,subtitle)
    assert fit["filter"].endswith(","+subtitle)
    assert "[v]"+subtitle+"[outv]" in blur["filter"]

def test_unknown_profile_fails_closed_instead_of_falling_back_to_crop():
    with pytest.raises(ValueError,match="Unsupported render profile"):
        build_video_filter("crop_fill")

def test_profile_notes_are_explicit_about_content_preservation():
    assert "full source frame preserved" in profile_warning(PROFILE_FIT_PAD)[0]
    assert "full source frame preserved" in profile_warning(PROFILE_BLUR_FILL)[0]
