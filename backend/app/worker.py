import json, re, subprocess, threading, hashlib, os
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
from .db import connect, now, event
from .integrations.twitch import TwitchHelix
from .integrations.youtube import upload_video, fetch_video_analytics, UploadReconciliationRequired, YouTubeNotConfigured, youtube_service, youtube_discovery_available, search_public_videos

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

def srt_time(seconds):
    milliseconds=max(0,int(round(float(seconds)*1000)))
    hours,milliseconds=divmod(milliseconds,3600000)
    minutes,milliseconds=divmod(milliseconds,60000)
    seconds_part,milliseconds=divmod(milliseconds,1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d},{milliseconds:03d}"

def escape_filter_path(path):
    value=Path(path).resolve().as_posix()
    value=value.replace(":", "\\:")
    for char in ("'", ",", "[", "]", ";"):
        value=value.replace(char, "\\"+char)
    return value

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
            self.schedule_due_youtube_discovery()
            return False
        try:
            if job["job_type"] == "ANALYZE_MEDIA":
                self.analyze(job)
            elif job["job_type"] == "RENDER_CANDIDATE":
                self.render_candidate(job)
            elif job["job_type"] == "DISCOVER_TWITCH_CLIPS":
                self.discover_twitch(job)
            elif job["job_type"] == "DISCOVER_YOUTUBE_VIDEOS":
                self.discover_youtube(job)
            elif job["job_type"] == "UPLOAD_YOUTUBE":
                self.upload_youtube(job)
            elif job["job_type"] == "CHECK_YOUTUBE_PROCESSING":
                self.check_youtube_processing(job)
            elif job["job_type"] == "PUBLISH_YOUTUBE":
                self.publish_youtube(job)
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
                if status == "FAILED" and job["job_type"] == "CHECK_YOUTUBE_PROCESSING":
                    try:
                        published_id=json.loads(job["payload_json"] or "{}").get("published_video_id")
                        if published_id:
                            c.execute("UPDATE published_videos SET remote_processing_status='check_error',processing_checked_at=?,processing_failure_reason=?,publishing_status='PROCESSING_STATUS_ERROR',updated_at=? WHERE id=? AND remote_processing_status NOT IN ('succeeded','failed','terminated')",
                                      (t,str(e)[:1000],t,published_id))
                    except (ValueError, TypeError):
                        pass
                if status == "FAILED" and job["job_type"] == "PUBLISH_YOUTUBE":
                    try:
                        published_id=json.loads(job["payload_json"] or "{}").get("published_video_id")
                        if published_id:
                            c.execute("UPDATE published_videos SET publishing_status='PUBLICATION_FAILED',updated_at=? WHERE id=? AND publishing_status IN ('SCHEDULED','PROCESSING','READY_PRIVATE')")
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
        if os.getenv("GCF_ENABLE_TRANSCRIPTION","").lower()=="true":
            if importlib.util.find_spec("faster_whisper") is not None:
                c=connect(self.db)
                try:
                    active=c.execute("SELECT id FROM jobs WHERE job_type='TRANSCRIBE_MEDIA' AND media_id=? AND status IN ('QUEUED','RUNNING')",(job["media_id"],)).fetchone()
                    if not active:
                        jid=str(uuid4()); t=now()
                        c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,created_at,updated_at) VALUES(?,'TRANSCRIBE_MEDIA','QUEUED',?,?,?,?)",(jid,job["source_id"],job["media_id"],t,t))
                        event(c,"INFO","transcription.queued","Local transcription queued after media analysis.",{"job_id":jid,"media_id":job["media_id"]})
                        c.commit()
                finally: c.close()
            else:
                c=connect(self.db)
                try:
                    event(c,"WARNING","transcription.not_installed","Transcription is enabled but faster-whisper is not installed; install backend/requirements-ml.txt.",{"media_id":job["media_id"]}); c.commit()
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
            # Build a per-candidate SRT sidecar from timestamped speech segments, if available.
            caption_rows=c.execute("SELECT start_seconds,end_seconds,text,confidence FROM caption_segments WHERE candidate_id=? ORDER BY start_seconds",(cid,)).fetchall()
            caption_path=None
            captions_burned=False
            caption_filter_warning=False
            vf="scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black"
            if caption_rows:
                caption_path=output.with_suffix(".srt")
                blocks=[]
                cap_index=0
                for cap in caption_rows:
                    cap_start=max(start,float(cap["start_seconds"]))-start
                    cap_end=min(end,float(cap["end_seconds"]))-start
                    text_line=str(cap["text"]).replace("-->","→").replace("\\r"," ").replace("\\n"," ").strip()
                    if not text_line or cap_end<=cap_start: continue
                    cap_index+=1
                    blocks.append(f"{cap_index}\\n{srt_time(cap_start)} --> {srt_time(cap_end)}\\n{text_line}\\n")
                if blocks:
                    with caption_path.open("x",encoding="utf-8",newline="\\n") as sidecar:
                        sidecar.write("\\n".join(blocks))
                    if os.getenv("GCF_BURN_CAPTIONS","true").lower()=="true":
                        filters=run([self.ffmpeg,"-hide_banner","-filters"],15)
                        if " subtitles " in filters.stdout or re.search(r"\\bsubtitles\\s+V->V",filters.stdout):
                            escaped=escape_filter_path(caption_path)
                            vf+=",subtitles=filename='"+escaped+"':force_style='FontName=Arial,FontSize=24,Outline=2,Shadow=0,MarginV=110'"
                            captions_burned=True
                        else:
                            caption_filter_warning=True
            # Preserve the complete gameplay frame in a fitted portrait canvas; no forced center crop.
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
            if video.get("codec_name") != "h264":
                raise RuntimeError("QC failed: expected H.264 video encoding.")
            if audio and audio.get("codec_name") != "aac":
                raise RuntimeError("QC failed: expected AAC audio encoding.")
            if not (max(0.5,expected-0.5) <= duration <= expected+0.5):
                raise RuntimeError(f"QC failed: output duration {duration:.3f}s differs from expected {expected:.3f}s.")
            if source_has_audio and not audio:
                raise RuntimeError("QC failed: source contains audio but output audio stream is missing.")
            # Extra decode/defect scan. Black or frozen intervals are review flags, not silently ignored.
            scan=run([self.ffmpeg,"-hide_banner","-nostdin","-i",str(output),"-vf","blackdetect=d=1.5:pix_th=0.10,freezedetect=n=-50dB:d=2.0","-an","-f","null","-"],self.timeout)
            black_events=[]
            for match in re.finditer(r"black_start:([0-9.]+) black_end:([0-9.]+) black_duration:([0-9.]+)",scan.stderr):
                black_events.append({"start":float(match.group(1)),"end":float(match.group(2)),"duration":float(match.group(3))})
            freeze_events=[]
            freeze_start=None
            for line in scan.stderr.splitlines():
                start_match=re.search(r"freeze_start:([0-9.]+)",line)
                if start_match: freeze_start=float(start_match.group(1))
                duration_match=re.search(r"freeze_duration:([0-9.]+)",line)
                if duration_match and freeze_start is not None:
                    freeze_events.append({"start":freeze_start,"duration":float(duration_match.group(1))})
                    freeze_start=None
            review_flags=[]
            if black_events: review_flags.append("black_interval_detected")
            if freeze_events: review_flags.append("potential_frozen_frame_detected")
            if caption_filter_warning: review_flags.append("subtitle_filter_unavailable_caption_sidecar_only")
            qc_status="REVIEW" if review_flags else "PASS"
            h=hashlib.sha256()
            with output.open("rb") as handle:
                for block in iter(lambda:handle.read(1024*1024),b""):
                    h.update(block)
            warnings=list(review_flags)
            if not audio and not source_has_audio: warnings.append("Source had no audio stream.")
            report={"passed":qc_status=="PASS","checks":{"nonempty":True,"video_stream":True,"dimensions":"1080x1920","video_codec":video.get("codec_name"),"audio_codec":audio.get("codec_name") if audio else None,"duration_seconds":duration,"source_audio_present":source_has_audio,"output_audio_present":bool(audio),"ffmpeg_decode_scan":"PASS","black_intervals":black_events,"freeze_intervals":freeze_events},"layout":"fit-and-pad-no-crop","caption_sidecar_path":str(caption_path) if caption_path and caption_path.is_file() else None,"captions_burned":captions_burned,"warnings":warnings}
            out_id=str(uuid4()); t=now()
            c.execute("""INSERT INTO rendered_outputs(id,candidate_id,media_id,output_path,file_size,sha256,duration_seconds,width,height,video_codec,audio_codec,qc_status,qc_report_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(out_id,cid,row["media_id"],str(output),output.stat().st_size,h.hexdigest(),duration,video["width"],video["height"],video.get("codec_name","unknown"),audio.get("codec_name") if audio else None,qc_status,json.dumps(report),t))
            candidate_state="RENDERED_QC_PASSED" if qc_status=="PASS" else "RENDERED_QC_REVIEW"
            c.execute("UPDATE candidate_moments SET state=? WHERE id=?",(candidate_state,cid))
            summary={"rendered_output_id":out_id,"output_path":str(output),"file_size":output.stat().st_size,"sha256":h.hexdigest(),"qc_status":qc_status,"qc_report":report}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",(json.dumps(summary),t,t,job["id"]))
            event(c,"INFO" if qc_status=="PASS" else "WARNING","render.qc_passed" if qc_status=="PASS" else "render.qc_review_required","Candidate rendered; deterministic QC status is "+qc_status+".",{"job_id":job["id"],"candidate_id":cid,"output_id":out_id,"duration_seconds":duration,"warnings":warnings})
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
                if row["expires_at"]:
                    expiry=datetime.fromisoformat(row["expires_at"].replace("Z","+00:00"))
                    if expiry.tzinfo is None: expiry=expiry.replace(tzinfo=timezone.utc)
                    if expiry<=publish_at:
                        raise PermissionError("Rights expire before scheduled publication; scheduled public publishing is blocked.")
            t=now()
            c.execute("UPDATE published_videos SET publishing_status='UPLOADING',upload_request_started_at=?,updated_at=? WHERE id=?",(t,t,published_id))
            c.commit()
            tags=payload.get("tags",[])
            if not isinstance(tags,list): tags=[]
            # Remote upload always starts private. A separate worker polls processing status,
            # then schedules public release only after YouTube reports processing succeeded.
            result=upload_video(output,row["title"],row["description"],[str(x) for x in tags])
            t=now()
            c.execute("""UPDATE published_videos SET youtube_video_id=?,publishing_status='PROCESSING',
                remote_processing_status='unknown',processing_checked_at=NULL,processing_failure_reason='',
                scheduled_publish_at=?,updated_at=? WHERE id=?""",
                (result["video_id"],publish_at.isoformat() if publish_at else None,t,published_id))
            summary={"published_video_id":published_id,"youtube_video_id":result["video_id"],"url":result["url"],
                     "publishing_status":"PROCESSING","scheduled_publish_at":publish_at.isoformat() if publish_at else None,
                     "initial_privacy_status":"private","processing_status":"pending"}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",(json.dumps(summary),t,t,job["id"]))
            check_job_id=str(uuid4())
            check_at=(datetime.now(timezone.utc)+__import__("datetime").timedelta(seconds=15)).isoformat(timespec="seconds")
            c.execute("""INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at,next_attempt_at)
                VALUES(?,'CHECK_YOUTUBE_PROCESSING','QUEUED',?,?, ?,?,?,?)""",
                (check_job_id,row["source_id"],row["media_id"],json.dumps({"published_video_id":published_id,"poll_attempt":0,"max_polls":20}),t,t,check_at))
            event(c,"INFO","youtube.upload_completed","Upload accepted by YouTube as private; a bounded processing-status poll is queued before any public release.",{"job_id":job["id"],"processing_job_id":check_job_id,**summary})
            c.commit()
        finally:
            c.close()

    def check_youtube_processing(self, job):
        payload=json.loads(job["payload_json"] or "{}")
        published_id=payload.get("published_video_id")
        if not published_id:
            raise ValueError("Processing check is missing published_video_id.")
        c=connect(self.db)
        try:
            row=c.execute("SELECT id,youtube_video_id,scheduled_publish_at,publishing_status FROM published_videos WHERE id=?",(published_id,)).fetchone()
            if not row or not row["youtube_video_id"]:
                raise FileNotFoundError("Published video record or remote YouTube ID is missing.")
            if row["publishing_status"] in {"BLOCKED_RIGHTS","PROCESSING_FAILED","PROCESSING_TERMINATED","PROCESSING_TIMEOUT","PUBLICATION_FAILED"}:
                t=now()
                c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",
                          (json.dumps({"published_video_id":published_id,"skipped":True,"reason":row["publishing_status"]}),t,t,job["id"]))
                c.commit()
                return
            video_id=row["youtube_video_id"]
        finally:
            c.close()
        response=youtube_service().videos().list(part="processingDetails,status",id=video_id).execute()
        items=response.get("items",[]) or []
        remote=items[0] if items else {}
        details=remote.get("processingDetails") or {}
        remote_status=str(details.get("processingStatus","unknown")).lower()
        failure_reason=str(details.get("processingFailureReason","") or "")
        t=now()
        c=connect(self.db)
        try:
            row=c.execute("SELECT pv.*,m.source_id,m.id AS media_id FROM published_videos pv JOIN media_assets m ON m.id=pv.media_id WHERE pv.id=?",(published_id,)).fetchone()
            if not row: raise FileNotFoundError("Published record disappeared during processing poll.")
            if remote_status in {"failed","terminated"}:
                final_state="PROCESSING_FAILED" if remote_status=="failed" else "PROCESSING_TERMINATED"
                reason=failure_reason or ("YouTube reported that processing failed." if remote_status=="failed" else "YouTube no longer exposes processing status for this video.")
                c.execute("UPDATE published_videos SET remote_processing_status=?,processing_checked_at=?,processing_failure_reason=?,publishing_status=?,updated_at=? WHERE id=?",
                          (remote_status,t,reason,final_state,t,published_id))
                c.execute("UPDATE jobs SET status='CANCELLED',error=?,updated_at=?,finished_at=? WHERE job_type='PUBLISH_YOUTUBE' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ?",
                          (reason,t,t,"%"+published_id+"%"))
                c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",
                          (json.dumps({"published_video_id":published_id,"processing_status":remote_status,"failure_reason":reason}),t,t,job["id"]))
                event(c,"ERROR","youtube.processing_failed","YouTube could not complete processing; no public release will be scheduled.",{"job_id":job["id"],"published_video_id":published_id,"youtube_video_id":video_id,"processing_status":remote_status,"reason":reason})
                c.commit()
                return
            if remote_status=="succeeded":
                scheduled_at=row["scheduled_publish_at"]
                final_state="SCHEDULED" if scheduled_at else "READY_PRIVATE"
                c.execute("UPDATE published_videos SET remote_processing_status='succeeded',processing_checked_at=?,processing_failure_reason='',publishing_status=?,updated_at=? WHERE id=?",
                          (t,final_state,t,published_id))
                scheduled_job_id=None
                if scheduled_at and os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()=="true" and os.getenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","").lower()=="true":
                    existing=c.execute("SELECT id FROM jobs WHERE job_type='PUBLISH_YOUTUBE' AND status IN ('QUEUED','RUNNING','COMPLETED','RECONCILE_REQUIRED') AND payload_json LIKE ? ORDER BY created_at DESC LIMIT 1",("%"+published_id+"%",)).fetchone()
                    if not existing:
                        parsed=datetime.fromisoformat(scheduled_at.replace("Z","+00:00"))
                        if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
                        due=max(parsed,datetime.now(timezone.utc)).isoformat(timespec="seconds")
                        scheduled_job_id=str(uuid4())
                        c.execute("""INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at,next_attempt_at)
                            VALUES(?,'PUBLISH_YOUTUBE','QUEUED',?,?, ?,?,?,?)""",
                            (scheduled_job_id,row["source_id"],row["media_id"],json.dumps({"published_video_id":published_id,"publish_at":parsed.isoformat()}),t,t,due))
                c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",
                          (json.dumps({"published_video_id":published_id,"processing_status":"succeeded","publishing_status":final_state,"scheduled_publish_job_id":scheduled_job_id}),t,t,job["id"]))
                event(c,"INFO","youtube.processing_succeeded","YouTube processing succeeded; video is ready for private viewing or separately gated publication.",{"job_id":job["id"],"published_video_id":published_id,"youtube_video_id":video_id,"publishing_status":final_state,"scheduled_publish_job_id":scheduled_job_id})
                c.commit()
                return
            poll_attempt=int(payload.get("poll_attempt",0))
            max_polls=max(1,min(60,int(payload.get("max_polls",20))))
            if poll_attempt>=max_polls:
                c.execute("UPDATE published_videos SET remote_processing_status=?,processing_checked_at=?,processing_failure_reason='Processing poll limit reached; owner may request a new check.',publishing_status='PROCESSING_TIMEOUT',updated_at=? WHERE id=?",
                          (remote_status,t,t,published_id))
                c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",
                          (json.dumps({"published_video_id":published_id,"processing_status":remote_status,"timed_out":True,"polls":poll_attempt+1}),t,t,job["id"]))
                event(c,"WARNING","youtube.processing_poll_limit","YouTube processing is still pending after bounded polling; no public release was scheduled.",{"job_id":job["id"],"published_video_id":published_id,"polls":poll_attempt+1})
                c.commit()
                return
            delay=min(300,15*(2**min(poll_attempt,5)))
            due=(datetime.now(timezone.utc)+__import__("datetime").timedelta(seconds=delay)).isoformat(timespec="seconds")
            next_id=str(uuid4())
            c.execute("UPDATE published_videos SET remote_processing_status=?,processing_checked_at=?,publishing_status='PROCESSING',updated_at=? WHERE id=?",
                      (remote_status,t,t,published_id))
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",
                      (json.dumps({"published_video_id":published_id,"processing_status":remote_status,"next_poll_at":due}),t,t,job["id"]))
            c.execute("""INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at,next_attempt_at)
                VALUES(?,'CHECK_YOUTUBE_PROCESSING','QUEUED',?,?, ?,?,?,?)""",
                (next_id,row["source_id"],row["media_id"],json.dumps({"published_video_id":published_id,"poll_attempt":poll_attempt+1,"max_polls":max_polls}),t,t,due))
            event(c,"INFO","youtube.processing_pending","YouTube is still processing; next status check is scheduled with a bounded poll count.",{"job_id":job["id"],"next_job_id":next_id,"published_video_id":published_id,"processing_status":remote_status,"next_poll_at":due,"poll_attempt":poll_attempt+1})
            c.commit()
        finally:
            c.close()

    def publish_youtube(self, job):
        payload=json.loads(job["payload_json"] or "{}")
        published_id=payload.get("published_video_id")
        if not published_id:
            raise ValueError("Publication job is missing published_video_id.")
        if os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()!="true" or os.getenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","").lower()!="true":
            raise PermissionError("Scheduled public publishing is disabled by environment policy.")
        c=connect(self.db)
        try:
            row=c.execute("""SELECT pv.*,ro.qc_status AS render_qc,ro.sha256 AS render_sha,ro.file_size AS render_size,
                p.status AS rights_status,p.expires_at
                FROM published_videos pv JOIN rendered_outputs ro ON ro.id=pv.rendered_output_id
                JOIN media_assets m ON m.id=ro.media_id JOIN source_permissions p ON p.source_id=m.source_id
                WHERE pv.id=?""",(published_id,)).fetchone()
            if not row:
                raise FileNotFoundError("Scheduled publication record not found.")
            if row["publishing_status"]!="SCHEDULED" or not row["youtube_video_id"]:
                raise PermissionError("Publication is not in a valid scheduled state.")
            if row["qc_status"]!="PASS" or row["render_qc"]!="PASS":
                raise PermissionError("Scheduled publication blocked: output no longer has QC PASS.")
            if row["rights_status"]!="AUTHORIZED" or expiry_passed(row["expires_at"]):
                c.execute("UPDATE published_videos SET publishing_status='BLOCKED_RIGHTS',updated_at=? WHERE id=?",(now(),published_id)); c.commit()
                raise PermissionError("Scheduled publication blocked because rights are expired, revoked or unauthorized.")
            output=Path(row["output_path"])
            if not output.is_file() or output.stat().st_size!=row["render_size"]:
                raise PermissionError("Scheduled publication blocked: rendered output changed or disappeared.")
            h=hashlib.sha256()
            with output.open("rb") as stream:
                for block in iter(lambda:stream.read(1024*1024),b""): h.update(block)
            if h.hexdigest()!=row["render_sha"]:
                raise PermissionError("Scheduled publication blocked: output hash no longer matches QC.")
            enabled=c.execute("SELECT value FROM settings WHERE key='youtube_uploads_enabled'").fetchone()
            if not enabled or enabled["value"]!="true":
                raise PermissionError("Scheduled publication blocked: owner disabled YouTube uploads.")
            t=now()
            try:
                youtube_service().videos().update(part="status",body={"id":row["youtube_video_id"],"status":{"privacyStatus":"public"}}).execute()
            except Exception as exc:
                status=getattr(getattr(exc,"resp",None),"status",None)
                if status in {400,401,403,404,409,422}:
                    raise RuntimeError(f"YouTube rejected scheduled publication with HTTP {status}; video remains private.") from exc
                raise UploadReconciliationRequired(f"Public update outcome is uncertain ({type(exc).__name__}); check the channel before retrying.") from exc
            c.execute("UPDATE published_videos SET publishing_status='PUBLISHED',updated_at=? WHERE id=?",(t,published_id))
            summary={"published_video_id":published_id,"youtube_video_id":row["youtube_video_id"],"publishing_status":"PUBLISHED"}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",(json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","youtube.scheduled_video_published","Scheduled video made public after just-in-time rights/QC checks.",{"job_id":job["id"],**summary})
            c.commit()
        finally:
            c.close()


    def transcribe_media(self, job):
        if os.getenv("GCF_ENABLE_TRANSCRIPTION","").lower()!="true":
            raise PermissionError("Local transcription is disabled by configuration.")
        c=connect(self.db)
        try:
            row=c.execute("""SELECT m.*,p.status AS rights_status,p.expires_at
                FROM media_assets m JOIN source_permissions p ON p.source_id=m.source_id WHERE m.id=?""",(job["media_id"],)).fetchone()
            if not row: raise FileNotFoundError("Media record missing.")
            if row["rights_status"]!="AUTHORIZED" or expiry_passed(row["expires_at"]):
                raise PermissionError("Rights gate blocked transcription: current AUTHORIZED permission is required.")
            media_path=Path(row["original_path"])
            if not media_path.is_file(): raise FileNotFoundError("Media file is missing.")
        finally:
            c.close()
        timeout=int(os.getenv("GCF_TRANSCRIPTION_TIMEOUT_SECONDS","3600"))
        timeout=max(60,min(timeout,21600))
        backend_dir=Path(__file__).resolve().parents[1]
        output=run([sys.executable,"-m","app.transcribe_cli","--input",str(media_path)],timeout,cwd=backend_dir)
        result=json.loads(output.stdout)
        segments=result.get("segments") or []
        if not isinstance(segments,list): raise RuntimeError("Transcription result has an invalid segment list.")
        cleaned=[]
        for item in segments:
            try:
                start=float(item["start_seconds"]); end=float(item["end_seconds"]); text=str(item.get("text","")).strip()
                confidence=item.get("confidence")
                confidence=float(confidence) if confidence is not None else None
            except (KeyError,TypeError,ValueError):
                continue
            if text and start>=0 and end>start:
                cleaned.append({"start_seconds":round(start,3),"end_seconds":round(end,3),"text":text[:1000],"confidence":confidence})
        full_text=" ".join(x["text"] for x in cleaned).strip()
        c=connect(self.db)
        try:
            current=c.execute("SELECT id FROM transcripts WHERE media_id=?",(job["media_id"],)).fetchone()
            transcript_id=current["id"] if current else str(uuid4())
            t=now()
            c.execute("""INSERT INTO transcripts(id,media_id,language,language_probability,full_text,model_name,status,error,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'COMPLETED','',?,?)
                ON CONFLICT(media_id) DO UPDATE SET language=excluded.language,language_probability=excluded.language_probability,
                full_text=excluded.full_text,model_name=excluded.model_name,status='COMPLETED',error='',updated_at=excluded.updated_at""",
                (transcript_id,job["media_id"],str(result.get("language","unknown")),float(result.get("language_probability",0) or 0),full_text,str(result.get("model_name","unknown")),t,t))
            transcript_id=c.execute("SELECT id FROM transcripts WHERE media_id=?",(job["media_id"],)).fetchone()["id"]
            c.execute("DELETE FROM transcript_segments WHERE transcript_id=?",(transcript_id,))
            for idx,seg in enumerate(cleaned):
                c.execute("INSERT INTO transcript_segments(id,transcript_id,segment_index,start_seconds,end_seconds,text,confidence) VALUES(?,?,?,?,?,?,?)",
                    (str(uuid4()),transcript_id,idx,seg["start_seconds"],seg["end_seconds"],seg["text"],seg["confidence"]))
            media_row=c.execute("SELECT s.title,s.game FROM media_assets m JOIN sources s ON s.id=m.source_id WHERE m.id=?",(job["media_id"],)).fetchone()
            candidates=c.execute("SELECT id,start_seconds,end_seconds FROM candidate_moments WHERE media_id=? AND state NOT IN ('REJECTED','RENDER_FAILED')",(job["media_id"],)).fetchall()
            for candidate in candidates:
                candidate_segments=[x for x in cleaned if x["end_seconds"]>candidate["start_seconds"] and x["start_seconds"]<candidate["end_seconds"]]
                plan=plan_candidate(media_row["game"],media_row["title"],candidate["start_seconds"],candidate["end_seconds"],candidate_segments)
                old_plan=c.execute("SELECT id FROM content_plans WHERE candidate_id=?",(candidate["id"],)).fetchone()
                plan_id=old_plan["id"] if old_plan else str(uuid4())
                c.execute("""INSERT INTO content_plans(id,candidate_id,chosen_format,hook_text,title,description,tags_json,narration_text,evidence_json,confidence,strategy_version,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,'DRAFT',?,?)
                    ON CONFLICT(candidate_id) DO UPDATE SET chosen_format=excluded.chosen_format,hook_text=excluded.hook_text,title=excluded.title,
                    description=excluded.description,tags_json=excluded.tags_json,narration_text=excluded.narration_text,evidence_json=excluded.evidence_json,
                    confidence=excluded.confidence,strategy_version=excluded.strategy_version,status='DRAFT',updated_at=excluded.updated_at""",
                    (plan_id,candidate["id"],plan["chosen_format"],plan["hook_text"],plan["title"],plan["description"],json.dumps(plan["tags"]),plan["narration_text"],json.dumps(plan["evidence"]),plan["confidence"],plan["strategy_version"],t,t))
                c.execute("UPDATE candidate_moments SET ranking_score=?,ranking_components_json=? WHERE id=?",(plan["confidence"],json.dumps(plan["ranking_components"]),candidate["id"]))
                c.execute("DELETE FROM caption_segments WHERE candidate_id=?",(candidate["id"],))
                caption_index=0
                for seg in candidate_segments:
                    seg_start=max(float(candidate["start_seconds"]),seg["start_seconds"])
                    seg_end=min(float(candidate["end_seconds"]),seg["end_seconds"])
                    if seg_end<=seg_start: continue
                    c.execute("INSERT INTO caption_segments(id,candidate_id,segment_index,start_seconds,end_seconds,text,confidence,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (str(uuid4()),candidate["id"],caption_index,round(seg_start,3),round(seg_end,3),seg["text"],seg["confidence"],t))
                    caption_index+=1
            c.execute("UPDATE media_assets SET state='TRANSCRIBED' WHERE id=?",(job["media_id"],))
            summary={"media_id":job["media_id"],"transcript_id":transcript_id,"language":result.get("language","unknown"),"segment_count":len(cleaned),"candidate_plans_updated":len(candidates),"model_name":result.get("model_name"),"local_only":True}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",(json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","transcription.completed","Local transcription, content plans and caption timings persisted.",{"job_id":job["id"],**summary})
            c.commit()
        finally:
            c.close()


    def schedule_due_youtube_discovery(self):
        """Queue scheduled YouTube public-metadata search when configured and due."""
        if not youtube_discovery_available():
            return
        c=connect(self.db)
        try:
            values={r["key"]:r["value"] for r in c.execute("SELECT key,value FROM settings WHERE key IN ('global_paused','youtube_discovery_enabled','youtube_discovery_queries','youtube_discovery_interval_minutes','youtube_discovery_next_at')")}
            if values.get("global_paused")=="true" or values.get("youtube_discovery_enabled")!="true":
                return
            try: queries=json.loads(values.get("youtube_discovery_queries","[]"))
            except ValueError: return
            if not isinstance(queries,list) or not queries: return
            next_at=values.get("youtube_discovery_next_at")
            if next_at:
                try:
                    due=datetime.fromisoformat(next_at.replace("Z","+00:00"))
                    if due.tzinfo is None: due=due.replace(tzinfo=timezone.utc)
                    if due>datetime.now(timezone.utc): return
                except ValueError:
                    pass
            jid=str(uuid4()); t=now()
            c.execute("INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at) VALUES(?,'DISCOVER_YOUTUBE_VIDEOS','QUEUED',?,?,?)",(jid,json.dumps({"game_queries":queries,"days":7,"max_results":50}),t,t))
            minutes=max(15,min(1440,int(values.get("youtube_discovery_interval_minutes","60"))))
            next_time=(datetime.now(timezone.utc)+__import__("datetime").timedelta(minutes=minutes)).isoformat(timespec="seconds")
            c.execute("UPDATE settings SET value=?,updated_at=? WHERE key='youtube_discovery_next_at'",(next_time,t))
            event(c,"INFO","discovery.scheduled_job_queued","Scheduled YouTube metadata search queued.",{"job_id":jid,"queries":queries,"next_run":next_time})
            c.commit()
        finally:
            c.close()

    def discover_youtube(self, job):
        payload=json.loads(job["payload_json"] or "{}")
        queries=payload.get("game_queries",[])
        if not isinstance(queries,list) or not queries:
            raise ValueError("YouTube discovery job requires one or more search queries.")
        if not youtube_discovery_available():
            raise YouTubeNotConfigured("Set GCF_YOUTUBE_API_KEY for public metadata search or connect a YouTube OAuth channel.")
        c=connect(self.db)
        found_total=0; inserted_total=0; outcomes=[]
        try:
            for raw_query in queries[:20]:
                query=str(raw_query).strip()[:120]
                if not query: continue
                run_id=str(uuid4()); started=now()
                c.execute("INSERT INTO discovery_runs(id,provider,query,status,started_at) VALUES(?,?,?,?,?)",(run_id,"youtube_data_api",query,"RUNNING",started)); c.commit()
                try:
                    items=search_public_videos(query,days=int(payload.get("days",7)),max_results=int(payload.get("max_results",50)))
                    inserted=0
                    for item in items:
                        found_total+=1
                        cur=c.execute("""INSERT OR IGNORE INTO sources(id,source_url,platform,title,creator,game,discovered_at,created_at,external_source_id,provider_metadata_json)
                            VALUES(?,?,?,?,?,?,?,?,?,?)""",(str(uuid4()),item["url"],"youtube",item["title"],item["creator"],item["game"],now(),now(),item["provider_video_id"],json.dumps(item)))
                        if cur.rowcount:
                            sid=c.execute("SELECT id FROM sources WHERE source_url=?",(item["url"],)).fetchone()["id"]
                            c.execute("INSERT OR IGNORE INTO source_permissions(source_id,status,updated_at) VALUES(?,'UNKNOWN',?)",(sid,now()))
                            inserted+=1; inserted_total+=1
                    c.execute("UPDATE discovery_runs SET status='COMPLETED',found_count=?,inserted_count=?,finished_at=? WHERE id=?",(len(items),inserted,now(),run_id))
                    outcomes.append({"query":query,"found":len(items),"inserted":inserted})
                    c.commit()
                except Exception as exc:
                    c.execute("UPDATE discovery_runs SET status='FAILED',error=?,finished_at=? WHERE id=?",(str(exc)[:1500],now(),run_id)); c.commit()
                    raise
            t=now()
            summary={"provider":"youtube_data_api","queries":outcomes,"found_total":found_total,"new_sources":inserted_total,"rights_status":"UNKNOWN","media_downloaded":False}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",(json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","discovery.completed",f"Discovered metadata for {found_total} YouTube videos; inserted {inserted_total} new sources.",{"job_id":job["id"],**summary}); c.commit()
        finally:
            c.close()

