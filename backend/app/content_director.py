"""Deterministic, transcript-grounded content planning and explainable ranking."""
from __future__ import annotations
import re

FORMAT_RULES = [
    ("clutch_candidate", ("clutch", "1v1", "1v2", "1v3", "1v4", "1v5", "last alive", "last player", "ace", "overtime")),
    ("educational_breakdown", ("tip", "you should", "you need", "because", "watch this", "look at", "instead", "never do", "always do")),
    ("reaction_moment", ("haha", "hahaha", "lol", "lmao", "bruh", "no way", "what", "oh my god", "lets go", "let's go")),
    ("action_highlight", ("headshot", "knocked", "eliminated", "kill", "enemy", "nice shot", "team wipe", "victory")),
]

def plan_candidate(game: str, source_title: str, candidate_start: float, candidate_end: float, transcript_segments: list[dict]) -> dict:
    relevant=[s for s in transcript_segments if float(s.get("end_seconds",0))>candidate_start and float(s.get("start_seconds",0))<candidate_end and str(s.get("text","")).strip()]
    evidence_text=" ".join(str(s["text"]).strip() for s in relevant)
    lowered=evidence_text.casefold()
    matched=[]
    chosen="gameplay_highlight"
    for format_name,signals in FORMAT_RULES:
        found=[word for word in signals if word in lowered]
        if found:
            chosen=format_name
            matched=found
            break
    excerpt=re.sub(r"\s+"," ",evidence_text).strip()
    hook=excerpt[:90].rstrip(" ,.;:-") if excerpt else ""
    game_label=(game or "").strip()
    if game_label:
        title=f"{game_label} | {chosen.replace('_',' ').title()}"
    elif source_title.strip():
        title=f"{source_title.strip()[:70]} | Gameplay highlight"
    else:
        title="Gameplay highlight"
    title=title[:100]
    description_parts=[]
    if game_label: description_parts.append(f"Gameplay from {game_label}.")
    if chosen!="gameplay_highlight": description_parts.append(f"Format suggestion: {chosen.replace('_',' ')}.")
    if excerpt: description_parts.append("Transcript excerpt: "+excerpt[:240])
    description_parts.append("This is an AI-assisted draft. Verify the clip, transcript, permissions, attribution, and metadata before publishing.")
    tags=list(dict.fromkeys(([game_label] if game_label else [])+[chosen.replace("_"," "),"gaming","gameplay"]))[:10]
    duration=max(0.0,candidate_end-candidate_start)
    components={
        "transcript_available":bool(excerpt),
        "matched_transcript_signals":matched,
        "duration_seconds":round(duration,3),
        "source_title_available":bool(source_title.strip()),
        "method":"deterministic-transcript-rules-v1",
    }
    score=0.30
    if excerpt: score+=0.22
    score+=min(0.25,0.08*len(matched))
    if 8.0<=duration<=35.0: score+=0.12
    if game_label: score+=0.06
    score=round(min(0.93,score),3)
    return {
        "chosen_format":chosen,
        "hook_text":hook,
        "title":title,
        "description":" ".join(description_parts),
        "tags":tags,
        "narration_text":"",
        "evidence":{"matched_signals":matched,"transcript_excerpt":excerpt[:1000],"transcript_segment_count":len(relevant),"claim_policy":"No match signal is treated as proof that an event happened; owner review is required."},
        "confidence":score,
        "ranking_components":components,
        "strategy_version":"rules-v1",
    }
