"""Deterministic Shorts cover-art generation from the actual rendered frame.

This is an editorial layout, not generated imagery: the visible scene comes from the
rendered video and the only semantic copy is the owner-selected video title/game.
"""
from __future__ import annotations
from pathlib import Path
import hashlib
import os
import tempfile
from PIL import Image, ImageDraw, ImageFont, ImageOps

TARGET_SIZE=(2160,3840)
FALLBACK_SIZE=(1080,1920)
MAX_UPLOAD_BYTES=1_900_000

def _font(size: int, bold: bool=True):
    candidates = (
        ("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate,size=size)
            except OSError:
                continue
    return ImageFont.load_default()

def _wrap(text: str, font, max_width: int) -> list[str]:
    words=" ".join(str(text or "").replace("\r"," ").replace("\n"," ").split()).split(" ")
    lines=[]; current=""
    for word in words:
        if not word:
            continue
        attempt=word if not current else current+" "+word
        if current and font.getlength(attempt)>max_width:
            lines.append(current)
            current=word
        else:
            current=attempt
    if current:
        lines.append(current)
    return lines or ["GAMEPLAY MOMENT"]

def _draw_cover(image: Image.Image, game: str, title: str) -> Image.Image:
    image=ImageOps.fit(image.convert("RGB"),TARGET_SIZE,method=Image.Resampling.LANCZOS,centering=(0.5,0.5))
    overlay=Image.new("RGBA",TARGET_SIZE,(0,0,0,0))
    overlay_draw=ImageDraw.Draw(overlay)
    width,height=TARGET_SIZE
    # Gentle edge-to-edge gradients protect text contrast without covering the gameplay focal point.
    # Draw scan lines rather than touching every pixel in Python; the result is identical per row and much faster.
    for y in range(height):
        if y < 1550:
            alpha=int(215*(1-y/1550))
        elif y > 2800:
            alpha=int(165*((y-2800)/(height-2800)))
        else:
            alpha=0
        if alpha:
            overlay_draw.line((0,y,width,y),fill=(4,8,18,alpha))
    image=Image.alpha_composite(image.convert("RGBA"),overlay)
    draw=ImageDraw.Draw(image)
    margin=150
    small=_font(72,True)
    brand="GAMING CLIP FACTORY"
    draw.rounded_rectangle((margin,180,margin+760,300),radius=48,fill=(8,14,28,220),outline=(72,224,255,255),width=6)
    draw.text((margin+35,207),brand,font=_font(48,True),fill=(103,232,255,255))
    game_text=" ".join(str(game or "").replace("\r"," ").replace("\n"," ").split()).strip()
    if game_text:
        game_text=game_text[:72]
        draw.text((margin,385),game_text.upper(),font=small,fill=(255,217,102,255),stroke_width=2,stroke_fill=(5,8,16,210))
    clean_title=" ".join(str(title or "").replace("\r"," ").replace("\n"," ").split()).strip()
    if not clean_title:
        clean_title="Gameplay moment"
    clean_title=clean_title[:120]
    usable_width=width-2*margin
    chosen_font=None; lines=None
    for font_size in (190,176,162,148,136,124,112,100):
        candidate_font=_font(font_size,True)
        candidate_lines=_wrap(clean_title,candidate_font,usable_width)
        if len(candidate_lines)<=4 and max(candidate_font.getlength(line) for line in candidate_lines)<=usable_width:
            chosen_font=candidate_font;lines=candidate_lines;break
    if chosen_font is None:
        chosen_font=_font(92,True)
        lines=_wrap(clean_title,chosen_font,usable_width)[:5]
        if lines:
            lines[-1]=lines[-1].rstrip(" .,:;")[:38]
    line_height=int((chosen_font.size if hasattr(chosen_font,"size") else 92)*1.16)
    text_height=line_height*len(lines)
    text_y=max(940,min(1680,2020-text_height//2))
    # Left-side rail and shadows give the headline contrast while keeping the frame visible.
    draw.rounded_rectangle((margin,text_y-45,margin+20,text_y+text_height+35),radius=10,fill=(72,224,255,255))
    for index,line in enumerate(lines):
        y=text_y+index*line_height
        draw.text((margin+50+9,y+9),line,font=chosen_font,fill=(0,0,0,190),stroke_width=6,stroke_fill=(0,0,0,165))
        draw.text((margin+50,y),line,font=chosen_font,fill=(255,255,255,255),stroke_width=3,stroke_fill=(7,12,25,255))
    footer_y=height-350
    draw.line((margin,footer_y-50,width-margin,footer_y-50),fill=(72,224,255,215),width=8)
    draw.text((margin,footer_y),"GAMEPLAY • SHORTS",font=_font(58,True),fill=(241,246,255,255),stroke_width=2,stroke_fill=(5,8,16,230))
    return image.convert("RGB")

def generate_shorts_thumbnail(
    poster_path: str | Path,
    title: str,
    game: str,
    output_path: str | Path,
    max_bytes: int=MAX_UPLOAD_BYTES,
) -> dict:
    poster=Path(poster_path).expanduser().resolve()
    target=Path(output_path).expanduser().resolve()
    if not poster.is_file() or poster.stat().st_size<=0:
        raise FileNotFoundError("A valid rendered poster frame is required to generate a YouTube Shorts cover.")
    target.parent.mkdir(parents=True,exist_ok=True)
    with Image.open(poster) as source:
        source.load()
        cover=_draw_cover(source,game,title)
    temp_name=None
    try:
        with tempfile.NamedTemporaryFile(prefix=target.stem+"-",suffix=".part",dir=target.parent,delete=False) as temp:
            temp_name=temp.name
        dimensions=TARGET_SIZE
        # Retain recommended high-resolution vertical art when practical; fall back to 1080x1920
        # only when the visual entropy of the gameplay frame makes a compact JPEG impractical.
        for size in (TARGET_SIZE,FALLBACK_SIZE):
            candidate=cover if size==TARGET_SIZE else cover.resize(size,Image.Resampling.LANCZOS)
            wrote=False
            for quality in (90,85,80,75,70,65,60):
                candidate.save(temp_name,format="JPEG",quality=quality,optimize=True,progressive=True,subsampling=0)
                wrote=True
                if Path(temp_name).stat().st_size<=max_bytes:
                    dimensions=size
                    break
            if wrote and Path(temp_name).stat().st_size<=max_bytes:
                break
        if Path(temp_name).stat().st_size>max_bytes:
            raise RuntimeError(f"Generated cover exceeds the configured {max_bytes}-byte upload limit.")
        with Image.open(temp_name) as check:
            check.verify()
        os.replace(temp_name,target)
        temp_name=None
    finally:
        if temp_name and Path(temp_name).exists():
            try: Path(temp_name).unlink()
            except OSError: pass
    h=hashlib.sha256()
    with target.open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):
            h.update(block)
    with Image.open(target) as final_image:
        width,height=final_image.size
    return {
        "path":str(target),
        "sha256":h.hexdigest(),
        "file_size":target.stat().st_size,
        "width":width,
        "height":height,
        "mime_type":"image/jpeg",
        "design_version":"shorts-cover-v1",
        "source_poster":str(poster),
        "max_bytes":max_bytes,
    }
