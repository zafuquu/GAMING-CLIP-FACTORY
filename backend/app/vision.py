"""Optional local vision-model adapter for evidence-grounded candidate assessment."""
from __future__ import annotations
import base64
import json
import os
from urllib.parse import urlparse
import requests

class VisionNotConfigured(RuntimeError):
    pass

class VisionAnalysisError(RuntimeError):
    pass

EVENT_TYPES={"clutch","elimination","victory","near_death","team_fight","skill_play","movement","menu_or_loading","ordinary_gameplay","unknown"}
SCHEMA={
    "type":"object",
    "properties":{
        "gameplay_context":{"type":"string"},
        "event_type":{"type":"string","enum":sorted(EVENT_TYPES)},
        "visible_event":{"type":"string"},
        "is_actionable_highlight":{"type":"boolean"},
        "highlight_score":{"type":"number","minimum":0,"maximum":1},
        "confidence":{"type":"number","minimum":0,"maximum":1},
        "evidence":{"type":"array","items":{"type":"string"},"maxItems":8},
        "quality_issues":{"type":"array","items":{"type":"string"},"maxItems":8},
    },
    "required":["gameplay_context","event_type","visible_event","is_actionable_highlight","highlight_score","confidence","evidence","quality_issues"],
}

def _settings():
    if os.getenv("GCF_ENABLE_VISION_ANALYSIS","").strip().lower()!="true":
        raise VisionNotConfigured("Vision analysis is disabled. Set GCF_ENABLE_VISION_ANALYSIS=true to opt in.")
    endpoint=os.getenv("GCF_VISION_BASE_URL","http://127.0.0.1:11434/api/chat").strip()
    parsed=urlparse(endpoint)
    if parsed.scheme!="http" or not parsed.hostname or not parsed.path.endswith("/api/chat"):
        raise VisionNotConfigured("GCF_VISION_BASE_URL must be an HTTP Ollama-compatible /api/chat endpoint.")
    local_host=parsed.hostname.lower() in {"127.0.0.1","localhost","::1"}
    if not local_host and os.getenv("GCF_VISION_ALLOW_REMOTE","").strip().lower()!="true":
        raise VisionNotConfigured("Remote vision endpoints are blocked by default. Use loopback, or explicitly set GCF_VISION_ALLOW_REMOTE=true after a privacy review.")
    model=os.getenv("GCF_VISION_MODEL","llama3.2-vision").strip()
    if not model:
        raise VisionNotConfigured("GCF_VISION_MODEL cannot be blank.")
    try:
        timeout=max(15,min(600,int(os.getenv("GCF_VISION_TIMEOUT_SECONDS","180"))))
    except (TypeError,ValueError):
        timeout=180
    return endpoint,model,timeout

def assess_candidate_images(images: list[bytes], game: str, source_title: str, transcript_excerpt: str="", http_post=None) -> dict:
    endpoint,model,timeout=_settings()
    if not images or len(images)>5 or any(not isinstance(x,bytes) or not x for x in images):
        raise ValueError("Vision assessment requires 1–5 nonempty sampled frame images.")
    encoded=[base64.b64encode(x).decode("ascii") for x in images]
    user_text=(
        "Assess these sequential gameplay screenshots from one short candidate clip. "
        "They are sparse still frames; do not claim motion or an event unless visible evidence supports it. "
        "Use event_type=unknown and low confidence when the game/event is unclear. "
        "Do not infer a kill, clutch, win, reaction, or strategy merely from an exciting-looking frame. "
        "Evidence must refer to visible HUD/scene cues or directly quoted transcript words. "
        "The scores are subjective model estimates, not calibrated probabilities. "
        f"Game metadata: {game or 'unknown'}. Source title: {source_title or 'unknown'}. "
        f"Optional transcript excerpt (may be inaccurate): {transcript_excerpt[:1200] or '[none]'}."
    )
    body={
        "model":model,
        "stream":False,
        "format":SCHEMA,
        "options":{"temperature":0},
        "messages":[
            {"role":"system","content":"You are a conservative gameplay clip reviewer. Never invent visual details. Separate observation from interpretation. Return only JSON matching the schema."},
            {"role":"user","content":user_text,"images":encoded},
        ],
    }
    post=http_post or requests.post
    try:
        response=post(endpoint,json=body,timeout=(5,timeout))
        response.raise_for_status()
        raw=response.json()
    except requests.RequestException as exc:
        raise VisionAnalysisError(f"Local vision endpoint request failed ({type(exc).__name__}). Is the configured service/model running?") from exc
    except (ValueError,TypeError) as exc:
        raise VisionAnalysisError("Vision endpoint returned invalid JSON.") from exc
    if not isinstance(raw,dict):
        raise VisionAnalysisError("Vision endpoint response must be a JSON object.")
    message=raw.get("message") or {}
    content=message.get("content")
    if not isinstance(content,str) or not content.strip():
        raise VisionAnalysisError("Vision endpoint response did not contain message.content.")
    try:
        result=json.loads(content)
    except ValueError as exc:
        raise VisionAnalysisError("Vision model did not return schema-conforming JSON.") from exc
    if not isinstance(result,dict):
        raise VisionAnalysisError("Vision result must be a JSON object.")
    for field in ("gameplay_context","event_type","visible_event"):
        if not isinstance(result.get(field),str):
            raise VisionAnalysisError(f"Vision result field {field} must be text.")
        result[field]=result[field][:1200]
    if result["event_type"] not in EVENT_TYPES:
        result["event_type"]="unknown"
    for field in ("highlight_score","confidence"):
        value=result.get(field)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not 0<=float(value)<=1:
            raise VisionAnalysisError(f"Vision result field {field} must be numeric from 0 to 1.")
        result[field]=round(float(value),4)
    if not isinstance(result.get("is_actionable_highlight"),bool):
        raise VisionAnalysisError("Vision result is_actionable_highlight must be boolean.")
    for field in ("evidence","quality_issues"):
        value=result.get(field)
        if not isinstance(value,list) or any(not isinstance(x,str) for x in value):
            raise VisionAnalysisError(f"Vision result field {field} must be a list of strings.")
        result[field]=[x[:400] for x in value[:8]]
    result["model_name"]=model
    result["assessment_method"]="local-vision-model-estimate"
    result["score_calibrated"]=False
    return result

def vision_status() -> dict:
    enabled=os.getenv("GCF_ENABLE_VISION_ANALYSIS","").strip().lower()=="true"
    endpoint=os.getenv("GCF_VISION_BASE_URL","http://127.0.0.1:11434/api/chat").strip()
    parsed=urlparse(endpoint)
    host=(parsed.hostname or "").lower()
    loopback=host in {"127.0.0.1","localhost","::1"}
    valid=parsed.scheme=="http" and bool(parsed.hostname) and parsed.path.endswith("/api/chat")
    remote_allowed=os.getenv("GCF_VISION_ALLOW_REMOTE","").strip().lower()=="true"
    return {
        "enabled":enabled,
        "model":os.getenv("GCF_VISION_MODEL","llama3.2-vision").strip() or "unknown",
        "endpoint_valid":valid,
        "endpoint_local":loopback,
        "remote_allowed":remote_allowed,
        "ready":bool(enabled and valid and (loopback or remote_allowed)),
        "max_candidates_per_media":max(1,min(100,int(os.getenv("GCF_VISION_MAX_CANDIDATES_PER_MEDIA","25") or "25"))),
        "score_calibrated":False,
        "automatic_approval":False,
    }
