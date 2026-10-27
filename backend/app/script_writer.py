"""Optional local Ollama-compatible narration script drafting.

The model may propose words, but every draft remains unreviewed until the owner
explicitly approves it. Only transcript/vision evidence supplied by the caller is
included in the prompt; no external network service is contacted by this module.
"""
from __future__ import annotations
import json
import os
from urllib.parse import urlparse
import requests

class ScriptDraftNotConfigured(RuntimeError): pass
class ScriptDraftError(RuntimeError): pass

SCHEMA={
    "type":"object",
    "properties":{
        "script":{"type":"string"},
        "hook":{"type":"string"},
        "evidence_used":{"type":"array","maxItems":8,"items":{
            "type":"object",
            "properties":{
                "ref":{"type":"string"},
                "quote":{"type":"string"},
            },
            "required":["ref","quote"],
        }},
        "unsupported_claims":{"type":"array","maxItems":8,"items":{"type":"string"}},
    },
    "required":["script","hook","evidence_used","unsupported_claims"],
}

def _config():
    if os.getenv("GCF_ENABLE_SCRIPT_DRAFTING","").strip().lower()!="true":
        raise ScriptDraftNotConfigured("Local script drafting is disabled. Set GCF_ENABLE_SCRIPT_DRAFTING=true to opt in.")
    endpoint=os.getenv("GCF_SCRIPT_DRAFTER_BASE_URL","http://127.0.0.1:11434/api/chat").strip()
    parsed=urlparse(endpoint)
    if parsed.scheme!="http" or not parsed.hostname or not parsed.path.endswith("/api/chat"):
        raise ScriptDraftNotConfigured("GCF_SCRIPT_DRAFTER_BASE_URL must be an HTTP Ollama-compatible /api/chat endpoint.")
    loopback=parsed.hostname.lower() in {"127.0.0.1","localhost","::1"}
    if not loopback and os.getenv("GCF_SCRIPT_DRAFTER_ALLOW_REMOTE","").strip().lower()!="true":
        raise ScriptDraftNotConfigured("Remote script-drafting endpoints are blocked by default; use loopback or explicitly permit remote inference after a privacy review.")
    model=os.getenv("GCF_SCRIPT_DRAFTER_MODEL","llama3.1:8b").strip()
    if not model:
        raise ScriptDraftNotConfigured("GCF_SCRIPT_DRAFTER_MODEL cannot be blank.")
    try: timeout=max(15,min(300,int(os.getenv("GCF_SCRIPT_DRAFTER_TIMEOUT_SECONDS","120"))))
    except (TypeError,ValueError): timeout=120
    return endpoint,model,timeout

def script_drafting_status() -> dict:
    enabled=os.getenv("GCF_ENABLE_SCRIPT_DRAFTING","").strip().lower()=="true"
    endpoint=os.getenv("GCF_SCRIPT_DRAFTER_BASE_URL","http://127.0.0.1:11434/api/chat").strip()
    parsed=urlparse(endpoint)
    valid=parsed.scheme=="http" and bool(parsed.hostname) and parsed.path.endswith("/api/chat")
    local=(parsed.hostname or "").lower() in {"127.0.0.1","localhost","::1"}
    remote_allowed=os.getenv("GCF_SCRIPT_DRAFTER_ALLOW_REMOTE","").strip().lower()=="true"
    try: model=os.getenv("GCF_SCRIPT_DRAFTER_MODEL","llama3.1:8b").strip()
    except Exception: model=""
    ready=enabled and valid and (local or remote_allowed) and bool(model)
    reason=None
    if not enabled: reason="Set GCF_ENABLE_SCRIPT_DRAFTING=true to enable local narration drafting."
    elif not valid: reason="Configure an Ollama-compatible /api/chat endpoint."
    elif not local and not remote_allowed: reason="Remote inference is disabled; use a local endpoint or explicitly allow it after privacy review."
    elif not model: reason="A model name is required."
    return {"enabled":enabled,"ready":ready,"model":model or "unknown","endpoint_local":local,
            "remote_allowed":remote_allowed,"timeout_seconds":max(15,min(300,int(os.getenv("GCF_SCRIPT_DRAFTER_TIMEOUT_SECONDS","120") or "120"))),
            "automatic_synthesis":False,"owner_review_required":True,"reason":reason}

