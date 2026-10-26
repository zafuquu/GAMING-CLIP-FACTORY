import json, re, subprocess, threading, hashlib
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
from .db import connect, now, event

def expiry_passed(value):
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True

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
        try:
            if job["job_type"] == "ANALYZE_MEDIA":
                self.analyze(job)
            elif job["job_type"] == "RENDER_CANDIDATE":
                self.render_candidate(job)
            else:
                raise RuntimeError(f"Unsupported job type: {job['job_type']}")
        except Exception as e:
            c=connect(self.db)
            try:
                r=c.execute("SELECT attempt_count,max_attempts FROM jobs WHERE id=?",(job["id"],)).fetchone()
                status="QUEUED" if r and r["attempt_count"]<r["max_attempts"] and not isinstance(e,PermissionError) else "FAILED"
                t=now(); c.execute("UPDATE jobs SET status=?,error=?,updated_at=?,finished_at=? WHERE id=?",(status,str(e)[:3000],t,t if status=="FAILED" else None,job["id"]))
                if status == "FAILED" and job["job_type"] == "RENDER_CANDIDATE":
                    try:
                        cid=json.loads(job["payload_json"]).get("candidate_id")
                        if cid: c.execute("UPDATE candidate_moments SET state='RENDER_FAILED' WHERE id=? AND state='APPROVED'",(cid,))
                    except (ValueError, TypeError):
                        pass
                event(c,"ERROR","job.failed" if status=="FAILED" else "job.retry_queued",str(e),{"job_id":job["id"]}); c.commit()
            finally: c.close()
        return True
    def analyze(self,job):
        if not self.ffmpeg or not self.ffprobe: raise RuntimeError("FFmpeg/FFprobe not configured. Set GCF_FFMPEG_PATH and GCF_FFPROBE_PATH.")
        c=connect(self.db)
        try:
            m=c.execute("SELECT m.*,p.status AS rights_status,p.expires_at FROM media_assets m JOIN source_permissions p ON p.source_id=m.source_id WHERE m.id=?",(job["media_id"],)).fetchone()
            if not m: raise FileNotFoundError("Media record missing")
            if m["rights_status"]!="AUTHORIZED" or expiry_passed(m["expires_at"]): raise PermissionError("Rights gate blocked analysis: current AUTHORIZED permission is required")
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

    def render_candidate(self, job):
        if not self.ffmpeg or not self.ffprobe:
            raise RuntimeError("FFmpeg/FFprobe not configured.")
        payload=json.loads(job["payload_json"] or "{}")
        cid=payload.get("candidate_id")
        if not cid:
            raise RuntimeError("Render job is missing candidate_id.")
        c=connect(self.db)
        try:
            row=c.execute("""SELECT cm.*,m.original_path,m.file_name,m.source_id,p.status AS rights_status,p.expires_at
                FROM candidate_moments cm
                JOIN media_assets m ON m.id=cm.media_id
                JOIN source_permissions p ON p.source_id=m.source_id
                WHERE cm.id=?""",(cid,)).fetchone()
            if not row:
                raise FileNotFoundError("Candidate no longer exists.")
            if row["rights_status"] != "AUTHORIZED" or expiry_passed(row["expires_at"]):
                raise PermissionError("Rights gate blocked rendering: current AUTHORIZED permission is required.")
            if row["state"] not in {"APPROVED","RENDER_FAILED"}:
                raise RuntimeError(f"Candidate is not approved for rendering (state={row['state']}).")
            source=Path(row["original_path"])
            if not source.is_file():
                raise FileNotFoundError("Source media file is missing.")
            start=float(row["start_seconds"]); end=float(row["end_seconds"])
            if start < 0 or end <= start:
                raise RuntimeError("Candidate has invalid time boundaries.")
            probe=run([self.ffprobe,"-v","error","-print_format","json","-show_streams","-show_format",str(source)],min(60,self.timeout))
            source_data=json.loads(probe.stdout)
            source_duration=float(source_data.get("format",{}).get("duration",0))
            if source_duration <= 0 or end > source_duration + 0.05:
                raise RuntimeError("Candidate interval exceeds verified source duration.")
            source_has_audio=any(x.get("codec_type")=="audio" for x in source_data.get("streams",[]))
            output_dir=self.db.parent/"outputs"
            output_dir.mkdir(parents=True,exist_ok=True)
            output=output_dir/(cid+"_"+uuid4().hex[:12]+".mp4")
            if output.exists():
                raise FileExistsError("Unique output path unexpectedly already exists.")
            # Preserve the complete gameplay frame in a fitted portrait canvas; no forced center crop.
            vf="scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black"
            args=[self.ffmpeg,"-hide_banner","-nostdin","-n","-ss",f"{start:.3f}","-i",str(source),"-t",f"{end-start:.3f}","-map","0:v:0","-map","0:a?","-vf",vf,"-c:v","libx264","-preset","veryfast","-crf","21","-pix_fmt","yuv420p","-c:a","aac","-b:a","128k","-movflags","+faststart",str(output)]
            run(args,self.timeout)
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError("FFmpeg reported success but produced no nonempty file.")
            rendered_probe=run([self.ffprobe,"-v","error","-print_format","json","-show_streams","-show_format",str(output)],min(60,self.timeout))
            details=json.loads(rendered_probe.stdout)
            streams=details.get("streams") or []
            video=next((x for x in streams if x.get("codec_type")=="video"),None)
            audio=next((x for x in streams if x.get("codec_type")=="audio"),None)
            duration=float(details.get("format",{}).get("duration",0))
            expected=end-start
            if not video or video.get("width")!=1080 or video.get("height")!=1920:
                raise RuntimeError("QC failed: output is missing expected 1080x1920 video.")
            if not (max(0.5,expected-0.5) <= duration <= expected+0.5):
                raise RuntimeError(f"QC failed: output duration {duration:.3f}s differs from expected {expected:.3f}s.")
            if source_has_audio and not audio:
                raise RuntimeError("QC failed: source contains audio but output audio stream is missing.")
            h=hashlib.sha256()
            with output.open("rb") as handle:
                for block in iter(lambda:handle.read(1024*1024),b""):
                    h.update(block)
            report={"passed":True,"checks":{"nonempty":True,"video_stream":True,"dimensions":"1080x1920","duration_seconds":duration,"source_audio_present":source_has_audio,"output_audio_present":bool(audio)},"layout":"fit-and-pad-no-crop","warnings":[] if audio or not source_has_audio else ["Source had no audio stream."]}
            out_id=str(uuid4()); t=now()
            c.execute("""INSERT INTO rendered_outputs(id,candidate_id,media_id,output_path,file_size,sha256,duration_seconds,width,height,video_codec,audio_codec,qc_status,qc_report_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(out_id,cid,row["media_id"],str(output),output.stat().st_size,h.hexdigest(),duration,video["width"],video["height"],video.get("codec_name","unknown"),audio.get("codec_name") if audio else None,"PASS",json.dumps(report),t))
            c.execute("UPDATE candidate_moments SET state='RENDERED_QC_PASSED' WHERE id=?",(cid,))
            summary={"rendered_output_id":out_id,"output_path":str(output),"file_size":output.stat().st_size,"sha256":h.hexdigest(),"qc_status":"PASS","qc_report":report}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",(json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","render.qc_passed","Candidate rendered and passed deterministic QC.",{"job_id":job["id"],"candidate_id":cid,"output_id":out_id,"duration_seconds":duration})
            c.commit()
        finally:
            c.close()

