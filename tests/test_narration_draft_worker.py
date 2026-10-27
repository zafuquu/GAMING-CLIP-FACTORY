from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import sys
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from app.config import Settings
from app.main import create_app
from app.worker import Worker
import app.worker as worker_module

def _fixture(tmp_path, with_transcript=True):
    root=tmp_path/"project"; data=root/"data"; media=data/"media"
    media.mkdir(parents=True,exist_ok=True)
    cfg=Settings(root,data,media,data/"script-drafts.sqlite3",None,None,5)
    client=TestClient(create_app(settings=cfg,start_worker=False))
    client.__enter__()
    source=client.post("/api/sources",json={"source_url":"https://example.com/script-draft","platform":"test","title":"Authorized match","game":"Valorant"})
    assert source.status_code==201,source.text
    sid=source.json()["id"]
    rights=client.put(f"/api/sources/{sid}/rights",json={"status":"AUTHORIZED","license_type":"Synthetic fixture permission","permission_evidence":"Test fixture only","editing_allowed":True,"commercial_use_allowed":True,"reviewed_by_owner":True})
    assert rights.status_code==200,rights.text
    (media/"source.mp4").write_bytes(b"synthetic test media bytes")
    imported=client.post("/api/media/import",json={"source_id":sid,"path":"source.mp4"})
    assert imported.status_code==201,imported.text
    mid=imported.json()["id"]
    cid="candidate-script-draft"
    stamp=datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("""INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",(cid,mid,0.0,8.0,"test","Synthetic review candidate","NEEDS_REVIEW",stamp))
        if with_transcript:
            db.execute("""INSERT INTO transcripts(id,media_id,language,language_probability,full_text,model_name,status,error,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'COMPLETED','',?,?)""",
                ("transcript-script-draft",mid,"en",0.95,"Watch this angle","synthetic-test-model",stamp,stamp))
            db.execute("""INSERT INTO transcript_segments(id,transcript_id,segment_index,start_seconds,end_seconds,text,confidence)
                VALUES(?,?,?,?,?,?,?)""",("segment-script-draft","transcript-script-draft",2,1.2,4.6,"Watch this angle",0.9))
        db.execute("""INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at)
            VALUES(?,?, 'QUEUED',?,?, ?,?,?)""",
            ("draft-script-job","DRAFT_NARRATION_SCRIPT",sid,mid,json.dumps({"candidate_id":cid}),stamp,stamp))
    return client,cfg,sid,mid,cid

def _fake_result():
    return {
        "script":"Watch this angle, then reset.",
        "hook":"Watch this angle.",
        "evidence_used":[{"ref":"transcript:2","quote":"Watch this angle"}],
        "unsupported_claims":[],
        "model_name":"mock-local-script-model",
        "status":"GENERATED_UNREVIEWED",
        "owner_review_required":True,
        "automatic_synthesis":False,
        "strategy_version":"local-script-drafter-v1",
    }

def test_worker_saves_local_script_draft_without_overwriting_or_approving_script(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_SCRIPT_DRAFTING","true")
    client,cfg,sid,mid,cid=_fixture(tmp_path,with_transcript=True)
    monkeypatch.setattr(worker_module,"generate_narration_draft",lambda **kwargs:_fake_result())
    assert Worker(cfg.database_path,None,None,5).one() is True
    with sqlite3.connect(cfg.database_path) as db:
        db.row_factory=sqlite3.Row
        plan=db.execute("SELECT narration_text,narration_script_reviewed,narration_draft_text,narration_draft_status,narration_draft_model,narration_draft_evidence_json,narration_audio_status FROM content_plans WHERE candidate_id=?",(cid,)).fetchone()
        job=db.execute("SELECT status,payload_json FROM jobs WHERE id='draft-script-job'").fetchone()
    assert plan is not None
    assert plan["narration_text"]==""
    assert plan["narration_script_reviewed"]==0
    assert plan["narration_draft_text"]=="Watch this angle, then reset."
    assert plan["narration_draft_status"]=="GENERATED_UNREVIEWED"
    assert plan["narration_draft_model"]=="mock-local-script-model"
    assert "transcript:2" in plan["narration_draft_evidence_json"]
    assert plan["narration_audio_status"]=="NOT_GENERATED"
    assert job["status"]=="COMPLETED"
    assert json.loads(job["payload_json"])["owner_review_required"] is True
    client.__exit__(None,None,None)

def test_worker_discards_script_draft_if_rights_change_during_inference(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_SCRIPT_DRAFTING","true")
    client,cfg,sid,mid,cid=_fixture(tmp_path,with_transcript=True)
    def fake(**kwargs):
        with sqlite3.connect(cfg.database_path) as db:
            db.execute("UPDATE source_permissions SET status='REVOKED' WHERE source_id=?",(sid,))
        return _fake_result()
    monkeypatch.setattr(worker_module,"generate_narration_draft",fake)
    assert Worker(cfg.database_path,None,None,5).one() is True
    with sqlite3.connect(cfg.database_path) as db:
        db.row_factory=sqlite3.Row
        plan=db.execute("SELECT narration_draft_text,narration_draft_status,narration_script_reviewed FROM content_plans WHERE candidate_id=?",(cid,)).fetchone()
        job=db.execute("SELECT status,error FROM jobs WHERE id='draft-script-job'").fetchone()
    assert plan["narration_draft_text"]==""
    assert plan["narration_draft_status"]=="FAILED"
    assert plan["narration_script_reviewed"]==0
    assert job["status"]=="FAILED"
    assert "Rights expired or were revoked" in job["error"]
    client.__exit__(None,None,None)


def test_worker_keeps_existing_owner_script_and_review_state(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_SCRIPT_DRAFTING","true")
    client,cfg,sid,mid,cid=_fixture(tmp_path,with_transcript=True)
    stamp=datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("""INSERT INTO content_plans(id,candidate_id,chosen_format,title,narration_text,narration_script_reviewed,
            created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
            ("existing-owner-plan",cid,"gameplay_highlight","Owner title","Existing owner-approved script.",1,stamp,stamp))
    monkeypatch.setattr(worker_module,"generate_narration_draft",lambda **kwargs:_fake_result())
    assert Worker(cfg.database_path,None,None,5).one() is True
    with sqlite3.connect(cfg.database_path) as db:
        db.row_factory=sqlite3.Row
        plan=db.execute("SELECT narration_text,narration_script_reviewed,narration_draft_text,narration_draft_status FROM content_plans WHERE candidate_id=?",(cid,)).fetchone()
    assert plan["narration_text"]=="Existing owner-approved script."
    assert plan["narration_script_reviewed"]==1
    assert plan["narration_draft_text"]=="Watch this angle, then reset."
    assert plan["narration_draft_status"]=="GENERATED_UNREVIEWED"
    client.__exit__(None,None,None)