def generate_narration_draft(*, game: str, source_title: str, candidate_duration: float,
                             transcript_segments: list[dict], vision: dict | None=None,
                             http_post=None) -> dict:
    endpoint,model,timeout=_config()
    if candidate_duration<=0 or candidate_duration>180:
        raise ValueError("Candidate duration must be positive and no longer than 180 seconds.")
    evidence=[]
    clean_segments=[]
    for item in transcript_segments[:80]:
        try:
            idx=int(item["segment_index"])
            start=float(item["start_seconds"]); end=float(item["end_seconds"])
            text=str(item["text"]).strip()
        except (KeyError,TypeError,ValueError):
            continue
        if not text or start<0 or end<=start:
            continue
        ref=f"transcript:{idx}"
        clean={"ref":ref,"start_seconds":round(start,3),"end_seconds":round(end,3),"text":text[:1000]}
        clean_segments.append(clean)
        evidence.append({"ref":ref,"quote":text[:1000]})
    clean_vision=None
    if isinstance(vision,dict) and vision.get("status")=="COMPLETED":
        try:
            vis_evidence=json.loads(vision.get("evidence_json") or "[]") if isinstance(vision.get("evidence_json"),str) else vision.get("evidence") or []
        except (ValueError,TypeError):
            vis_evidence=[]
        clean_vision={
            "model_name":str(vision.get("model_name","unknown"))[:120],
            "event_type":str(vision.get("event_type","unknown"))[:80],
            "visible_event":str(vision.get("visible_event",""))[:1000],
            "gameplay_context":str(vision.get("gameplay_context",""))[:1000],
            "highlight_score":vision.get("highlight_score"),
            "confidence":vision.get("confidence"),
            "evidence":[str(x)[:500] for x in vis_evidence[:8] if isinstance(x,str)],
            "quality_issues":[str(x)[:500] for x in (json.loads(vision.get("quality_issues_json") or "[]") if isinstance(vision.get("quality_issues_json"),str) else vision.get("quality_issues") or [])[:8] if isinstance(x,str)],
        }
        for index,quote in enumerate(clean_vision["evidence"]):
            evidence.append({"ref":f"vision:{index}","quote":quote})
        if clean_vision["visible_event"]:
            evidence.append({"ref":"vision:visible_event","quote":clean_vision["visible_event"]})
        if clean_vision["gameplay_context"]:
            evidence.append({"ref":"vision:gameplay_context","quote":clean_vision["gameplay_context"]})
    if not evidence:
        raise ValueError("Narration drafting needs stored transcript segments or a completed vision assessment with evidence.")
    packet={
        "game":(game or "unknown")[:120],
        "source_title":(source_title or "unknown")[:300],
        "candidate_duration_seconds":round(float(candidate_duration),3),
        "transcript_segments":clean_segments,
        "vision_assessment":clean_vision,
        "available_evidence_refs":[{"ref":e["ref"],"quote":e["quote"][:500]} for e in evidence],
    }
    system=(
        "You write a concise gaming Shorts voiceover draft for human review. Use only facts supported by the supplied transcript or visible-scene assessment. "
        "Never invent kills, clutches, wins, ranks, player motives, dialogue, game mechanics, timestamps, or results. Sparse-frame vision assessments are uncertain, "
        "and any model score is uncalibrated; do not convert a score into a factual claim. Do not add fabricated quotes or claim certainty beyond the evidence. "
        "If evidence is thin, write a neutral, short setup or return an empty script. Keep the script conversational, 1–3 sentences and appropriate for the candidate duration. "
        "Return JSON matching the schema. evidence_used must only reference supplied evidence refs and quotes must be literal substrings of the cited evidence. unsupported_claims should list any requested claim the evidence cannot support. "
        "This script is a DRAFT, not approved speech."
    )
    user="Draft one short voiceover for this clip using this evidence packet:\n"+json.dumps(packet,ensure_ascii=False)
    body={"model":model,"stream":False,"format":SCHEMA,"options":{"temperature":0},"messages":[
        {"role":"system","content":system},{"role":"user","content":user}
    ]}
    post=http_post or requests.post
    try:
        response=post(endpoint,json=body,timeout=(5,timeout))
        response.raise_for_status()
        raw=response.json()
    except requests.RequestException as exc:
        raise ScriptDraftError(f"Local script-drafting request failed ({type(exc).__name__}). Is the configured service/model running?") from exc
    except (ValueError,TypeError) as exc:
        raise ScriptDraftError("Script-drafting endpoint returned invalid JSON.") from exc
    try:
        result=json.loads((raw.get("message") or {}).get("content",""))
    except (AttributeError,ValueError,TypeError) as exc:
        raise ScriptDraftError("Model did not return a JSON script draft.") from exc
    if not isinstance(result,dict):
        raise ScriptDraftError("Script-drafting result must be a JSON object.")
    script=result.get("script"); hook=result.get("hook")
    if not isinstance(script,str) or not isinstance(hook,str):
        raise ScriptDraftError("Draft script and hook must be text.")
    script=" ".join(script.split()); hook=" ".join(hook.split())
    if len(script)>1500 or len(hook)>180:
        raise ScriptDraftError("Draft script exceeds the configured maximum length.")
    if not isinstance(result.get("evidence_used"),list) or not isinstance(result.get("unsupported_claims"),list):
        raise ScriptDraftError("Evidence and unsupported_claims must be arrays.")
    evidence_map={x["ref"]:x["quote"] for x in evidence}
    used=[]
    for item in result["evidence_used"][:8]:
        if not isinstance(item,dict) or not isinstance(item.get("ref"),str) or not isinstance(item.get("quote"),str):
            raise ScriptDraftError("Each evidence reference must include text ref and quote fields.")
        ref=item["ref"]; quote=" ".join(item["quote"].split())
        if ref not in evidence_map or not quote or quote.casefold() not in evidence_map[ref].casefold():
            raise ScriptDraftError("Model cited evidence that was not present in the supplied transcript/vision evidence.")
        used.append({"ref":ref,"quote":quote[:500]})
    unsupported=[]
    for item in result["unsupported_claims"][:8]:
        if isinstance(item,str) and item.strip():
            unsupported.append(" ".join(item.split())[:400])
    if script and not used:
        raise ScriptDraftError("A non-empty narration draft must cite at least one verified evidence fragment.")
    return {
        "script":script,
        "hook":hook,
        "evidence_used":used,
        "unsupported_claims":unsupported,
        "model_name":model,
        "status":"GENERATED_UNREVIEWED",
        "owner_review_required":True,
        "automatic_synthesis":False,
        "strategy_version":"local-script-drafter-v1",
    }
