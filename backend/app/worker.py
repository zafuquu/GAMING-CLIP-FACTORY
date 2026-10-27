import json, re, subprocess, threading, hashlib, os, sys, importlib.util, tempfile, sqlite3, wave
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
from .db import connect, now, event, session
from .integrations.twitch import TwitchHelix
from .integrations.youtube import upload_video, fetch_video_analytics, UploadReconciliationRequired, YouTubeNotConfigured, ThumbnailUploadError, set_custom_thumbnail, youtube_service, youtube_discovery_available, youtube_oauth_available, search_public_videos
from .thumbnail_art import generate_shorts_thumbnail
from .content_director import plan_candidate
from .render_profiles import build_video_filter, profile_warning
from .vision import assess_candidate_images, VisionNotConfigured
from .narration import synthesize_piper, NarrationNotConfigured, NarrationError, narration_status as get_narration_status, validate_script

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
def run(args,timeout,cwd=None):
    try: p=subprocess.run(args,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=timeout,shell=False,cwd=str(cwd) if cwd else None)
    except subprocess.TimeoutExpired as e: raise RuntimeError(f"Command timed out after {timeout}s: {args[0]}") from e
    if p.returncode: raise RuntimeError(f"{Path(args[0]).name} exited {p.returncode}: {(p.stderr or p.stdout)[-1500:]}")
    return p
