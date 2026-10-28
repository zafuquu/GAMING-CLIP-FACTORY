from pathlib import Path
from datetime import datetime, timezone
import json, shutil, sqlite3, subprocess, sys
import pytest
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from app.config import Settings
from app.main import create_app
from app.worker import Worker
import app.worker as worker_module

def test_candidate_vision_extracts_frames_and_saves_evidence_without_auto_approval(tmp_path,monkeypatch):
    ffmpeg=shutil.which("ffmpeg")
    if not ffmpeg: pytest.skip("FFmpeg not installed")
    monkeypatch.setenv("GCF_ENABLE_VISION_ANALYSIS","true")
    # This regression covers the optional five-frame path explicitly; the
    # low-memory application default is now three sampled frames.
    monkeypatch.setenv("GCF_VISION_SAMPLE_FRAMES","5")
    root=tmp_path/"project"; data=root/"data"; media=data/"media"
    cfg=Settings(root,data,media,data/"vision.sqlite3",ffmpeg,None,60)
    with TestClient(create_app(settings=cfg,start_worker=False)) as client:
        sid_resp=client.post("/api/sources",json={"source_url":"https://example.com/vision-fixture","platform":"test","title":"Synthetic test match","game":"Valorant"})
        assert sid_resp.status_code==201,sid_resp.text
        sid=sid_resp.json()["id"]
        rights=client.put(f"/api/sources/{sid}/rights",json={"status":"AUTHORIZED","license_type":"Synthetic CI permission","permission_evidence":"Generated synthetic frames, no third-party content","editing_allowed":True,"commercial_use_allowed":True,"reviewed_by_owner":True})
        assert rights.status_code==200
        media.mkdir(parents=True,exist_ok=True)
        video=media/"gameplay.mp4"
        command=[ffmpeg,"-hide_banner","-loglevel","error","-nostdin","-f","lavfi","-i","testsrc=size=320x180:rate=12:duration=5","-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p","-y",str(video)]
        subprocess.run(command,capture_output=True,text=True,timeout=30,check=True)
        imported=client.post("/api/media/import",json={"source_id":sid,"path":"gameplay.mp4"})
        assert imported.status_code==201,imported.text
        mid=imported.json()["id"]
        cid="vision-ci-candidate"; jid="vision-ci-job"; stamp=datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(cfg.database_path) as db:
            db.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",(cid,mid,0.25,4.75,"test_fixture","Test only","NEEDS_REVIEW",stamp))
            # Event time is an absolute source timestamp; vision sampling should
            # include it closely while retaining early/late context frames.
            db.execute("UPDATE candidate_moments SET ranking_components_json=? WHERE id=?",(json.dumps({"event_time_seconds":2.0}),cid))
            db.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(jid,"ANALYZE_CANDIDATE_VISION","RUNNING",sid,mid,json.dumps({"candidate_id":cid}),stamp,stamp))
        captured={}
        def fake_assessment(images,game,source_title,transcript_excerpt="",frame_times_seconds=None):
            captured.update({"frame_count":len(images),"image_sizes":[len(x) for x in images],"game":game,"title":source_title,"relative_frame_times_seconds":frame_times_seconds})
            return {"model_name":"fake-local-vision","gameplay_context":"FPS practice footage","event_type":"unknown","visible_event":"No specific event is established by the sampled still frames.","is_actionable_highlight":False,"highlight_score":0.12,"confidence":0.88,"evidence":["The sampled frames do not establish a distinct event."],"quality_issues":["Sparse screenshots cannot prove event timing."],"score_calibrated":False}
        monkeypatch.setattr(worker_module,"assess_candidate_images",fake_assessment)
        worker=Worker(cfg.database_path,ffmpeg,None,60)
        worker.analyze_candidate_vision({"id":jid,"payload_json":json.dumps({"candidate_id":cid})})
        assert captured["frame_count"]==5
        assert captured["relative_frame_times_seconds"]==[0.45,1.39,1.75,2.11,4.05]
        assert all(size>0 for size in captured["image_sizes"])
        assert captured["game"]=="Valorant"
        with sqlite3.connect(cfg.database_path) as db:
            visual=db.execute("SELECT status,model_name,event_type,highlight_score,confidence FROM candidate_vision_assessments WHERE candidate_id=?",(cid,)).fetchone()
            ranking=db.execute("SELECT ranking_score,state,ranking_components_json FROM candidate_moments WHERE id=?",(cid,)).fetchone()
            job=db.execute("SELECT status FROM jobs WHERE id=?",(jid,)).fetchone()
        assert tuple(visual)==("COMPLETED","fake-local-vision","unknown",0.12,0.88)
        assert 0<=ranking[0]<=1
        assert ranking[1]=="NEEDS_REVIEW"
        assert json.loads(ranking[2])["vision_score_calibrated"] is False
        assert json.loads(ranking[2])["vision_sample_count"]==5
        assert json.loads(ranking[2])["vision_relative_frame_times_seconds"]==[0.45,1.39,1.75,2.11,4.05]
        assert job[0]=="COMPLETED"
        listed=client.get("/api/candidates").json()
        candidate=next(x for x in listed if x["id"]==cid)
        assert candidate["vision_assessment"]["visible_event"].startswith("No specific event")
        assert candidate["state"]=="NEEDS_REVIEW"
