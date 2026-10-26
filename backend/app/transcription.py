"""Optional local Faster-Whisper transcription; no remote transcription API is called."""
from __future__ import annotations
from pathlib import Path
import os
import re

MODEL_NAMES={"tiny","tiny.en","base","base.en","small","small.en","medium","medium.en","large-v2","large-v3","distil-small.en","distil-medium.en","distil-large-v3"}

def _pack_words(words, max_words=6, max_chars=34):
    packed=[]; current=[]
    def flush():
        if not current: return
        phrase="".join(str(getattr(w,"word","")) for w in current).strip()
        start=getattr(current[0],"start",None); end=getattr(current[-1],"end",None)
        probabilities=[getattr(w,"probability",None) for w in current]
        probabilities=[float(p) for p in probabilities if p is not None]
        confidence=sum(probabilities)/len(probabilities) if probabilities else None
        if phrase and start is not None and end is not None and float(end)>float(start):
            packed.append({"start_seconds":round(float(start),3),"end_seconds":round(float(end),3),"text":phrase,"confidence":round(confidence,4) if confidence is not None else None})
        current.clear()
    for word in words:
        token=str(getattr(word,"word",""))
        candidate="".join(str(getattr(w,"word","")) for w in current)+token
        if current and (len(current)>=max_words or len(candidate)>max_chars or re.search(r"[.!?]$", "".join(str(getattr(w,"word","")) for w in current))):
            flush()
        current.append(word)
    flush()
    return packed

def transcribe_file(media_path: str | Path) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Optional transcription dependency is missing. Run pip install -r backend/requirements-ml.txt.") from exc
    path=Path(media_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Media file not found: {path}")
    model_path=os.getenv("GCF_WHISPER_MODEL_PATH","").strip()
    allow_download=os.getenv("GCF_WHISPER_ALLOW_MODEL_DOWNLOAD","").lower()=="true"
    if model_path:
        model=Path(model_path).expanduser().resolve()
        if not model.is_dir():
            raise RuntimeError("GCF_WHISPER_MODEL_PATH must point to an existing local model directory.")
        model_source=str(model)
    else:
        model_source=os.getenv("GCF_WHISPER_MODEL_SIZE","small").strip()
        if model_source not in MODEL_NAMES:
            raise RuntimeError("Unsupported GCF_WHISPER_MODEL_SIZE. Choose a documented Faster-Whisper model name or set GCF_WHISPER_MODEL_PATH.")
        if not allow_download:
            raise RuntimeError("Model download is disabled. Set GCF_WHISPER_MODEL_PATH to a local model or explicitly set GCF_WHISPER_ALLOW_MODEL_DOWNLOAD=true.")
    cache=Path(os.getenv("GCF_WHISPER_CACHE_DIR",str(Path.home()/".cache"/"gaming-clip-factory-whisper"))).expanduser()
    cache.mkdir(parents=True,exist_ok=True)
    device=os.getenv("GCF_WHISPER_DEVICE","cpu").strip().lower()
    compute=os.getenv("GCF_WHISPER_COMPUTE_TYPE","int8" if device=="cpu" else "float16").strip()
    try:
        model=WhisperModel(model_source,device=device,compute_type=compute,download_root=str(cache))
        segment_iter,info=model.transcribe(str(path),beam_size=5,vad_filter=True,word_timestamps=True)
        segments=[]; fallback=[]
        for segment in segment_iter:
            words=getattr(segment,"words",None) or []
            if words:
                segments.extend(_pack_words(words))
            else:
                text=str(getattr(segment,"text","")).strip()
                if text and float(segment.end)>float(segment.start):
                    fallback.append({"start_seconds":round(float(segment.start),3),"end_seconds":round(float(segment.end),3),"text":text,"confidence":None})
        if not segments: segments=fallback
        segments.sort(key=lambda x:(x["start_seconds"],x["end_seconds"]))
        full_text=" ".join(s["text"] for s in segments).strip()
        return {
            "model_name":model_source,
            "language":str(getattr(info,"language","unknown") or "unknown"),
            "language_probability":float(getattr(info,"language_probability",0) or 0),
            "full_text":full_text,
            "segments":segments,
        }
    except Exception as exc:
        raise RuntimeError(f"Local transcription failed ({type(exc).__name__}): {str(exc)[:500]}") from exc