class Worker(threading.Thread):
    def __init__(self,db,ffmpeg,ffprobe,timeout=120,media_root=None):
        super().__init__(daemon=True,name="gcf-worker"); self.db=Path(db).resolve(); self.ffmpeg=ffmpeg; self.ffprobe=ffprobe; self.timeout=timeout; self.media_root=Path(media_root or (self.db.parent/"media")).expanduser().resolve(); self.stop_event=threading.Event()
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
        self.scan_due_folder_watches()
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
            self.schedule_due_analytics()
            return False
        try:
            if job["job_type"] == "ANALYZE_MEDIA":
                self.analyze(job)
            elif job["job_type"] == "RENDER_CANDIDATE":
                self.render_candidate(job)
            elif job["job_type"] == "TRANSCRIBE_MEDIA":
                self.transcribe_media(job)
            elif job["job_type"] == "SYNTHESIZE_NARRATION":
                self.synthesize_narration(job)
            elif job["job_type"] == "ANALYZE_CANDIDATE_VISION":
                self.analyze_candidate_vision(job)
            elif job["job_type"] == "DISCOVER_TWITCH_CLIPS":
                self.discover_twitch(job)
            elif job["job_type"] == "DISCOVER_YOUTUBE_VIDEOS":
                self.discover_youtube(job)
            elif job["job_type"] == "UPLOAD_YOUTUBE":
                self.upload_youtube(job)
            elif job["job_type"] == "GENERATE_YOUTUBE_THUMBNAIL":
                self.generate_youtube_thumbnail(job)
            elif job["job_type"] == "SET_YOUTUBE_THUMBNAIL":
                self.set_youtube_thumbnail(job)
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
                    retryable = not isinstance(e,PermissionError) and not isinstance(e,ValueError) and not isinstance(e,(YouTubeNotConfigured,VisionNotConfigured,NarrationNotConfigured)) and bool(getattr(e,"retryable",True))
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
                        if published_id:
                            current=c.execute("SELECT publishing_status FROM published_videos WHERE id=?",(published_id,)).fetchone()
                            if current and current["publishing_status"] in {"UPLOAD_PENDING","UPLOADING"}:
                                failure_state="BLOCKED_RIGHTS" if isinstance(e,PermissionError) and "rights" in str(e).lower() else "FAILED"
                                c.execute("UPDATE published_videos SET publishing_status=?,updated_at=? WHERE id=? AND publishing_status IN ('UPLOAD_PENDING','UPLOADING')",(failure_state,t,published_id))
                    except (ValueError, TypeError):
                        pass
                if job["job_type"] == "SYNTHESIZE_NARRATION":
                    try:
                        payload=json.loads(job["payload_json"] or "{}")
                        plan_id=payload.get("content_plan_id")
                        expected_script_sha=payload.get("script_sha256")
                        plan=c.execute("SELECT narration_text FROM content_plans WHERE id=?",(plan_id,)).fetchone() if plan_id else None
                        current_script=validate_script(plan["narration_text"] or "") if plan else ""
                        current_sha=hashlib.sha256(current_script.encode("utf-8")).hexdigest() if current_script else ""
                        # A stale job must not mark a newer, owner-edited script as failed.
                        if plan_id and expected_script_sha==current_sha:
                            plan_state="FAILED" if status=="FAILED" else "QUEUED" if status=="QUEUED" else "FAILED"
                            c.execute("UPDATE content_plans SET narration_audio_status=?,narration_audio_error=?,updated_at=? WHERE id=? AND narration_audio_status<>'READY'",
                                      (plan_state,str(e)[:1500],t,plan_id))
                    except (ValueError,TypeError,sqlite3.Error):
                        pass
                if status == "FAILED" and job["job_type"] == "ANALYZE_CANDIDATE_VISION":
                    try:
                        cid=json.loads(job["payload_json"] or "{}").get("candidate_id")
                        if cid:
                            model=os.getenv("GCF_VISION_MODEL","llama3.2-vision").strip() or "unknown"
                            c.execute("""INSERT INTO candidate_vision_assessments(id,candidate_id,model_name,status,error,created_at,updated_at)
                                VALUES(?,?,?,'FAILED',?,?,?)
                                ON CONFLICT(candidate_id) DO UPDATE SET model_name=excluded.model_name,status='FAILED',error=excluded.error,updated_at=excluded.updated_at""",
                                (str(uuid4()),cid,model,str(e)[:2000],t,t))
                    except (ValueError,TypeError,sqlite3.Error):
                        pass
                if status == "FAILED" and job["job_type"] == "CHECK_YOUTUBE_PROCESSING":
                    try:
                        published_id=json.loads(job["payload_json"] or "{}").get("published_video_id")
                        if published_id:
                            c.execute("UPDATE published_videos SET remote_processing_status='check_error',processing_checked_at=?,processing_failure_reason=?,publishing_status='PROCESSING_STATUS_ERROR',updated_at=? WHERE id=? AND remote_processing_status NOT IN ('succeeded','failed','terminated')",
                                      (t,str(e)[:1000],t,published_id))
                    except (ValueError, TypeError):
                        pass
                if status == "FAILED" and job["job_type"] == "SET_YOUTUBE_THUMBNAIL":
                    try:
                        published_id=json.loads(job["payload_json"] or "{}").get("published_video_id")
                        if published_id:
                            c.execute("UPDATE published_videos SET custom_thumbnail_status='FAILED',custom_thumbnail_error=?,updated_at=? WHERE id=? AND custom_thumbnail_status<>'SET_SUCCESS'",
                                      (str(e)[:1200],t,published_id))
                    except (ValueError,TypeError):
                        pass
                if status == "FAILED" and job["job_type"] == "GENERATE_YOUTUBE_THUMBNAIL":
                    try:
                        published_id=json.loads(job["payload_json"] or "{}").get("published_video_id")
                        if published_id:
                            c.execute("UPDATE published_videos SET custom_thumbnail_status='GENERATION_FAILED',custom_thumbnail_error=?,updated_at=? WHERE id=?",
                                      (str(e)[:1200],t,published_id))
                    except (ValueError,TypeError):
                        pass
                if status == "FAILED" and job["job_type"] == "PUBLISH_YOUTUBE":
                    try:
                        published_id=json.loads(job["payload_json"] or "{}").get("published_video_id")
                        if published_id:
                            c.execute("UPDATE published_videos SET publishing_status='PUBLICATION_FAILED',updated_at=? WHERE id=? AND publishing_status IN ('SCHEDULED','PROCESSING')")
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
        if os.getenv("GCF_ENABLE_VISION_ANALYSIS","").lower()=="true":
            try: maximum=max(1,min(100,int(os.getenv("GCF_VISION_MAX_CANDIDATES_PER_MEDIA","25"))))
            except (TypeError,ValueError): maximum=25
            c=connect(self.db)
            try:
                candidates=c.execute("SELECT id FROM candidate_moments WHERE media_id=? AND state='NEEDS_REVIEW' ORDER BY created_at DESC LIMIT ?",(job["media_id"],maximum)).fetchall()
                queued=0; stamp=now()
                for candidate in candidates:
                    active=c.execute("SELECT id FROM jobs WHERE job_type='ANALYZE_CANDIDATE_VISION' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ?",("%"+candidate["id"]+"%",)).fetchone()
                    if active: continue
                    jid=str(uuid4())
                    c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,'ANALYZE_CANDIDATE_VISION','QUEUED',?,?, ?,?,?)",
                              (jid,job["source_id"],job["media_id"],json.dumps({"candidate_id":candidate["id"]}),stamp,stamp))
                    queued+=1
                if queued:
                    event(c,"INFO","vision.batch_queued","Optional local vision assessment jobs queued after media analysis.",{"media_id":job["media_id"],"queued":queued,"maximum":maximum})
                    c.commit()
            finally:
                c.close()

    def render_candidate(self, job):
        if not self.ffmpeg or not self.ffprobe:
            raise RuntimeError("FFmpeg/FFprobe not configured.")
        payload=json.loads(job["payload_json"] or "{}")
        cid=payload.get("candidate_id")
        if not cid:
            raise RuntimeError("Render job is missing candidate_id.")
        c=connect(self.db)
        try:
            row=c.execute("""SELECT cm.*,m.original_path,m.file_name,m.source_id,p.status AS rights_status,p.expires_at,
                cp.narration_text,cp.narration_audio_path,cp.narration_audio_sha256,
                cp.narration_audio_duration_seconds,cp.narration_audio_status
                FROM candidate_moments cm
                JOIN media_assets m ON m.id=cm.media_id
                JOIN source_permissions p ON p.source_id=m.source_id
                LEFT JOIN content_plans cp ON cp.candidate_id=cm.id
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
            narration_path=None
            narration_duration=None
            narration_sha=None
            narration_requested=bool(row["narration_audio_status"] and row["narration_audio_status"] in {"QUEUED","SYNTHESIZING"})
            if row["narration_audio_status"]=="READY":
                if not row["narration_audio_path"] or not row["narration_audio_sha256"]:
                    raise RuntimeError("Narration is marked READY but its audio artifact is missing from the content plan.")
                narration_path=Path(row["narration_audio_path"]).resolve()
                try: narration_path.relative_to((self.db.parent/"outputs").resolve())
                except ValueError: raise PermissionError("Narration audio must remain inside the managed outputs directory.")
                if not narration_path.is_file() or narration_path.stat().st_size<=44:
                    raise FileNotFoundError("Narration audio is missing or empty; synthesize narration again.")
                audio_hash=hashlib.sha256()
                with narration_path.open("rb") as audio_handle:
                    for block in iter(lambda:audio_handle.read(1024*1024),b""): audio_hash.update(block)
                if audio_hash.hexdigest()!=row["narration_audio_sha256"]:
                    raise PermissionError("Narration audio hash no longer matches the stored artifact.")
                try:
                    with wave.open(str(narration_path),"rb") as voice:
                        narration_duration=voice.getnframes()/float(voice.getframerate())
                except (wave.Error,EOFError,OSError) as exc:
                    raise RuntimeError("Narration audio is not a valid WAV file.") from exc
                if narration_duration>end-start+0.25:
                    raise ValueError(f"Narration takes {narration_duration:.1f}s but this clip is only {end-start:.1f}s. Shorten the script or select a longer candidate.")
                narration_sha=audio_hash.hexdigest()
            elif narration_requested:
                raise RuntimeError("Narration synthesis is still in progress; wait for READY before rendering the voiceover.")
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
            render_profile=str(payload.get("render_profile","fit_pad"))
            subtitle_filter=None
            if caption_rows:
                caption_path=output.with_suffix(".srt")
                blocks=[]
                cap_index=0
                for cap in caption_rows:
                    cap_start=max(start,float(cap["start_seconds"]))-start
                    cap_end=min(end,float(cap["end_seconds"]))-start
                    text_line=str(cap["text"]).replace("-->","→").replace("\r"," ").replace("\n"," ").strip()
                    if not text_line or cap_end<=cap_start: continue
                    cap_index+=1
                    blocks.append(f"{cap_index}\n{srt_time(cap_start)} --> {srt_time(cap_end)}\n{text_line}\n")
                if blocks:
                    with caption_path.open("x",encoding="utf-8",newline="\n") as sidecar:
                        sidecar.write("\n".join(blocks))
                    if os.getenv("GCF_BURN_CAPTIONS","true").lower()=="true":
                        filters=run([self.ffmpeg,"-hide_banner","-filters"],15)
                        if " subtitles " in filters.stdout or re.search(r"\bsubtitles\s+V->V",filters.stdout):
                            escaped=escape_filter_path(caption_path)
                            subtitle_filter="subtitles=filename='"+escaped+"':force_style='FontName=Arial,FontSize=24,Outline=2,Shadow=0,MarginV=180'"
                            captions_burned=True
                        else:
                            caption_filter_warning=True
            # Both profiles keep the complete original game frame; blur_fill uses a darkened,
            # blurred enlargement of that same frame to avoid black bars without cropping the foreground.
            rendered_filter=build_video_filter(render_profile,subtitle_filter if captions_burned else None)
            args=[self.ffmpeg,"-hide_banner","-nostdin","-n","-ss",f"{start:.3f}","-i",str(source)]
            if narration_path:
                args += ["-i",str(narration_path)]
            args += ["-t",f"{end-start:.3f}"]
            audio_graph=None
            if narration_path and source_has_audio:
                audio_graph=(
                    "[0:a]aformat=sample_rates=48000:channel_layouts=stereo[gameaudio];"
                    "[1:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[voicekey][voice];"
                    "[gameaudio][voicekey]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[ducked];"
                    "[ducked][voice]amix=inputs=2:duration=first:dropout_transition=0,alimiter=limit=0.95[aout]"
                )
            if rendered_filter["mode"]=="vf":
                args += ["-map",rendered_filter["video_map"],"-vf",rendered_filter["filter"]]
                if audio_graph:
                    args += ["-filter_complex",audio_graph,"-map","[aout]"]
                elif narration_path:
                    args += ["-map","1:a:0"]
                else:
                    args += ["-map","0:a?"]
            else:
                graph=rendered_filter["filter"]
                if audio_graph:
                    graph += ";"+audio_graph
                args += ["-filter_complex",graph,"-map",rendered_filter["video_map"]]
                if audio_graph:
                    args += ["-map","[aout]"]
                elif narration_path:
                    args += ["-map","1:a:0"]
                else:
                    args += ["-map","0:a?"]
            args += ["-c:v","libx264","-preset","veryfast","-crf","21","-pix_fmt","yuv420p","-c:a","aac","-b:a","128k","-movflags","+faststart",str(output)]
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
            # Create a local portrait poster from the completed render for dashboard/review use.
            # This image is a local preview asset, not a YouTube custom-thumbnail upload.
            thumbnail=output.with_suffix(".thumbnail.jpg")
            thumbnail_path=None
            thumbnail_hash=None
            thumbnail_width=None
            thumbnail_height=None
            thumbnail_error=None
            try:
                sample_time=max(0.0,min(max(0.0,duration-0.05),duration/2.0))
                thumb_filter="scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black"
                run([self.ffmpeg,"-hide_banner","-nostdin","-n","-ss",f"{sample_time:.3f}","-i",str(output),"-frames:v","1","-map","0:v:0","-vf",thumb_filter,"-q:v","2",str(thumbnail)],min(self.timeout,60))
                if not thumbnail.is_file() or thumbnail.stat().st_size<=0:
                    raise RuntimeError("FFmpeg did not produce a nonempty poster image.")
                thumb_probe=run([self.ffprobe,"-v","error","-print_format","json","-show_streams",str(thumbnail)],min(30,self.timeout))
                thumb_data=json.loads(thumb_probe.stdout)
                thumb_video=next((x for x in thumb_data.get("streams",[]) if x.get("codec_type")=="video"),None)
                if not thumb_video or thumb_video.get("width")!=720 or thumb_video.get("height")!=1280:
                    raise RuntimeError("Poster image failed the expected 720x1280 dimension check.")
                th=hashlib.sha256()
                with thumbnail.open("rb") as image_file:
                    for block in iter(lambda:image_file.read(1024*1024),b""): th.update(block)
                thumbnail_path=str(thumbnail)
                thumbnail_hash=th.hexdigest()
                thumbnail_width=int(thumb_video["width"])
                thumbnail_height=int(thumb_video["height"])
            except Exception as exc:
                thumbnail_error=f"{type(exc).__name__}: {str(exc)[:400]}"
                try: thumbnail.unlink(missing_ok=True)
                except OSError: pass
                warnings.append("local_thumbnail_generation_failed")
            report={"passed":qc_status=="PASS","checks":{"nonempty":True,"video_stream":True,"dimensions":"1080x1920","video_codec":video.get("codec_name"),"audio_codec":audio.get("codec_name") if audio else None,"duration_seconds":duration,"source_audio_present":source_has_audio,"output_audio_present":bool(audio),"narration_mixed":bool(narration_path),"narration_audio_path":str(narration_path) if narration_path else None,"narration_audio_sha256":narration_sha,"narration_audio_duration_seconds":narration_duration,"ffmpeg_decode_scan":"PASS","black_intervals":black_events,"freeze_intervals":freeze_events,"local_thumbnail_generated":bool(thumbnail_path),"local_thumbnail_dimensions":[thumbnail_width,thumbnail_height] if thumbnail_path else None,"local_thumbnail_error":thumbnail_error},"layout":render_profile,"render_profile":render_profile,"profile_notes":profile_warning(render_profile),"caption_safe_area":{"style_margin_v":180,"automated_visual_safe_area_check":False},"caption_sidecar_path":str(caption_path) if caption_path and caption_path.is_file() else None,"captions_burned":captions_burned,"warnings":warnings}
            out_id=str(uuid4()); t=now()
            c.execute("""INSERT INTO rendered_outputs(id,candidate_id,media_id,output_path,file_size,sha256,duration_seconds,width,height,video_codec,audio_codec,qc_status,qc_report_json,created_at,render_profile,thumbnail_path,thumbnail_sha256,thumbnail_width,thumbnail_height)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(out_id,cid,row["media_id"],str(output),output.stat().st_size,h.hexdigest(),duration,video["width"],video["height"],video.get("codec_name","unknown"),audio.get("codec_name") if audio else None,qc_status,json.dumps(report),t,render_profile,thumbnail_path,thumbnail_hash,thumbnail_width,thumbnail_height))
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
                ro.thumbnail_path AS poster_path,m.source_id,p.status AS rights_status,p.expires_at,s.game
                FROM published_videos pv JOIN rendered_outputs ro ON ro.id=pv.rendered_output_id
                JOIN media_assets m ON m.id=ro.media_id JOIN sources s ON s.id=m.source_id
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
            # Re-read pause immediately before starting the non-idempotent remote upload.
            # The check is separate from the earlier queue claim to close the pause race.
            pause=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            # Remote upload always starts private. A separate worker polls processing status,
            # then schedules public release only after YouTube reports processing succeeded.
            if pause and pause["value"]=="true":
                t=now()
                due=(datetime.now(timezone.utc)+__import__("datetime").timedelta(seconds=15)).isoformat(timespec="seconds")
                c.execute("UPDATE published_videos SET publishing_status='UPLOAD_PENDING',upload_request_started_at=NULL,updated_at=? WHERE id=? AND publishing_status='UPLOADING'",(t,published_id))
                c.execute("UPDATE jobs SET status='QUEUED',error='Global pause enabled before upload started; waiting for resume.',updated_at=?,finished_at=NULL,next_attempt_at=? WHERE id=?",(t,due,job["id"]))
                event(c,"WARNING","youtube.upload_deferred_by_pause","Global pause prevented the remote upload from starting.",{"job_id":job["id"],"published_video_id":published_id,"next_attempt_at":due})
                c.commit()
                return
            # Generate a designed cover from the actual rendered poster and the owner-selected title.
            # Cover failures are non-fatal to the video upload and remain visible in history.
            cover=None; cover_error=""
            try:
                poster=Path(row["poster_path"]) if row["poster_path"] else None
                if not poster or not poster.is_file():
                    raise FileNotFoundError("The render has no valid local poster frame.")
                cover_path=self.db.parent/"outputs"/(published_id+"_youtube_cover.jpg")
                cover=generate_shorts_thumbnail(poster,row["title"],row["game"],cover_path)
            except Exception as exc:
                cover_error=f"{type(exc).__name__}: {str(exc)[:1000]}"
            t=now()
            if cover:
                c.execute("""UPDATE published_videos SET custom_thumbnail_path=?,custom_thumbnail_sha256=?,
                    custom_thumbnail_width=?,custom_thumbnail_height=?,custom_thumbnail_status='GENERATED',
                    custom_thumbnail_error='',updated_at=? WHERE id=?""",
                    (cover["path"],cover["sha256"],cover["width"],cover["height"],t,published_id))
                event(c,"INFO","youtube.thumbnail_generated","Generated a title-led Shorts cover from the rendered video frame.",{
                    "published_video_id":published_id,"thumbnail_path":cover["path"],"sha256":cover["sha256"],
                    "width":cover["width"],"height":cover["height"],"file_size":cover["file_size"],"design_version":cover["design_version"]})
            else:
                c.execute("UPDATE published_videos SET custom_thumbnail_status='GENERATION_FAILED',custom_thumbnail_error=?,updated_at=? WHERE id=?",
                    (cover_error,t,published_id))
                event(c,"WARNING","youtube.thumbnail_generation_failed","Custom cover generation failed; video upload will continue without it.",{
                    "published_video_id":published_id,"error":cover_error})
            c.commit()
            # Re-check rights and pause after cover generation, immediately before the non-idempotent upload.
            latest=c.execute("""SELECT p.status AS rights_status,p.expires_at FROM media_assets m
                JOIN source_permissions p ON p.source_id=m.source_id WHERE m.id=?""",(row["media_id"],)).fetchone()
            if not latest or latest["rights_status"]!="AUTHORIZED" or expiry_passed(latest["expires_at"]):
                raise PermissionError("Rights gate blocked upload: permission expired or was revoked during preparation.")
            pause=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if pause and pause["value"]=="true":
                t=now()
                due=(datetime.now(timezone.utc)+__import__("datetime").timedelta(seconds=15)).isoformat(timespec="seconds")
                c.execute("UPDATE published_videos SET publishing_status='UPLOAD_PENDING',upload_request_started_at=NULL,updated_at=? WHERE id=? AND publishing_status='UPLOADING'",(t,published_id))
                c.execute("UPDATE jobs SET status='QUEUED',error='Global pause enabled before upload started; waiting for resume.',updated_at=?,finished_at=NULL,next_attempt_at=? WHERE id=?",(t,due,job["id"]))
                event(c,"WARNING","youtube.upload_deferred_by_pause","Global pause prevented the remote upload from starting after local cover generation.",{"job_id":job["id"],"published_video_id":published_id,"next_attempt_at":due})
                c.commit()
                return
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
            row=c.execute("""SELECT pv.*,m.source_id,m.id AS media_id,p.status AS rights_status,p.expires_at
                FROM published_videos pv JOIN media_assets m ON m.id=pv.media_id
                JOIN source_permissions p ON p.source_id=m.source_id WHERE pv.id=?""",(published_id,)).fetchone()
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
                rights_current=row["rights_status"]=="AUTHORIZED" and not expiry_passed(row["expires_at"])
                rights_cover_schedule=rights_current
                if scheduled_at and rights_current and row["expires_at"]:
                    try:
                        expiry_at=datetime.fromisoformat(row["expires_at"].replace("Z","+00:00"))
                        scheduled_dt=datetime.fromisoformat(scheduled_at.replace("Z","+00:00"))
                        if expiry_at.tzinfo is None: expiry_at=expiry_at.replace(tzinfo=timezone.utc)
                        if scheduled_dt.tzinfo is None: scheduled_dt=scheduled_dt.replace(tzinfo=timezone.utc)
                        rights_cover_schedule=expiry_at>scheduled_dt
                    except (ValueError,TypeError):
                        rights_cover_schedule=False
                upload_enabled=c.execute("SELECT value FROM settings WHERE key='youtube_uploads_enabled'").fetchone()
                upload_gate=bool(upload_enabled and upload_enabled["value"]=="true" and os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()=="true")
                public_gate=os.getenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","").lower()=="true"
                scheduled_job_id=None
                if scheduled_at and not rights_cover_schedule:
                    final_state="BLOCKED_RIGHTS"
                    c.execute("UPDATE jobs SET status='CANCELLED',error='Rights are expired/revoked or expire before scheduled release.',updated_at=?,finished_at=? WHERE job_type='PUBLISH_YOUTUBE' AND status='QUEUED' AND payload_json LIKE ?",(t,t,"%"+published_id+"%"))
                elif scheduled_at and upload_gate and public_gate:
                    final_state="SCHEDULED"
                else:
                    final_state="READY_PRIVATE"
                    if scheduled_at:
                        # A missing owner/environment gate cancels release; keep the uploaded video private.
                        c.execute("UPDATE published_videos SET scheduled_publish_at=NULL WHERE id=?",(published_id,))
                        c.execute("UPDATE jobs SET status='CANCELLED',error='Public release gate was disabled; video remains private.',updated_at=?,finished_at=? WHERE job_type='PUBLISH_YOUTUBE' AND status='QUEUED' AND payload_json LIKE ?",(t,t,"%"+published_id+"%"))
                c.execute("UPDATE published_videos SET remote_processing_status='succeeded',processing_checked_at=?,processing_failure_reason='',publishing_status=?,updated_at=? WHERE id=?",
                          (t,final_state,t,published_id))
                # Generate the custom cover before any scheduled public-state change. It is a
                # separate, idempotent job so thumbnail permission/quota failures do not break upload.
                thumb=c.execute("SELECT custom_thumbnail_path,custom_thumbnail_status FROM published_videos WHERE id=?",(published_id,)).fetchone()
                if rights_current and thumb and thumb["custom_thumbnail_path"] and thumb["custom_thumbnail_status"]=="GENERATED":
                    thumb_active=c.execute("SELECT id FROM jobs WHERE job_type='SET_YOUTUBE_THUMBNAIL' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ? LIMIT 1",("%"+published_id+"%",)).fetchone()
                    if not thumb_active:
                        thumb_job_id=str(uuid4())
                        c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,'SET_YOUTUBE_THUMBNAIL','QUEUED',?,?, ?,?,?)",
                            (thumb_job_id,row["source_id"],row["media_id"],json.dumps({"published_video_id":published_id}),t,t))
                        c.execute("UPDATE published_videos SET custom_thumbnail_status='SET_PENDING',updated_at=? WHERE id=?",(t,published_id))
                if final_state=="SCHEDULED":
                    existing=c.execute("SELECT id FROM jobs WHERE job_type='PUBLISH_YOUTUBE' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ? ORDER BY created_at DESC LIMIT 1",("%"+published_id+"%",)).fetchone()
                    if not existing:
                        parsed=datetime.fromisoformat(scheduled_at.replace("Z","+00:00"))
                        if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
                        due=max(parsed,datetime.now(timezone.utc)).isoformat(timespec="seconds")
                        scheduled_job_id=str(uuid4())
                        c.execute("""INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at,next_attempt_at)
                            VALUES(?,'PUBLISH_YOUTUBE','QUEUED',?,?, ?,?,?,?)""",
                            (scheduled_job_id,row["source_id"],row["media_id"],json.dumps({"published_video_id":published_id,"publish_at":parsed.isoformat()}),t,t,due))
                c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",
                          (json.dumps({"published_video_id":published_id,"processing_status":"succeeded","publishing_status":final_state,"scheduled_publish_job_id":scheduled_job_id,"rights_current":rights_current,"rights_cover_schedule":rights_cover_schedule}),t,t,job["id"]))
                event_level="WARNING" if final_state=="BLOCKED_RIGHTS" else "INFO"
                event_kind="youtube.scheduled_release_blocked" if final_state=="BLOCKED_RIGHTS" else "youtube.processing_succeeded"
                event(c,event_level,event_kind,"YouTube processing succeeded; public-release conditions were re-evaluated.",{"job_id":job["id"],"published_video_id":published_id,"youtube_video_id":video_id,"publishing_status":final_state,"scheduled_publish_job_id":scheduled_job_id,"rights_current":rights_current,"rights_cover_schedule":rights_cover_schedule,"upload_gate":upload_gate,"public_gate":public_gate})
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

    def generate_youtube_thumbnail(self, job):
        payload=json.loads(job["payload_json"] or "{}")
        published_id=payload.get("published_video_id")
        if not published_id:
            raise ValueError("Thumbnail generation job is missing published_video_id.")
        c=connect(self.db)
        try:
            row=c.execute("""SELECT pv.*,ro.qc_status AS render_qc,ro.sha256 AS render_sha,ro.file_size AS render_size,
                ro.thumbnail_path AS poster_path,m.source_id,s.game,p.status AS rights_status,p.expires_at
                FROM published_videos pv JOIN media_assets m ON m.id=pv.media_id
                JOIN sources s ON s.id=m.source_id
                JOIN source_permissions p ON p.source_id=m.source_id
                LEFT JOIN rendered_outputs ro ON ro.id=pv.rendered_output_id
                WHERE pv.id=?""",(published_id,)).fetchone()
            if not row:
                raise FileNotFoundError("Publication record not found for custom-thumbnail generation.")
            if row["rights_status"]!="AUTHORIZED" or expiry_passed(row["expires_at"]):
                raise PermissionError("Rights gate blocked custom-thumbnail generation.")
            if row["qc_status"]!="PASS" or row["render_qc"]!="PASS":
                raise PermissionError("Custom-thumbnail generation requires a QC-passed output.")
            output=Path(row["output_path"])
            if not output.is_file() or output.stat().st_size!=row["render_size"]:
                raise PermissionError("Custom-thumbnail generation blocked: render is missing or its size changed.")
            out_hash=hashlib.sha256()
            with output.open("rb") as stream:
                for block in iter(lambda:stream.read(1024*1024),b""): out_hash.update(block)
            if out_hash.hexdigest()!=row["render_sha"]:
                raise PermissionError("Custom-thumbnail generation blocked: render hash no longer matches QC.")
            poster=Path(row["poster_path"]).resolve() if row["poster_path"] else None
            if not poster or not poster.is_file():
                raise FileNotFoundError("Source render poster frame was not generated. Render the clip again to produce a poster frame.")
            try: poster.relative_to((self.db.parent/"outputs").resolve())
            except ValueError: raise PermissionError("Poster path is outside the managed outputs directory.")
            pause=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if pause and pause["value"]=="true":
                raise PermissionError("Global pause blocked custom-thumbnail generation.")
            title=row["title"]
            game=row["game"]
            destination=self.db.parent/"outputs"/(published_id+"_youtube_cover.jpg")
            old_thumb_hash=row["custom_thumbnail_sha256"]
        finally:
            c.close()
        generated=generate_shorts_thumbnail(poster,title,game,destination)
        c=connect(self.db)
        try:
            # Recheck rights and output identity after local image processing.
            latest=c.execute("""SELECT pv.youtube_video_id,pv.remote_processing_status,pv.scheduled_publish_at,
                pv.custom_thumbnail_sha256,p.status AS rights_status,p.expires_at,
                ro.qc_status AS render_qc,ro.sha256 AS render_sha,ro.file_size AS render_size
                FROM published_videos pv JOIN media_assets m ON m.id=pv.media_id
                JOIN source_permissions p ON p.source_id=m.source_id
                LEFT JOIN rendered_outputs ro ON ro.id=pv.rendered_output_id WHERE pv.id=?""",(published_id,)).fetchone()
            if not latest:
                raise FileNotFoundError("Publication disappeared while custom-thumbnail generation was running.")
            if latest["rights_status"]!="AUTHORIZED" or expiry_passed(latest["expires_at"]):
                raise PermissionError("Rights expired during custom-thumbnail generation; cover was not registered.")
            if latest["render_qc"]!="PASS" or latest["render_sha"]!=row["render_sha"] or latest["render_size"]!=row["render_size"]:
                raise PermissionError("Render QC/hash changed during custom-thumbnail generation; cover was not registered.")
            t=now()
            c.execute("""UPDATE published_videos SET custom_thumbnail_path=?,custom_thumbnail_sha256=?,
                custom_thumbnail_width=?,custom_thumbnail_height=?,custom_thumbnail_status='GENERATED',
                custom_thumbnail_error='',updated_at=? WHERE id=?""",
                (generated["path"],generated["sha256"],generated["width"],generated["height"],t,published_id))
            thumb_job_id=None
            enabled=c.execute("SELECT value FROM settings WHERE key='youtube_uploads_enabled'").fetchone()
            uploads_enabled=bool(enabled and enabled["value"]=="true" and os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()=="true")
            if latest["youtube_video_id"] and latest["remote_processing_status"]=="succeeded" and uploads_enabled:
                active=c.execute("SELECT id FROM jobs WHERE job_type='SET_YOUTUBE_THUMBNAIL' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ? LIMIT 1",("%"+published_id+"%",)).fetchone()
                if not active:
                    thumb_job_id=str(uuid4())
                    c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,'SET_YOUTUBE_THUMBNAIL','QUEUED',?,?, ?,?,?)",
                        (thumb_job_id,row["source_id"],row["media_id"],json.dumps({"published_video_id":published_id,"generated_automatically":True}),t,t))
                    c.execute("UPDATE published_videos SET custom_thumbnail_status='SET_PENDING' WHERE id=?",(published_id,))
            summary={"published_video_id":published_id,"thumbnail_path":generated["path"],"sha256":generated["sha256"],
                "width":generated["width"],"height":generated["height"],"file_size":generated["file_size"],
                "design_version":generated["design_version"],"upload_job_id":thumb_job_id}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",
                (json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","youtube.thumbnail_regenerated","Custom Shorts cover generated from the QC-passed render poster.",{"job_id":job["id"],**summary})
            c.commit()
        finally:
            c.close()

    def set_youtube_thumbnail(self, job):
        payload=json.loads(job["payload_json"] or "{}")
        published_id=payload.get("published_video_id")
        if not published_id:
            raise ValueError("Thumbnail job is missing published_video_id.")
        c=connect(self.db)
        try:
            row=c.execute("""SELECT pv.*,ro.qc_status AS render_qc,ro.sha256 AS render_sha,ro.file_size AS render_size,
                p.status AS rights_status,p.expires_at
                FROM published_videos pv JOIN media_assets m ON m.id=pv.media_id
                JOIN source_permissions p ON p.source_id=m.source_id
                LEFT JOIN rendered_outputs ro ON ro.id=pv.rendered_output_id
                WHERE pv.id=?""",(published_id,)).fetchone()
            if not row: raise FileNotFoundError("Publication record not found for thumbnail upload.")
            if not row["youtube_video_id"]:
                raise PermissionError("Thumbnail upload requires a recorded YouTube video ID.")
            if row["remote_processing_status"]!="succeeded":
                raise PermissionError("Thumbnail upload waits until YouTube processing succeeds.")
            if row["rights_status"]!="AUTHORIZED" or expiry_passed(row["expires_at"]):
                raise PermissionError("Rights gate blocked custom-thumbnail upload: current permission is required.")
            if row["qc_status"]!="PASS" or row["render_qc"]!="PASS":
                raise PermissionError("Custom-thumbnail upload requires a QC-passed render.")
            if os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()!="true":
                raise PermissionError("YouTube uploads are disabled by environment policy.")
            enabled=c.execute("SELECT value FROM settings WHERE key='youtube_uploads_enabled'").fetchone()
            if not enabled or enabled["value"]!="true":
                raise PermissionError("YouTube uploads are disabled in database settings.")
            image_path=Path(row["custom_thumbnail_path"]).resolve() if row["custom_thumbnail_path"] else None
            if not image_path or not image_path.is_file() or image_path.stat().st_size<=0:
                raise FileNotFoundError("Generated custom thumbnail is missing.")
            try: image_path.relative_to((self.db.parent/"outputs").resolve())
            except ValueError: raise PermissionError("Custom thumbnail path is outside the managed outputs directory.")
            if not row["custom_thumbnail_sha256"] or hashlib.sha256(image_path.read_bytes()).hexdigest()!=row["custom_thumbnail_sha256"]:
                raise PermissionError("Custom thumbnail hash does not match its stored artifact.")
            if row["custom_thumbnail_width"] not in {1080,2160} or row["custom_thumbnail_height"] not in {1920,3840}:
                raise PermissionError("Custom thumbnail dimensions are not a supported vertical profile.")
            try:
                from PIL import Image
                with Image.open(image_path) as cover:
                    cover.verify()
                with Image.open(image_path) as cover:
                    actual_size=cover.size
                    actual_format=cover.format
            except Exception as exc:
                raise PermissionError(f"Custom thumbnail failed image-integrity verification ({type(exc).__name__}).") from exc
            if actual_format!="JPEG" or actual_size!=(row["custom_thumbnail_width"],row["custom_thumbnail_height"]):
                raise PermissionError("Custom thumbnail file format/dimensions disagree with its registered metadata.")
            if actual_size[1] != actual_size[0]*16//9:
                raise PermissionError("Custom thumbnail must use a 9:16 vertical aspect ratio.")
            # Recheck mutable gates immediately before the external upload.
            pause=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if pause and pause["value"]=="true":
                t=now(); due=(datetime.now(timezone.utc)+__import__("datetime").timedelta(seconds=15)).isoformat(timespec="seconds")
                c.execute("UPDATE jobs SET status='QUEUED',error='Global pause prevented thumbnail upload; waiting for resume.',updated_at=?,finished_at=NULL,next_attempt_at=? WHERE id=?",(t,due,job["id"]))
                c.execute("UPDATE published_videos SET custom_thumbnail_status='SET_PENDING',updated_at=? WHERE id=?",(t,published_id))
                event(c,"WARNING","youtube.thumbnail_deferred_by_pause","Global pause prevented custom-thumbnail upload.",{"job_id":job["id"],"published_video_id":published_id,"next_attempt_at":due})
                c.commit()
                return
            latest=c.execute("""SELECT p.status AS rights_status,p.expires_at FROM media_assets m
                JOIN source_permissions p ON p.source_id=m.source_id WHERE m.id=?""",(row["media_id"],)).fetchone()
            if not latest or latest["rights_status"]!="AUTHORIZED" or expiry_passed(latest["expires_at"]):
                raise PermissionError("Rights gate blocked custom-thumbnail upload: permission expired or was revoked.")
            image_hash=hashlib.sha256(image_path.read_bytes()).hexdigest()
            if image_hash!=row["custom_thumbnail_sha256"]:
                raise PermissionError("Custom thumbnail changed during preparation.")
            video_id=row["youtube_video_id"]
            thumbnail_path=str(image_path)
            thumb_hash=row["custom_thumbnail_sha256"]
            t=now()
        finally:
            c.close()
        result=set_custom_thumbnail(video_id,Path(thumbnail_path))
        c=connect(self.db)
        try:
            # Do not attach the result to a different or since-reconciled remote video ID.
            current=c.execute("SELECT youtube_video_id,custom_thumbnail_sha256 FROM published_videos WHERE id=?",(published_id,)).fetchone()
            if not current or current["youtube_video_id"]!=video_id or current["custom_thumbnail_sha256"]!=thumb_hash:
                raise RuntimeError("Publication or thumbnail changed while YouTube thumbnail upload was in flight.")
            t=now()
            c.execute("UPDATE published_videos SET custom_thumbnail_status='SET_SUCCESS',custom_thumbnail_error='',custom_thumbnail_uploaded_at=?,updated_at=? WHERE id=?",(t,t,published_id))
            summary={"published_video_id":published_id,"youtube_video_id":video_id,"thumbnail_path":thumbnail_path,"sha256":thumb_hash,"uploaded":True,"remote_items":len(result.get("items",[]))}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",(json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","youtube.thumbnail_uploaded","Custom Shorts cover uploaded to the connected YouTube video.",{"job_id":job["id"],**summary})
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
            # Global pause has priority over schedule-time deferral and any remote mutation.
            pause=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if pause and pause["value"]=="true":
                t=now()
                due=(datetime.now(timezone.utc)+__import__("datetime").timedelta(seconds=15)).isoformat(timespec="seconds")
                c.execute("UPDATE jobs SET status='QUEUED',error='Global pause enabled before public state change; waiting for resume.',updated_at=?,finished_at=NULL,next_attempt_at=? WHERE id=?",(t,due,job["id"]))
                event(c,"WARNING","youtube.publication_deferred_by_pause","Global pause prevented the scheduled worker from changing remote video privacy.",{"job_id":job["id"],"published_video_id":published_id,"next_attempt_at":due})
                c.commit()
                return
            row=c.execute("""SELECT pv.*,ro.qc_status AS render_qc,ro.sha256 AS render_sha,ro.file_size AS render_size,
                p.status AS rights_status,p.expires_at
                FROM published_videos pv JOIN rendered_outputs ro ON ro.id=pv.rendered_output_id
                JOIN media_assets m ON m.id=ro.media_id JOIN source_permissions p ON p.source_id=m.source_id
                WHERE pv.id=?""",(published_id,)).fetchone()
            if not row:
                raise FileNotFoundError("Scheduled publication record not found.")
            if not row["youtube_video_id"]:
                raise PermissionError("Publication has no reconciled YouTube video ID.")
            if row["publishing_status"] not in {"SCHEDULED","PROCESSING"} or not row["scheduled_publish_at"]:
                raise PermissionError("Scheduled public release was cancelled or is no longer eligible.")
            try:
                scheduled_time=datetime.fromisoformat(row["scheduled_publish_at"].replace("Z","+00:00"))
            except ValueError as exc:
                raise PermissionError("Scheduled public release timestamp is invalid.") from exc
            if scheduled_time.tzinfo is None:
                scheduled_time=scheduled_time.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc)<scheduled_time:
                due=scheduled_time.astimezone(timezone.utc).isoformat(timespec="seconds")
                c.execute("UPDATE jobs SET status='QUEUED',error='Release time has not arrived; requeued for scheduled time.',updated_at=?,finished_at=NULL,next_attempt_at=? WHERE id=?",(now(),due,job["id"]))
                c.commit()
                return
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
            service=youtube_service()
            remote_items=service.videos().list(part="processingDetails,status",id=row["youtube_video_id"]).execute().get("items",[]) or []
            remote=remote_items[0] if remote_items else {}
            remote_processing=str((remote.get("processingDetails") or {}).get("processingStatus","unknown")).lower()
            remote_privacy=str((remote.get("status") or {}).get("privacyStatus","unknown")).lower()
            if remote_processing in {"failed","terminated"}:
                failure=(remote.get("processingDetails") or {}).get("processingFailureReason") or remote_processing
                c.execute("UPDATE published_videos SET remote_processing_status=?,processing_checked_at=?,processing_failure_reason=?,publishing_status='PROCESSING_FAILED',updated_at=? WHERE id=?",
                          (remote_processing,now(),str(failure),now(),published_id))
                c.commit()
                raise PermissionError(f"Scheduled publication blocked: YouTube processing status is {remote_processing} ({failure}).")
            if remote_processing!="succeeded":
                # A transient processing lag is not a publishing failure. Keep the release queued
                # and request another bounded processing check; public state remains unchanged.
                t=now()
                due=(datetime.now(timezone.utc)+__import__("datetime").timedelta(seconds=60)).isoformat(timespec="seconds")
                c.execute("UPDATE published_videos SET remote_processing_status=?,processing_checked_at=?,publishing_status='PROCESSING',updated_at=? WHERE id=?",
                          (remote_processing,t,t,published_id))
                active=c.execute("SELECT id FROM jobs WHERE job_type='CHECK_YOUTUBE_PROCESSING' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ? LIMIT 1",("%"+published_id+"%",)).fetchone()
                if not active:
                    check_id=str(uuid4())
                    c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at,next_attempt_at) VALUES(?,'CHECK_YOUTUBE_PROCESSING','QUEUED',?,?, ?,?,?,?)",
                              (check_id,row["source_id"],row["media_id"],json.dumps({"published_video_id":published_id,"poll_attempt":0,"max_polls":20}),t,t,due))
                c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",
                          (json.dumps({"published_video_id":published_id,"deferred":True,"reason":"YouTube processing is not yet successful","processing_status":remote_processing,"recheck_job_queued":True}),t,t,job["id"]))
                event(c,"WARNING","youtube.publication_deferred","Public release deferred because YouTube processing is not yet successful.",{"job_id":job["id"],"published_video_id":published_id,"processing_status":remote_processing,"next_attempt_at":due})
                c.commit()
                return
            if remote_privacy=="public":
                t=now()
                c.execute("UPDATE published_videos SET remote_processing_status='succeeded',processing_checked_at=?,publishing_status='PUBLISHED',updated_at=? WHERE id=?",(t,t,published_id))
                c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",
                          (json.dumps({"published_video_id":published_id,"youtube_video_id":row["youtube_video_id"],"publishing_status":"PUBLISHED","already_public":True}),t,t,job["id"]))
                event(c,"INFO","youtube.publication_confirmed","YouTube already reports this video public; remote state reconciled after rights and QC checks.",{"job_id":job["id"],"published_video_id":published_id,"youtube_video_id":row["youtube_video_id"]})
                c.commit()
                return
            if remote_privacy!="private":
                raise UploadReconciliationRequired(f"YouTube privacy state is unexpectedly '{remote_privacy}'. Reconcile the channel video before attempting public release.")
            if row["remote_processing_status"]!="succeeded":
                # Keep remote processing state as a recorded precondition; the live query above is authoritative.
                c.execute("UPDATE published_videos SET remote_processing_status='succeeded',processing_checked_at=?,updated_at=? WHERE id=?",(now(),now(),published_id))
            pause=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if pause and pause["value"]=="true":
                t=now()
                due=(datetime.now(timezone.utc)+__import__("datetime").timedelta(seconds=15)).isoformat(timespec="seconds")
                c.execute("UPDATE jobs SET status='QUEUED',error='Global pause enabled before public state change; waiting for resume.',updated_at=?,finished_at=NULL,next_attempt_at=? WHERE id=?",(t,due,job["id"]))
                event(c,"WARNING","youtube.publication_deferred_by_pause","Global pause prevented the remote video from being made public.",{"job_id":job["id"],"published_video_id":published_id,"next_attempt_at":due})
                c.commit()
                return
            t=now()
            try:
                service.videos().update(part="status",body={"id":row["youtube_video_id"],"status":{"privacyStatus":"public"}}).execute()
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



    def sync_youtube_analytics(self, job):
        """Fetch official Analytics rows and persist only the fields returned by YouTube."""
        payload=json.loads(job["payload_json"] or "{}")
        start_date=str(payload.get("start_date",""))[:10]
        end_date=str(payload.get("end_date",""))[:10]
        try:
            datetime.strptime(start_date,"%Y-%m-%d")
            datetime.strptime(end_date,"%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Analytics sync requires start_date and end_date in YYYY-MM-DD format.") from exc
        if end_date<start_date:
            raise ValueError("Analytics end_date cannot precede start_date.")
        rows=fetch_video_analytics(start_date,end_date,max_results=200)
        if not isinstance(rows,list):
            raise RuntimeError("YouTube Analytics adapter returned an unexpected response shape.")
        c=connect(self.db)
        try:
            inserted=0
            unmatched=0
            t=now()
            for metrics in rows:
                if not isinstance(metrics,dict):
                    continue
                video_id=str(metrics.get("video","")).strip()
                if not video_id:
                    continue
                record=c.execute("SELECT id FROM published_videos WHERE youtube_video_id=?",(video_id,)).fetchone()
                if not record:
                    unmatched+=1
                    continue
                snapshot={
                    "start_date":start_date,
                    "end_date":end_date,
                    "metrics":metrics,
                    "retrieved_at":t,
                }
                c.execute(
                    "INSERT INTO analytics_snapshots(id,published_video_id,captured_at,metrics_json,provider) VALUES(?,?,?,?,?)",
                    (str(uuid4()),record["id"],t,json.dumps(snapshot,ensure_ascii=False),"youtube_analytics_api"),
                )
                inserted+=1
            summary={
                "start_date":start_date,
                "end_date":end_date,
                "rows_returned":len(rows),
                "snapshots_saved":inserted,
                "unmatched_video_rows":unmatched,
            }
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",
                (json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","youtube.analytics_synced","Official YouTube Analytics data persisted without fabricating missing metrics.",{"job_id":job["id"],**summary})
            c.commit()
        finally:
            c.close()

    def analyze_candidate_vision(self, job):
        payload=json.loads(job["payload_json"] or "{}")
        cid=payload.get("candidate_id")
        if not cid: raise ValueError("Vision analysis job is missing candidate_id.")
        if not self.ffmpeg: raise RuntimeError("FFmpeg is required to sample candidate frames for vision assessment.")
        c=connect(self.db)
        try:
            row=c.execute("""SELECT cm.*,m.original_path,m.source_id,m.id AS media_id,s.game,s.title AS source_title,
                p.status AS rights_status,p.expires_at
                FROM candidate_moments cm JOIN media_assets m ON m.id=cm.media_id
                JOIN sources s ON s.id=m.source_id JOIN source_permissions p ON p.source_id=s.id
                WHERE cm.id=?""",(cid,)).fetchone()
            if not row: raise FileNotFoundError("Candidate for vision analysis not found.")
            if row["rights_status"]!="AUTHORIZED" or expiry_passed(row["expires_at"]):
                raise PermissionError("Rights gate blocked vision analysis: current AUTHORIZED permission is required.")
            if row["state"]!="NEEDS_REVIEW":
                raise PermissionError(f"Vision analysis only runs on human-review candidates (state={row['state']}).")
            source=Path(row["original_path"])
            if not source.is_file(): raise FileNotFoundError("Source media for vision analysis is missing.")
            start=float(row["start_seconds"]); end=float(row["end_seconds"])
            if start<0 or end<=start: raise ValueError("Candidate time boundaries are invalid.")
            transcript_rows=c.execute("""SELECT ts.start_seconds,ts.end_seconds,ts.text FROM transcript_segments ts
                JOIN transcripts tr ON tr.id=ts.transcript_id WHERE tr.media_id=? AND ts.end_seconds>? AND ts.start_seconds<?
                ORDER BY ts.start_seconds LIMIT 20""",(row["media_id"],start,end)).fetchall()
            transcript_excerpt=" ".join(str(x["text"]) for x in transcript_rows)[:1200]
            old_assessment=c.execute("SELECT id FROM candidate_vision_assessments WHERE candidate_id=?",(cid,)).fetchone()
            assessment_id=old_assessment["id"] if old_assessment else str(uuid4())
        finally:
            c.close()
        frame_times=[round(start+(end-start)*fraction,3) for fraction in (0.2,0.5,0.8)]
        images=[]
        with tempfile.TemporaryDirectory(prefix="gcf-vision-") as temp:
            for index,frame_time in enumerate(frame_times):
                frame=Path(temp)/f"frame-{index}.jpg"
                args=[self.ffmpeg,"-hide_banner","-loglevel","error","-nostdin","-ss",f"{frame_time:.3f}","-i",str(source),"-frames:v","1","-vf","scale=768:-2","-q:v","3","-y",str(frame)]
                run(args,min(45,self.timeout))
                if not frame.is_file() or frame.stat().st_size<=0:
                    raise RuntimeError(f"FFmpeg did not extract candidate sample frame at {frame_time:.3f}s.")
                images.append(frame.read_bytes())
        result=assess_candidate_images(images,str(row["game"] or ""),str(row["source_title"] or ""),transcript_excerpt)
        t=now()
        c=connect(self.db)
        try:
            # Re-check rights after inference, immediately before persisting/ranking.
            latest=c.execute("""SELECT cm.state,p.status AS rights_status,p.expires_at,cm.ranking_score,cm.ranking_components_json
                FROM candidate_moments cm JOIN media_assets m ON m.id=cm.media_id
                JOIN source_permissions p ON p.source_id=m.source_id WHERE cm.id=?""",(cid,)).fetchone()
            if not latest: raise FileNotFoundError("Candidate disappeared during vision analysis.")
            if latest["rights_status"]!="AUTHORIZED" or expiry_passed(latest["expires_at"]):
                raise PermissionError("Rights expired during vision analysis; results were not applied.")
            if latest["state"]!="NEEDS_REVIEW":
                raise PermissionError("Candidate state changed while vision analysis was running; results were not applied.")
            c.execute("""INSERT INTO candidate_vision_assessments(id,candidate_id,model_name,status,gameplay_context,event_type,visible_event,
                is_actionable_highlight,highlight_score,confidence,evidence_json,quality_issues_json,frame_times_json,raw_response_json,error,created_at,updated_at)
                VALUES(?,?,?,'COMPLETED',?,?,?,?,?,?,?,?,?,?, '',?,?)
                ON CONFLICT(candidate_id) DO UPDATE SET model_name=excluded.model_name,status='COMPLETED',
                gameplay_context=excluded.gameplay_context,event_type=excluded.event_type,visible_event=excluded.visible_event,
                is_actionable_highlight=excluded.is_actionable_highlight,highlight_score=excluded.highlight_score,confidence=excluded.confidence,
                evidence_json=excluded.evidence_json,quality_issues_json=excluded.quality_issues_json,frame_times_json=excluded.frame_times_json,
                raw_response_json=excluded.raw_response_json,error='',updated_at=excluded.updated_at""",
                (assessment_id,cid,result["model_name"],result["gameplay_context"],result["event_type"],result["visible_event"],
                 int(result["is_actionable_highlight"]),result["highlight_score"],result["confidence"],json.dumps(result["evidence"]),
                 json.dumps(result["quality_issues"]),json.dumps(frame_times),json.dumps(result),t,t))
            try: components=json.loads(latest["ranking_components_json"] or "{}")
            except (ValueError,TypeError): components={}
            base=float(latest["ranking_score"]) if latest["ranking_score"] is not None else 0.45
            confidence=result["confidence"]
            evidence_score=(0.7*result["highlight_score"]+0.3*base)
            combined=round(max(0,min(1,confidence*evidence_score+(1-confidence)*base)),4)
            components.update({"vision_model":result["model_name"],"vision_event_type":result["event_type"],
                "vision_confidence":confidence,"vision_highlight_score":result["highlight_score"],
                "vision_assessment_method":"local-vision-model-estimate","vision_score_calibrated":False,
                "vision_frame_times_seconds":frame_times})
            c.execute("UPDATE candidate_moments SET ranking_score=?,ranking_components_json=? WHERE id=?",(combined,json.dumps(components),cid))
            summary={"candidate_id":cid,"assessment_id":assessment_id,"model_name":result["model_name"],"event_type":result["event_type"],
                "highlight_score":result["highlight_score"],"confidence":confidence,"combined_ranking_score":combined,
                "score_calibrated":False,"automatic_approval":False,"sampled_frame_times_seconds":frame_times}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",(json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","vision.assessment_completed","Local vision assessment saved as a review aid; candidate remains human-review-only.",{"job_id":job["id"],**summary})
            c.commit()
        finally:
            c.close()

    def synthesize_narration(self, job):
        if os.getenv("GCF_ENABLE_NARRATION","").lower()!="true":
            raise PermissionError("Local narration is disabled by configuration.")
        payload=json.loads(job["payload_json"] or "{}")
        candidate_id=payload.get("candidate_id")
        plan_id=payload.get("content_plan_id")
        expected_script_sha=payload.get("script_sha256")
        if not candidate_id or not plan_id or not expected_script_sha:
            raise ValueError("Narration job is missing candidate, content-plan, or script identity.")
        c=connect(self.db)
        try:
            row=c.execute("""SELECT cp.id AS plan_id,cp.narration_text,cp.narration_audio_status,
                cm.id AS candidate_id,cm.media_id,cm.state,m.source_id,p.status AS rights_status,p.expires_at
                FROM content_plans cp
                JOIN candidate_moments cm ON cm.id=cp.candidate_id
                JOIN media_assets m ON m.id=cm.media_id
                JOIN source_permissions p ON p.source_id=m.source_id
                WHERE cp.id=? AND cm.id=?""",(plan_id,candidate_id)).fetchone()
            if not row: raise FileNotFoundError("Content plan or candidate no longer exists.")
            if row["rights_status"]!="AUTHORIZED" or expiry_passed(row["expires_at"]):
                raise PermissionError("Rights gate blocked narration: current AUTHORIZED permission is required.")
            if row["state"] not in {"NEEDS_REVIEW","APPROVED","RENDER_FAILED"}:
                raise PermissionError(f"Narration generation is not allowed for candidate state {row['state']}.")
            script=validate_script(row["narration_text"] or "")
            current_sha=hashlib.sha256(script.encode("utf-8")).hexdigest()
            if current_sha!=expected_script_sha:
                raise PermissionError("Narration script changed after this job was queued; save and queue the current script again.")
            c.execute("UPDATE content_plans SET narration_audio_status='SYNTHESIZING',narration_audio_error='',updated_at=? WHERE id=?",(now(),plan_id))
            c.commit()
            outdir=self.db.parent/"outputs"
            outdir.mkdir(parents=True,exist_ok=True)
            target=outdir/(candidate_id+"_"+uuid4().hex[:12]+"_narration.wav")
            media_id=row["media_id"]
        finally:
            c.close()
        generated=synthesize_piper(script,target)
        c=connect(self.db)
        try:
            latest=c.execute("""SELECT cp.narration_text,cm.state,p.status AS rights_status,p.expires_at
                FROM content_plans cp JOIN candidate_moments cm ON cm.id=cp.candidate_id
                JOIN media_assets m ON m.id=cm.media_id JOIN source_permissions p ON p.source_id=m.source_id
                WHERE cp.id=? AND cm.id=?""",(plan_id,candidate_id)).fetchone()
            if not latest:
                target.unlink(missing_ok=True)
                raise FileNotFoundError("Narration plan disappeared during synthesis.")
            latest_script=validate_script(latest["narration_text"] or "")
            latest_sha=hashlib.sha256(latest_script.encode("utf-8")).hexdigest()
            if latest["rights_status"]!="AUTHORIZED" or expiry_passed(latest["expires_at"]):
                target.unlink(missing_ok=True)
                raise PermissionError("Rights were expired or revoked during narration synthesis; audio was discarded.")
            if latest_sha!=expected_script_sha:
                target.unlink(missing_ok=True)
                raise PermissionError("Narration script changed during synthesis; audio was discarded.")
            t=now()
            c.execute("""UPDATE content_plans SET narration_audio_path=?,narration_audio_sha256=?,
                narration_audio_duration_seconds=?,narration_audio_status='READY',narration_audio_error='',
                narration_audio_generated_at=?,updated_at=? WHERE id=?""",
                (generated["path"],generated["sha256"],generated["duration_seconds"],t,t,plan_id))
            summary={"candidate_id":candidate_id,"content_plan_id":plan_id,"narration_audio_path":generated["path"],
                "sha256":generated["sha256"],"file_size":generated["file_size"],"duration_seconds":generated["duration_seconds"],
                "sample_rate":generated["sample_rate"],"channels":generated["channels"],"model_name":generated["model_name"]}
            c.execute("UPDATE jobs SET status='COMPLETED',payload_json=?,error='',updated_at=?,finished_at=? WHERE id=?",
                (json.dumps(summary),t,t,job["id"]))
            event(c,"INFO","narration.completed","Local narration audio generated from an owner-reviewed script.",{"job_id":job["id"],**summary})
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



    def schedule_due_analytics(self):
        """Queue one recent-window Analytics job on a persisted, opt-in schedule."""
        if not youtube_oauth_available():
            return
        c=connect(self.db)
        try:
            values={r["key"]:r["value"] for r in c.execute(
                "SELECT key,value FROM settings WHERE key IN ('global_paused','youtube_analytics_schedule_enabled','youtube_analytics_schedule_interval_minutes','youtube_analytics_schedule_window_days','youtube_analytics_schedule_next_at')")}
            if values.get("global_paused")=="true" or values.get("youtube_analytics_schedule_enabled")!="true":
                return
            next_at=values.get("youtube_analytics_schedule_next_at")
            if next_at:
                try:
                    due=datetime.fromisoformat(next_at.replace("Z","+00:00"))
                    if due.tzinfo is None: due=due.replace(tzinfo=timezone.utc)
                    if due>datetime.now(timezone.utc):
                        return
                except ValueError:
                    pass
            active=c.execute("SELECT id FROM jobs WHERE job_type='SYNC_YOUTUBE_ANALYTICS' AND status IN ('QUEUED','RUNNING') LIMIT 1").fetchone()
            if active:
                return
            try: interval=max(360,min(1440,int(values.get("youtube_analytics_schedule_interval_minutes","1440"))))
            except (TypeError,ValueError): interval=1440
            try: window=max(1,min(90,int(values.get("youtube_analytics_schedule_window_days","7"))))
            except (TypeError,ValueError): window=7
            # YouTube metrics can lag. End yesterday (UTC), not the incomplete current day.
            end_date=(datetime.now(timezone.utc).date()-__import__("datetime").timedelta(days=1))
            start_date=end_date-__import__("datetime").timedelta(days=window-1)
            t=now()
            jid=str(uuid4())
            payload={"start_date":start_date.isoformat(),"end_date":end_date.isoformat(),"scheduled":True,"window_days":window}
            c.execute("INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at) VALUES(?,'SYNC_YOUTUBE_ANALYTICS','QUEUED',?,?,?)",
                      (jid,json.dumps(payload),t,t))
            next_time=(datetime.now(timezone.utc)+__import__("datetime").timedelta(minutes=interval)).isoformat(timespec="seconds")
            c.execute("INSERT INTO settings(key,value,updated_at) VALUES('youtube_analytics_schedule_next_at',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                      (next_time,t))
            event(c,"INFO","youtube.analytics_scheduled_job_queued","Scheduled YouTube Analytics sync queued for a completed reporting window.",
                  {"job_id":jid,"start_date":payload["start_date"],"end_date":payload["end_date"],"next_run":next_time,"interval_minutes":interval})
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


    def scan_due_folder_watches(self):
        """Scan mapped local folders on a persisted cadence; only import files for a current authorized source."""
        t=now()
        c=connect(self.db)
        watches=[]
        try:
            pause=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if pause and pause["value"]=="true":
                return
            c.execute("BEGIN IMMEDIATE")
            watches=list(c.execute("""SELECT * FROM media_folder_watches
                WHERE enabled=1 AND (next_scan_at IS NULL OR next_scan_at<=?)
                ORDER BY created_at LIMIT 20""",(t,)))
            for watch in watches:
                due=(datetime.now(timezone.utc)+__import__("datetime").timedelta(seconds=max(15,min(3600,int(watch["scan_interval_seconds"]))))).isoformat(timespec="seconds")
                c.execute("UPDATE media_folder_watches SET last_scan_at=?,next_scan_at=?,last_error='' WHERE id=?",(t,due,watch["id"]))
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
        for watch in watches:
            try:
                self.scan_folder_watch(watch["id"])
            except Exception as exc:
                c=connect(self.db)
                try:
                    stamp=now()
                    c.execute("UPDATE media_folder_watches SET last_error=?,updated_at=? WHERE id=?",(f"{type(exc).__name__}: {str(exc)[:900]}",stamp,watch["id"]))
                    event(c,"ERROR","folder_watch.scan_failed","Local watched-folder scan failed.",{"watch_id":watch["id"],"error":str(exc)[:900]})
                    c.commit()
                finally:
                    c.close()

    def scan_folder_watch(self, watch_id, force=False):
        c=connect(self.db)
        try:
            watch=c.execute("SELECT * FROM media_folder_watches WHERE id=?",(watch_id,)).fetchone()
            if not watch:
                raise FileNotFoundError("Watched-folder record not found.")
            if not watch["enabled"] and not force:
                return {"watch_id":watch_id,"enabled":False,"files_seen":0,"imported":0}
            root=self.media_root.resolve()
            folder=(root/str(watch["relative_path"]).replace("\\\\","/")).resolve()
            try: folder.relative_to(root)
            except ValueError: raise PermissionError("Watched folder escapes the configured media root.")
            if not folder.is_dir():
                raise FileNotFoundError("Watched folder does not exist; create it under the media root.")
            source=c.execute("""SELECT s.id,p.status,p.expires_at FROM sources s
                JOIN source_permissions p ON p.source_id=s.id WHERE s.id=?""",(watch["source_id"],)).fetchone()
            if not source:
                raise FileNotFoundError("Watched-folder source no longer exists.")
            supported={".mp4",".mkv",".webm",".mov",".avi",".m4v"}
            files=[]
            if bool(watch["recursive"]):
                iterator=folder.rglob("*")
            else:
                iterator=folder.iterdir()
            for path in iterator:
                try:
                    if path.name.startswith(".") or path.is_symlink() or not path.is_file() or path.suffix.lower() not in supported:
                        continue
                    resolved=path.resolve()
                    resolved.relative_to(root)
                    resolved.relative_to(folder)
                    if resolved.stat().st_size<=0:
                        continue
                    files.append(resolved)
                except (OSError,ValueError):
                    continue
            files.sort(key=lambda p:str(p).casefold())
            c.close()
            return self._process_watched_files(watch,source,files,root)
        finally:
            try: c.close()
            except Exception: pass

    def _process_watched_files(self, watch, source, files, root):
        c=connect(self.db)
        imported=0; duplicates=0; pending=0; blocked=0
        try:
            t=now()
            stable_seconds=max(5,min(300,int(os.getenv("GCF_WATCH_STABLE_SECONDS","15") or "15")))
            for path in files:
                stat=path.stat()
                rel=path.relative_to(root).as_posix()
                prior=c.execute("SELECT * FROM watched_media_files WHERE watch_id=? AND relative_path=?",(watch["id"],rel)).fetchone()
                unchanged=bool(prior and int(prior["observed_size"])==stat.st_size and int(prior["observed_mtime_ns"])==stat.st_mtime_ns)
                if unchanged and prior["status"] in {"IMPORTED","DUPLICATE"}:
                    continue
                if unchanged:
                    try:
                        stable_since=datetime.fromisoformat(str(prior["stable_since"]).replace("Z","+00:00"))
                        if stable_since.tzinfo is None: stable_since=stable_since.replace(tzinfo=timezone.utc)
                        stable_age=(datetime.now(timezone.utc)-stable_since).total_seconds()
                    except (ValueError,TypeError):
                        stable_age=0
                    if stable_age<stable_seconds:
                        pending+=1
                        c.execute("UPDATE watched_media_files SET last_seen_at=? WHERE id=?",(t,prior["id"]))
                        continue
                else:
                    stable_since=t
                    if prior:
                        c.execute("UPDATE watched_media_files SET observed_size=?,observed_mtime_ns=?,stable_since=?,last_seen_at=?,status='PENDING_STABILITY',media_id=NULL,error='' WHERE id=?",
                                  (stat.st_size,stat.st_mtime_ns,t,t,prior["id"]))
                    else:
                        c.execute("""INSERT INTO watched_media_files(id,watch_id,relative_path,observed_size,observed_mtime_ns,stable_since,first_seen_at,last_seen_at,status)
                            VALUES(?,?,?,?,?,?,?,?,'PENDING_STABILITY')""",
                                  (str(uuid4()),watch["id"],rel,stat.st_size,stat.st_mtime_ns,t,t,t))
                    pending+=1
                    continue
                # Commit any stability observations, then hold a write lock from the
                # just-in-time rights check through registration to serialize with revocation.
                c.commit()
                c.execute("BEGIN IMMEDIATE")
                latest_rights=c.execute("SELECT status,expires_at FROM source_permissions WHERE source_id=?",(watch["source_id"],)).fetchone()
                if not latest_rights or latest_rights["status"]!="AUTHORIZED" or expiry_passed(latest_rights["expires_at"]):
                    blocked+=1
                    c.execute("""UPDATE watched_media_files SET status='SKIPPED_RIGHTS',error='Source is not currently authorized or its permission has expired.',last_seen_at=? WHERE watch_id=? AND relative_path=?""",(t,watch["id"],rel))
                    c.commit()
                    continue
                current=path.stat()
                if current.st_size!=stat.st_size or current.st_mtime_ns!=stat.st_mtime_ns:
                    c.execute("UPDATE watched_media_files SET observed_size=?,observed_mtime_ns=?,stable_since=?,status='PENDING_STABILITY',last_seen_at=? WHERE watch_id=? AND relative_path=?",
                              (current.st_size,current.st_mtime_ns,t,t,watch["id"],rel))
                    pending+=1
                    c.commit()
                    continue
                digest=hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda:stream.read(1024*1024),b""): digest.update(block)
                sha=digest.hexdigest()
                after_hash=path.stat()
                if after_hash.st_size!=current.st_size or after_hash.st_mtime_ns!=current.st_mtime_ns:
                    c.execute("UPDATE watched_media_files SET observed_size=?,observed_mtime_ns=?,stable_since=?,status='PENDING_STABILITY',last_seen_at=? WHERE watch_id=? AND relative_path=?",
                              (after_hash.st_size,after_hash.st_mtime_ns,t,t,watch["id"],rel))
                    pending+=1
                    c.commit()
                    continue
                dupe=c.execute("SELECT id FROM media_assets WHERE source_id=? AND sha256=?",(watch["source_id"],sha)).fetchone()
                if dupe:
                    duplicates+=1
                    c.execute("""UPDATE watched_media_files SET status='DUPLICATE',media_id=?,error='This exact file is already registered for the source.',last_seen_at=? WHERE watch_id=? AND relative_path=?""",
                              (dupe["id"],t,watch["id"],rel))
                    c.commit()
                    continue
                media_id=str(uuid4())
                c.execute("""INSERT INTO media_assets(id,source_id,original_path,file_name,file_size,sha256,state,created_at)
                    VALUES(?,?,?,?,?,?,'IMPORTED',?)""",
                    (media_id,watch["source_id"],str(path),path.name,current.st_size,sha,t))
                if bool(watch["auto_analyze"]):
                    active=c.execute("""SELECT id FROM jobs WHERE media_id=? AND job_type='ANALYZE_MEDIA'
                        AND status IN ('QUEUED','RUNNING') LIMIT 1""",(media_id,)).fetchone()
                    if not active:
                        job_id=str(uuid4())
                        c.execute("""INSERT INTO jobs(id,job_type,status,source_id,media_id,created_at,updated_at)
                            VALUES(?,'ANALYZE_MEDIA','QUEUED',?,?,?,?)""",(job_id,watch["source_id"],media_id,t,t))
                c.execute("""UPDATE watched_media_files SET status='IMPORTED',media_id=?,error='',last_seen_at=? WHERE watch_id=? AND relative_path=?""",
                          (media_id,t,watch["id"],rel))
                event(c,"INFO","folder_watch.media_imported","Stable, authorized local file was imported from a watched folder.",
                      {"watch_id":watch["id"],"media_id":media_id,"source_id":watch["source_id"],"file_name":path.name,"auto_analyze":bool(watch["auto_analyze"])})
                imported+=1
                c.commit()
            c.execute("UPDATE media_folder_watches SET last_error='',updated_at=? WHERE id=?",(t,watch["id"]))
            c.commit()
            return {"watch_id":watch["id"],"files_seen":len(files),"imported":imported,"duplicates":duplicates,"pending_stability":pending,"blocked_rights":blocked}
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

