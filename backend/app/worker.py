import json, re, subprocess, threading, hashlib, os
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
from .db import connect, now, event
from .integrations.twitch import TwitchHelix
from .integrations.youtube import upload_video, fetch_video_analytics, UploadReconciliationRequired, YouTubeNotConfigured

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
            job=c.execute("SELECT * FROM jobs WHERE status='QUEUED' AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY created_at LIMIT 1",(now(),)).fetchone()
            if job:
                c.execute("UPDATE jobs SET status='RUNNING',attempt_count=attempt_count+1,started_at=?,updated_at=?,error='',next_attempt_at=NULL WHERE id=?",(now(),now(),job["id"])); c.commit()
            else:
                c.rollback()
        finally: c.close()
        if not job:
            self.schedule_due_discovery()
            return False
        try:
            if job["job_type"] == "ANALYZE_MEDIA":
                self.analyze(job)
            elif job["job_type"] == "RENDER_CANDIDATE":
                self.render_candidate(job)
            elif job["job_type"] == "DISCOVER_TWITCH_CLIPS":
                self.discover_twitch(job)
            elif job["job_type"] == "UPLOAD_YOUTUBE":
                self.upload_youtube(job)
            elif job["job_type"] == "SYNC_YOUTUBE_ANALYTICS":
                self.sync_youtube_analytics(job)
            else:
                raise RuntimeError(f"Unsupported job type: {job['job_type']}")
        except Exception as e:
            c=connect(self.db)
            try:
                r=c.execute("SELECT attempt_count,max_attempts FROM jobs WHERE id=?",(job["id"],)).fetchone()
                t=now()
                if isinstance(e, UploadReconciliationRequired):
                    status="RECONCILE_REQUIRED"
                    next_attempt=None
                    payload=json.loads(job["payload_json"] or "{}")
                    published_id=payload.get("published_video_id")
                    if published_id:
                        c.execute("UPDATE published_videos SET publishing_status='RECONCILE_REQUIRED',updated_at=? WHERE id=?",(t,published_id))
                else:
                    retryable = not isinstance(e,PermissionError) and not isinstance(e,ValueError) and not isinstance(e,YouTubeNotConfigured)
                    should_retry = bool(r and r["attempt_count"]<r["max_attempts"] and retryable)
                    status="QUEUED" if should_retry else "FAILED"
                    next_attempt = None
                    if should_retry:
                        import datetime as dt
                        delay=min(300, 2 ** min(int(r["attempt_count"]), 8))
                        next_attempt=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=delay)).isoformat(timespec="seconds")
                c.execute("UPDATE jobs SET status=?,error=?,updated_at=?,finished_at=?,next_attempt_at=? WHERE id=?",(status,str(e)[:3000],t,t if status in {"FAILED","RECONCILE_REQUIRED"} else None,next_attempt,job["id"]))
                if status == "FAILED" and job["job_type"] == "RENDER_CANDIDATE":
                    try:
                        cid=json.loads(job["payload_json"]).get("candidate_id")
                        if cid: c.execute("UPDATE candidate_moments SET state='RENDER_FAILED' WHERE id=? AND state='APPROVED'",(cid,))
                    except (ValueError, TypeError):
                        pass
                if status == "FAILED" and job["job_type"] == "UPLOAD_YOUTUBE":
                    try:
                        published_id=json.loads(job["payload_json"] or "{}").get("published_video_id")
                        if published_id: c.execute("UPDATE published_videos SET publishing_status='FAILED',updated_at=? WHERE id=? AND publishing_status='UPLOADING'",(t,published_id))
                    except (ValueError, TypeError):
                        pass
                event(c,"ERROR","job.reconciliation_required" if status=="RECONCILE_REQUIRED" else "job.failed" if status=="FAILED" else "job.retry_queued",str(e),{"job_id":job["id"]}); c.commit()
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


    def schedule_due_discovery(self):
        """Queue one scheduled discovery job when configured and due; never downloads media."""
        c=connect(self.db)
        try:
            values={r["key"]:r["value"] for r in c.execute("SELECT key,value FROM settings WHERE key IN ('global_paused','twitch_discovery_enabled','twitch_discovery_queries','twitch_discovery_interval_minutes','twitch_discovery_next_at')")}
            if values.get("global_paused")=="true" or values.get("twitch_discovery_enabled")!="true":
                return
            if not os.getenv("GCF_TWITCH_CLIENT_ID") or not os.getenv("GCF_TWITCH_CLIENT_SECRET"):
                return
            raw=values.get("twitch_discovery_queries","[]")
            try: queries=json.loads(raw)
            except ValueError: return
            if not isinstance(queries,list) or not queries:
                return
            next_at=values.get("twitch_discovery_next_at")
            if next_at:
                try:
                    due=datetime.fromisoformat(next_at.replace("Z","+00:00"))
                    if due.tzinfo is None: due=due.replace(tzinfo=timezone.utc)
                    if due>datetime.now(timezone.utc): return
                except ValueError:
                    pass
            jid=str(uuid4()); t=now()
            c.execute("INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at) VALUES(?,'DISCOVER_TWITCH_CLIPS','QUEUED',?,?,?)",(jid,json.dumps({"game_queries":queries,"days":7,"max_results":100}),t,t))
            minutes=max(15,min(1440,int(values.get("twitch_discovery_interval_minutes","60"))))
            next_time=(datetime.now(timezone.utc)+__import__("datetime").timedelta(minutes=minutes)).isoformat(timespec="seconds")
            c.execute("UPDATE settings SET value=?,updated_at=? WHERE key='twitch_discovery_next_at'",(next_time,t))
            event(c,"INFO","discovery.scheduled_job_queued","Scheduled Twitch metadata discovery job queued.",{"job_id":jid,"queries":queries,"next_run":next_time})
            c.commit()
        finally:
            c.close()

    def discover_twitch(self, job):
        payload=json.loads(job["payload_json"] or "{}")
        queries=payload.get("game_queries",[])
        if not isinstance(queries,list) or not queries:
            raise ValueError("Twitch discovery job requires one or more game_queries.")
        provider=TwitchHelix()
        c=connect(self.db)
        discovered=0; inserted=0; outcomes=[]
        try:
            for raw_query in queries[:20]:
                query=str(raw_query).strip()[:120]
                if not query: continue
                run_id=str(uuid4()); started=now()
                c.execute("INSERT INTO discovery_runs(id,provider,query,status,started_at) VALUES(?,?,?,?,?)",(run_id,"twitch_helix",query,"RUNNING",started)); c.commit()
                try:
                    items=provider.discover_game_clips(query,days=int(payload.get("days",7)),max_results=int(payload.get("max_results",100)))
                    added=0
                    for item in items:
                        discovered+=1
                        external_id=item.get("provider_clip_id") or None
                        cur=c.execute("""INSERT OR IGNORE INTO sources(id,source_url,platform,title,creator,game,discovered_at,created_at,external_source_id,provider_metadata_json)
                            VALUES(?,?,?,?,?,?,?,?,?,?)""",(str(uuid4()),item["url"],"twitch",item["title"],item["creator"],item["game"],now(),now(),external_id,json.dumps(item)))
                        if cur.rowcount:
                            sid=c.execute("SELECT id FROM sources WHERE source_url=?",(item["url"],)).fetchone()["id"]
                            c.execute("INSERT OR IGNORE INTO source_permissions(source_id,status,updated_at) VALUES(?,'UNKNOWN',?)",(sid,now()))
                            added+=1; inserted+=1
                    finished=now()
                    c.execute("UPDATE discovery_runs SET status='COMPLETED',found_count=?,inserted_count=?,finished_at=? WHERE id=?",(len(items),added,finished,run_id))
                    outcomes.append({"query":query,"found":len(items),"inserted":added})
                    c.commit()
                except Exception as exc:
                    c.execute("UPDATE discovery_runs SET status='FAILED',error=?,finished_at=? WHERE id=?",(str(exc)[:1500],now(),run_id)); c.commit()
                    raise
            t=now()
            summary={"provider":"twitch_helix","queries":outcomes,"found_total":discovered,"new_sources":inserted,"rights_status":"UNKNOWN","media_downloaded":False}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",(json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","discovery.completed",f"Discovered metadata for {discovered} Twitch clips; inserted {inserted} new sources.",{"job_id":job["id"],**summary}); c.commit()
        finally:
            c.close()

    def upload_youtube(self, job):
        payload=json.loads(job["payload_json"] or "{}")
        published_id=payload.get("published_video_id")
        if not published_id:
            raise ValueError("Upload job is missing published_video_id.")
        c=connect(self.db)
        try:
            row=c.execute("""SELECT pv.*,ro.qc_status AS render_qc,ro.sha256 AS render_sha,ro.file_size AS render_size,
                m.source_id,p.status AS rights_status,p.expires_at
                FROM published_videos pv JOIN rendered_outputs ro ON ro.id=pv.rendered_output_id
                JOIN media_assets m ON m.id=ro.media_id
                JOIN source_permissions p ON p.source_id=m.source_id
                WHERE pv.id=?""",(published_id,)).fetchone()
            if not row: raise FileNotFoundError("Publishing record or linked rendered output not found.")
            if row["render_qc"]!="PASS" or row["qc_status"]!="PASS": raise PermissionError("Upload blocked: render must have QC PASS.")
            if row["rights_status"]!="AUTHORIZED" or expiry_passed(row["expires_at"]): raise PermissionError("Upload blocked: current AUTHORIZED permission is required.")
            enabled=c.execute("SELECT value FROM settings WHERE key='youtube_uploads_enabled'").fetchone()
            if not enabled or enabled["value"]!="true" or os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()!="true":
                raise PermissionError("YouTube uploads are disabled. Owner must configure and enable them explicitly.")
            output=Path(row["output_path"])
            if not output.is_file() or output.stat().st_size != row["render_size"]:
                raise PermissionError("Upload blocked: output is missing or its size changed since QC.")
            import hashlib
            digest=hashlib.sha256()
            with output.open("rb") as h:
                for block in iter(lambda:h.read(1024*1024),b""): digest.update(block)
            if digest.hexdigest()!=row["render_sha"]:
                raise PermissionError("Upload blocked: output hash no longer matches QC record.")
            publish_at=None
            raw_date=payload.get("publish_at")
            if raw_date:
                publish_at=datetime.fromisoformat(raw_date.replace("Z","+00:00"))
                if publish_at.tzinfo is None or publish_at<=datetime.now(timezone.utc):
                    raise ValueError("Scheduled publication must have a timezone-aware future timestamp.")
                if os.getenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","").lower()!="true":
                    raise PermissionError("Scheduled public publication is disabled by environment policy.")
            t=now()
            c.execute("UPDATE published_videos SET publishing_status='UPLOADING',upload_request_started_at=?,updated_at=? WHERE id=?",(t,t,published_id))
            c.commit()
            title=row["title"]; description=row["description"]
            tags=payload.get("tags",[])
            if not isinstance(tags,list): tags=[]
            # upload_video raises a dedicated non-retryable reconciliation status on any uncertain result.
            result=upload_video(output,title,description,[str(x) for x in tags],publish_at=publish_at,allow_scheduled_public=os.getenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","").lower()=="true")
            final_status="SCHEDULED" if publish_at else "UPLOADED_PRIVATE"
            c.execute("UPDATE published_videos SET youtube_video_id=?,publishing_status=?,scheduled_publish_at=?,updated_at=? WHERE id=?",(result["video_id"],final_status,result.get("publish_at"),now(),published_id))
            t=now()
            summary={"published_video_id":published_id,"youtube_video_id":result["video_id"],"url":result["url"],"publishing_status":final_status,"scheduled_publish_at":result.get("publish_at")}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",(json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","youtube.upload_completed","Video uploaded to YouTube as private or scheduled.",{"job_id":job["id"],**summary})
            c.commit()
        finally:
            c.close()

    def sync_youtube_analytics(self, job):
        payload=json.loads(job["payload_json"] or "{}")
        start_date=str(payload.get("start_date",""))[:10]; end_date=str(payload.get("end_date",""))[:10]
        try:
            datetime.strptime(start_date,"%Y-%m-%d"); datetime.strptime(end_date,"%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Analytics job needs valid ISO dates (YYYY-MM-DD).") from exc
        rows=fetch_video_analytics(start_date,end_date,max_results=200)
        c=connect(self.db)
        try:
            inserted=0
            t=now()
            for metrics in rows:
                vid=str(metrics.get("video",""))
                record=c.execute("SELECT id FROM published_videos WHERE youtube_video_id=?",(vid,)).fetchone()
                if not record: continue
                c.execute("INSERT INTO analytics_snapshots(id,published_video_id,captured_at,metrics_json,provider) VALUES(?,?,?,?,?)",(str(uuid4()),record["id"],t,json.dumps({"start_date":start_date,"end_date":end_date,"metrics":metrics}),"youtube_analytics_api"))
                inserted+=1
            summary={"start_date":start_date,"end_date":end_date,"rows_returned":len(rows),"snapshots_saved":inserted}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",(json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","youtube.analytics_synced","YouTube Analytics snapshots synchronized.",{"job_id":job["id"],**summary})
            c.commit()
        finally:
            c.close()
