import json, re, subprocess, threading
from pathlib import Path
from uuid import uuid4
from .db import connect, now, event

SCENE=re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
def run(args,timeout):
    try: p=subprocess.run(args,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=timeout,shell=False)
    except subprocess.TimeoutExpired as e: raise RuntimeError(f"Command timed out after {timeout}s: {args[0]}") from e
    if p.returncode: raise RuntimeError(f"{Path(args[0]).name} exited {p.returncode}: {(p.stderr or p.stdout)[-1500:]}")
    return p
class Worker(threading.Thread):
    def __init__(self,db,ffmpeg,ffprobe,timeout=120):
        super().__init__(daemon=True,name="gcf-worker"); self.db=db; self.ffmpeg=ffmpeg; self.ffprobe=ffprobe; self.timeout=timeout; self.stop_event=threading.Event()
    def stop(self): self.stop_event.set()
    def run(self):
        while not self.stop_event.is_set():
            try: worked=self.one()
            except Exception as e:
                c=connect(self.db)
                try: event(c,"ERROR","worker.error",str(e)); c.commit()
                finally: c.close()
                worked=False
            if not worked: self.stop_event.wait(1)
    def one(self):
        c=connect(self.db); job=None
        try:
            c.execute("BEGIN IMMEDIATE")
            p=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if p and p["value"]=="true": c.rollback(); return False
            job=c.execute("SELECT * FROM jobs WHERE status='QUEUED' ORDER BY created_at LIMIT 1").fetchone()
            if not job: c.rollback(); return False
            c.execute("UPDATE jobs SET status='RUNNING',attempt_count=attempt_count+1,started_at=?,updated_at=?,error='' WHERE id=?",(now(),now(),job["id"])); c.commit()
        finally: c.close()
        try: self.analyze(job)
        except Exception as e:
            c=connect(self.db)
            try:
                r=c.execute("SELECT attempt_count,max_attempts FROM jobs WHERE id=?",(job["id"],)).fetchone()
                status="QUEUED" if r and r["attempt_count"]<r["max_attempts"] and not isinstance(e,PermissionError) else "FAILED"
                t=now(); c.execute("UPDATE jobs SET status=?,error=?,updated_at=?,finished_at=? WHERE id=?",(status,str(e)[:3000],t,t if status=="FAILED" else None,job["id"]))
                event(c,"ERROR","job.failed" if status=="FAILED" else "job.retry_queued",str(e),{"job_id":job["id"]}); c.commit()
            finally: c.close()
        return True
    def analyze(self,job):
        if not self.ffmpeg or not self.ffprobe: raise RuntimeError("FFmpeg/FFprobe not configured. Set GCF_FFMPEG_PATH and GCF_FFPROBE_PATH.")
        c=connect(self.db)
        try:
            m=c.execute("SELECT m.*,p.status AS rights_status FROM media_assets m JOIN source_permissions p ON p.source_id=m.source_id WHERE m.id=?",(job["media_id"],)).fetchone()
            if not m: raise FileNotFoundError("Media record missing")
            if m["rights_status"]!="AUTHORIZED": raise PermissionError("Rights gate blocked analysis")
            path=Path(m["original_path"])
            if not path.is_file(): raise FileNotFoundError("Media file missing")
            probe=run([self.ffprobe,"-v","error","-print_format","json","-show_streams","-show_format",str(path)],min(60,self.timeout))
            data=json.loads(probe.stdout); fmt=data.get("format",{}); duration=float(fmt.get("duration",0))
            if duration<=0: raise RuntimeError("Could not verify positive media duration")
            meta={"format_name":fmt.get("format_name"),"duration_seconds":duration,"size_bytes":path.stat().st_size,"streams":[{"type":s.get("codec_type"),"codec":s.get("codec_name"),"width":s.get("width"),"height":s.get("height"),"fps":s.get("avg_frame_rate")} for s in data.get("streams",[])]}
            c.execute("UPDATE media_assets SET probe_json=?,state='PROBED' WHERE id=?",(json.dumps(meta),m["id"]))
            p=run([self.ffmpeg,"-hide_banner","-nostdin","-i",str(path),"-vf","select='gt(scene,0.35)',showinfo","-an","-f","null","-"],self.timeout)
            ts=[]
            for hit in SCENE.finditer(p.stderr):
                t=float(hit.group(1))
                if 0<t<duration and (not ts or abs(t-ts[-1])>1): ts.append(t)
            added=0
            for t in ts:
                start=round(max(0,t-5),3); end=round(min(duration,t+5),3)
                if end-start<3: continue
                cur=c.execute("INSERT OR IGNORE INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,? ,?,'NEEDS_REVIEW',?)",(str(uuid4()),m["id"],start,end,"ffmpeg_scene_change","Scene-change heuristic only; human review required.",now()))
                added+=cur.rowcount
            summary={"duration_seconds":duration,"candidate_count":added,"note":"Scene changes are not semantic highlight detection."}; t=now()
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",(json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","media.analysis_completed",f"Created {added} scene-change review candidates.",{"job_id":job["id"],"media_id":m["id"],**summary}); c.commit()
        finally: c.close()
