import json, sqlite3, hashlib, subprocess, re, os
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone, date, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, AnyHttpUrl, Field, model_validator
from .config import Settings
from .db import initialize, connect, session, now, event, recover_interrupted_jobs
from .worker import Worker
from .integrations.youtube import connected_status

class SourceIn(BaseModel):
    source_url: AnyHttpUrl
    platform: str = Field(min_length=1,max_length=80)
    title: str = Field(default="",max_length=300)
    creator: str = Field(default="",max_length=200)
    game: str = Field(default="",max_length=120)
class RightsIn(BaseModel):
    status: str
    license_type: str = ""
    permission_evidence: str = ""
    editing_allowed: bool = False
    commercial_use_allowed: bool = False
    reviewed_by_owner: bool = False
    attribution: str = ""
    expires_at: str | None = None
    notes: str = ""
    @model_validator(mode="after")
    def check_authorized(self):
        if self.status not in {"UNKNOWN","DISCOVERED","EVIDENCE_REQUIRED","PENDING_REVIEW","AUTHORIZED","EXPIRED","REJECTED","REVOKED"}: raise ValueError("Invalid rights status")
        if self.status == "AUTHORIZED" and not (self.license_type.strip() and self.permission_evidence.strip() and self.editing_allowed and self.commercial_use_allowed and self.reviewed_by_owner):
            raise ValueError("AUTHORIZATION requires license type, permission evidence, editing/commercial permission, and owner review")
        if self.status == "AUTHORIZED" and expiry_passed(self.expires_at):
            raise ValueError("Cannot authorize a source with an expired or invalid expiration date")
        return self
class MediaIn(BaseModel):
    source_id: str
    path: str
class CandidateDecisionIn(BaseModel):
    decision: str
    notes: str = ""
    @model_validator(mode="after")
    def check_decision(self):
        if self.decision not in {"approve","reject"}:
            raise ValueError("decision must be approve or reject")
        return self
class TwitchDiscoveryIn(BaseModel):
    game_queries: list[str] = Field(min_length=1,max_length=20)
    days: int = Field(default=7,ge=1,le=30)
    max_results: int = Field(default=100,ge=1,le=100)
    @model_validator(mode="after")
    def normalize_queries(self):
        cleaned=list(dict.fromkeys(x.strip()[:120] for x in self.game_queries if x.strip()))
        if not cleaned: raise ValueError("At least one non-empty game query is required.")
        self.game_queries=cleaned[:20]
        return self
class TwitchScheduleIn(BaseModel):
    enabled: bool
    game_queries: list[str] = Field(default_factory=list,max_length=20)
    interval_minutes: int = Field(default=60,ge=15,le=1440)
    @model_validator(mode="after")
    def check_schedule(self):
        self.game_queries=list(dict.fromkeys(x.strip()[:120] for x in self.game_queries if x.strip()))[:20]
        if self.enabled and not self.game_queries: raise ValueError("Scheduled discovery needs at least one game query.")
        return self
class PublishingEnableIn(BaseModel):
    enabled: bool
class PublishRequest(BaseModel):
    rendered_output_id: str
    title: str = Field(min_length=1,max_length=100)
    description: str = Field(default="",max_length=5000)
    tags: list[str] = Field(default_factory=list,max_length=30)
    publication_mode: str = "private"
    publish_at: str | None = None
    @model_validator(mode="after")
    def check_publication(self):
        if self.publication_mode not in {"private","scheduled_public"}: raise ValueError("publication_mode must be private or scheduled_public.")
        if self.publication_mode=="scheduled_public":
            if not self.publish_at: raise ValueError("Scheduled publication requires publish_at.")
            try: dt=datetime.fromisoformat(self.publish_at.replace("Z","+00:00"))
            except ValueError as exc: raise ValueError("publish_at must be ISO 8601 with an explicit timezone.") from exc
            if dt.tzinfo is None or dt<=datetime.now(timezone.utc): raise ValueError("publish_at must be timezone-aware and in the future.")
        elif self.publish_at:
            raise ValueError("publish_at is only valid for scheduled_public mode.")
        self.tags=[str(x).strip()[:100] for x in self.tags if str(x).strip()][:30]
        return self
class AnalyticsSyncIn(BaseModel):
    start_date: str | None = None
    end_date: str | None = None
    @model_validator(mode="after")
    def check_dates(self):
        end=self.end_date or date.today().isoformat()
        start=self.start_date or (date.today()-timedelta(days=7)).isoformat()
        try:
            start_d=date.fromisoformat(start); end_d=date.fromisoformat(end)
        except ValueError as exc: raise ValueError("Analytics dates must use YYYY-MM-DD.") from exc
        if end_d<start_d: raise ValueError("end_date cannot precede start_date.")
        if end_d>date.today(): raise ValueError("end_date cannot be in the future.")
        self.start_date=start_d.isoformat(); self.end_date=end_d.isoformat()
        return self

class PauseIn(BaseModel): paused: bool
class ModeIn(BaseModel):
    mode: str
    @model_validator(mode="after")
    def check_mode(self):
        if self.mode not in {"development","supervised"}: raise ValueError("Only development/supervised are available; autonomous public publishing is disabled")
        return self

def expiry_passed(value):
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        # Corrupt or ambiguous expiry metadata fails closed.
        return True

def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""): h.update(block)
    return h.hexdigest()
def safe_media(raw, root):
    root=root.resolve()
    p=Path(raw).expanduser()
    p=(root/p).resolve() if not p.is_absolute() else p.resolve()
    try: p.relative_to(root)
    except ValueError: raise HTTPException(400,detail="Media path must remain inside configured media root")
    if p.suffix.lower() not in {".mp4",".mkv",".webm",".mov",".avi",".m4v"}: raise HTTPException(400,detail="Unsupported video file extension")
    if not p.is_file(): raise HTTPException(404,detail=f"Media file not found: {p}")
    return p

def create_app(settings=None,start_worker=True):
    cfg=settings or Settings.load()
    workers={}
    @asynccontextmanager
    async def lifespan(app):
        initialize(cfg.database_path); recover_interrupted_jobs(cfg.database_path); cfg.media_root.mkdir(parents=True,exist_ok=True)
        if start_worker:
            w=Worker(cfg.database_path,cfg.ffmpeg_path,cfg.ffprobe_path,cfg.timeout_seconds); w.start(); workers["worker"]=w
        yield
        if "worker" in workers: workers["worker"].stop(); workers["worker"].join(timeout=5)
    app=FastAPI(title="Gaming Clip Factory AI",version="0.1.0",lifespan=lifespan)
    app.state.settings=cfg
    @app.get("/api/health")
    def health():
        with connect(cfg.database_path) as c: c.execute("SELECT 1")
        return {"status":"ok","database":"available","ffmpeg_available":bool(cfg.ffmpeg_path),"ffprobe_available":bool(cfg.ffprobe_path),"publishing":"credential-gated","youtube_credentials_required":True}
    @app.get("/api/config")
    def config():
        return {"project_root":str(cfg.project_root),"media_root":str(cfg.media_root),"ffmpeg_available":bool(cfg.ffmpeg_path),"ffprobe_available":bool(cfg.ffprobe_path),"publishing_configured":bool(os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()=="true"),"mode":"supervised"}
    @app.get("/api/dashboard")
    def dashboard():
        with connect(cfg.database_path) as c:
            def n(sql): return c.execute(sql).fetchone()[0]
            counts={"sources_total":n("SELECT COUNT(*) FROM sources"),"sources_authorized":n("SELECT COUNT(*) FROM source_permissions WHERE status='AUTHORIZED' AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))"),"sources_pending":n("SELECT COUNT(*) FROM source_permissions WHERE status IN ('UNKNOWN','DISCOVERED','EVIDENCE_REQUIRED','PENDING_REVIEW')"),"media_total":n("SELECT COUNT(*) FROM media_assets"),"candidates_needing_review":n("SELECT COUNT(*) FROM candidate_moments WHERE state='NEEDS_REVIEW'"),"jobs_queued":n("SELECT COUNT(*) FROM jobs WHERE status='QUEUED'"),"jobs_running":n("SELECT COUNT(*) FROM jobs WHERE status='RUNNING'"),"jobs_failed":n("SELECT COUNT(*) FROM jobs WHERE status='FAILED'"),"published_videos":n("SELECT COUNT(*) FROM published_videos WHERE youtube_video_id IS NOT NULL")}
            paused=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            mode=c.execute("SELECT value FROM settings WHERE key='operating_mode'").fetchone()
            jobs=[dict(r) for r in c.execute("SELECT id,job_type,status,attempt_count,error,created_at,updated_at FROM jobs ORDER BY created_at DESC LIMIT 8")]
            events=[dict(r) for r in c.execute("SELECT id,level,event_type,message,created_at FROM system_events ORDER BY created_at DESC LIMIT 8")]
        return {"counts":counts,"global_paused":bool(paused and paused["value"]=="true"),"operating_mode":mode["value"] if mode else "supervised","recent_jobs":jobs,"recent_events":events}
    @app.get("/api/sources")
    def sources(status: str|None=None):
        with connect(cfg.database_path) as c:
            sql="SELECT s.*,p.status AS rights_status,p.license_type,p.permission_evidence,p.editing_allowed,p.commercial_use_allowed,p.reviewed_by_owner,p.attribution,p.expires_at,p.notes AS rights_notes FROM sources s JOIN source_permissions p ON p.source_id=s.id"
            rows = [dict(r) for r in c.execute(sql+(" WHERE p.status=?" if status else "")+" ORDER BY s.created_at DESC",((status,) if status else ()))]
            for row in rows:
                if row["rights_status"] == "AUTHORIZED" and expiry_passed(row.get("expires_at")):
                    row["rights_status"] = "EXPIRED"
            return rows
    @app.post("/api/sources",status_code=201)
    def add_source(data: SourceIn):
        sid=str(uuid4()); t=now()
        try:
            with session(cfg.database_path) as c:
                c.execute("INSERT INTO sources(id,source_url,platform,title,creator,game,discovered_at,created_at) VALUES(?,?,?,?,?,?,?,?)",(sid,str(data.source_url),data.platform.strip(),data.title,data.creator,data.game,t,t))
                c.execute("INSERT INTO source_permissions(source_id,status,updated_at) VALUES(?,'UNKNOWN',?)",(sid,t))
                event(c,"INFO","source.discovered_manually","Source registered; reuse rights remain UNKNOWN.",{"source_id":sid})
        except sqlite3.IntegrityError: raise HTTPException(409,detail="This source URL is already registered.")
        return {"id":sid,"source_url":str(data.source_url),"platform":data.platform,"title":data.title,"creator":data.creator,"game":data.game,"rights_status":"UNKNOWN","created_at":t}
    @app.put("/api/sources/{sid}/rights")
    def rights(sid: str,data: RightsIn):
        t=now()
        with session(cfg.database_path) as c:
            if not c.execute("SELECT id FROM sources WHERE id=?",(sid,)).fetchone(): raise HTTPException(404,detail="Source not found")
            c.execute("UPDATE source_permissions SET status=?,license_type=?,permission_evidence=?,editing_allowed=?,commercial_use_allowed=?,reviewed_by_owner=?,attribution=?,expires_at=?,notes=?,updated_at=? WHERE source_id=?",(data.status,data.license_type,data.permission_evidence,int(data.editing_allowed),int(data.commercial_use_allowed),int(data.reviewed_by_owner),data.attribution,data.expires_at,data.notes,t,sid))
            event(c,"INFO" if data.status=="AUTHORIZED" else "WARNING","rights.reviewed","Rights status changed to "+data.status,{"source_id":sid,"status":data.status})
        return {"source_id":sid,"status":data.status,"updated_at":t}
    @app.get("/api/media")
    def media():
        with connect(cfg.database_path) as c:
            result=[]
            for r in c.execute("SELECT m.*,s.title AS source_title,p.status AS rights_status,p.expires_at FROM media_assets m JOIN sources s ON s.id=m.source_id JOIN source_permissions p ON p.source_id=s.id ORDER BY m.created_at DESC"):
                item=dict(r); item["probe"]=json.loads(item.pop("probe_json")) if item["probe_json"] else None
                if item["rights_status"] == "AUTHORIZED" and expiry_passed(item.get("expires_at")): item["rights_status"] = "EXPIRED"
                result.append(item)
            return result
    @app.post("/api/media/import",status_code=201)
    def import_media(data: MediaIn):
        p=safe_media(data.path,cfg.media_root)
        with connect(cfg.database_path) as c:
            src=c.execute("SELECT s.id,p.status,p.expires_at FROM sources s JOIN source_permissions p ON p.source_id=s.id WHERE s.id=?",(data.source_id,)).fetchone()
            if not src: raise HTTPException(404,detail="Source not found")
            if src["status"]!="AUTHORIZED" or expiry_passed(src["expires_at"]): raise HTTPException(409,detail="Rights gate blocked import: source must have current AUTHORIZED permission.")
            sha=digest(p)
            if c.execute("SELECT id FROM media_assets WHERE source_id=? AND sha256=?",(data.source_id,sha)).fetchone(): raise HTTPException(409,detail="This exact file is already imported for this source.")
            mid=str(uuid4()); t=now()
            c.execute("INSERT INTO media_assets(id,source_id,original_path,file_name,file_size,sha256,state,created_at) VALUES(?,?,?,?,?,?,'IMPORTED',?)",(mid,data.source_id,str(p),p.name,p.stat().st_size,sha,t))
            event(c,"INFO","media.imported","Authorized media registered.",{"media_id":mid,"source_id":data.source_id})
        return {"id":mid,"source_id":data.source_id,"file_name":p.name,"file_size":p.stat().st_size,"sha256":sha,"state":"IMPORTED","created_at":t}
    @app.post("/api/media/{mid}/analyze",status_code=202)
    def analyze(mid: str):
        with session(cfg.database_path) as c:
            pause=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if pause and pause["value"]=="true": raise HTTPException(423,detail="Global pause is enabled.")
            r=c.execute("SELECT m.source_id,p.status,p.expires_at FROM media_assets m JOIN source_permissions p ON p.source_id=m.source_id WHERE m.id=?",(mid,)).fetchone()
            if not r: raise HTTPException(404,detail="Media asset not found")
            if r["status"]!="AUTHORIZED" or expiry_passed(r["expires_at"]): raise HTTPException(409,detail="Rights gate blocked analysis: current AUTHORIZED permission is required.")
            old=c.execute("SELECT id FROM jobs WHERE media_id=? AND job_type='ANALYZE_MEDIA' AND status IN ('QUEUED','RUNNING')",(mid,)).fetchone()
            if old: return {"id":old["id"],"status":"ALREADY_QUEUED"}
            jid=str(uuid4()); t=now()
            c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,created_at,updated_at) VALUES(?,'ANALYZE_MEDIA','QUEUED',?,?,?,?)",(jid,r["source_id"],mid,t,t))
            event(c,"INFO","job.queued","Media analysis queued.",{"job_id":jid,"media_id":mid})
        return {"id":jid,"status":"QUEUED","job_type":"ANALYZE_MEDIA"}
    @app.get("/api/candidates")
    def candidates(state: str | None = None):
        with connect(cfg.database_path) as c:
            sql = "SELECT c.*,m.file_name,s.title AS source_title,p.status AS rights_status,p.expires_at FROM candidate_moments c JOIN media_assets m ON m.id=c.media_id JOIN sources s ON s.id=m.source_id JOIN source_permissions p ON p.source_id=s.id"
            rows=[dict(r) for r in c.execute(sql+(" WHERE c.state=?" if state else "")+" ORDER BY c.created_at DESC",((state,) if state else ()))]
            for row in rows:
                if row["rights_status"] == "AUTHORIZED" and expiry_passed(row.get("expires_at")):
                    row["rights_status"] = "EXPIRED"
            return rows
    @app.post("/api/candidates/{cid}/decision", status_code=202)
    def candidate_decision(cid: str, data: CandidateDecisionIn):
        if data.decision == "approve" and (not cfg.ffmpeg_path or not cfg.ffprobe_path):
            raise HTTPException(503, detail="Rendering is unavailable: configure FFmpeg and FFprobe first.")
        t=now()
        with session(cfg.database_path) as c:
            pause=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if pause and pause["value"]=="true":
                raise HTTPException(423, detail="Global pause is enabled.")
            row=c.execute("""SELECT cm.id,cm.media_id,cm.state,m.source_id,p.status AS rights_status,p.expires_at
                FROM candidate_moments cm JOIN media_assets m ON m.id=cm.media_id
                JOIN source_permissions p ON p.source_id=m.source_id WHERE cm.id=?""",(cid,)).fetchone()
            if not row:
                raise HTTPException(404,detail="Candidate not found.")
            if row["rights_status"]!="AUTHORIZED" or expiry_passed(row["expires_at"]):
                raise HTTPException(409,detail="Rights gate blocked this decision: current AUTHORIZED permission is required.")
            if row["state"]!="NEEDS_REVIEW":
                raise HTTPException(409,detail=f"Candidate cannot be reviewed from state {row['state']}.")
            if data.decision=="reject":
                c.execute("UPDATE candidate_moments SET state='REJECTED' WHERE id=?",(cid,))
                event(c,"INFO","candidate.rejected","Candidate rejected by owner.",{"candidate_id":cid,"notes":data.notes})
                return {"candidate_id":cid,"state":"REJECTED"}
            jid=str(uuid4())
            c.execute("UPDATE candidate_moments SET state='APPROVED' WHERE id=?",(cid,))
            c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,'RENDER_CANDIDATE','QUEUED',?,?, ?,?,?)",
                (jid,row["source_id"],row["media_id"],json.dumps({"candidate_id":cid,"owner_notes":data.notes}),t,t))
            event(c,"INFO","candidate.approved","Candidate approved; render/QC job queued.",{"candidate_id":cid,"job_id":jid})
        return {"candidate_id":cid,"state":"APPROVED","job_id":jid,"job_status":"QUEUED"}
    @app.get("/api/renders")
    def renders():
        with connect(cfg.database_path) as c:
            rows=[]
            for r in c.execute("SELECT ro.*,m.file_name,s.title AS source_title FROM rendered_outputs ro JOIN media_assets m ON m.id=ro.media_id JOIN sources s ON s.id=m.source_id ORDER BY ro.created_at DESC"):
                item=dict(r); item["qc_report"]=json.loads(item.pop("qc_report_json")); rows.append(item)
            return rows
    @app.get("/api/jobs")
    def jobs():
        with connect(cfg.database_path) as c: return [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100")]
    @app.post("/api/jobs/{jid}/retry",status_code=202)
    def retry(jid: str):
        t=now()
        with session(cfg.database_path) as c:
            pause=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if pause and pause["value"]=="true": raise HTTPException(423,detail="Global pause is enabled.")
            r=c.execute("SELECT * FROM jobs WHERE id=?",(jid,)).fetchone()
            if not r: raise HTTPException(404,detail="Job not found")
            if r["status"]!="FAILED" or r["attempt_count"]>=r["max_attempts"]: raise HTTPException(409,detail="Job is not retryable or maximum attempts reached.")
            c.execute("UPDATE jobs SET status='QUEUED',error='',updated_at=?,finished_at=NULL WHERE id=?",(t,jid))
            event(c,"WARNING","job.retry_requested","Failed job returned to queue by owner.",{"job_id":jid})
        return {"id":jid,"status":"QUEUED"}
    @app.post("/api/system/pause")
    def pause(data: PauseIn):
        t=now(); v="true" if data.paused else "false"
        with session(cfg.database_path) as c:
            c.execute("INSERT INTO settings(key,value,updated_at) VALUES('global_paused',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(v,t))
            event(c,"WARNING" if data.paused else "INFO","system.pause_changed","Global pause changed.",{"paused":data.paused})
        return {"global_paused":data.paused,"updated_at":t}
    @app.get("/api/settings")
    def get_settings():
        with connect(cfg.database_path) as c: v={r["key"]:r["value"] for r in c.execute("SELECT key,value FROM settings")}
        return {"global_paused":v.get("global_paused","false")=="true","operating_mode":v.get("operating_mode","supervised"),"autonomous_public_publishing":False}
    @app.put("/api/settings/mode")
    def set_mode(data: ModeIn):
        t=now()
        with session(cfg.database_path) as c:
            c.execute("INSERT INTO settings(key,value,updated_at) VALUES('operating_mode',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(data.mode,t))
            event(c,"INFO","system.mode_changed","Operating mode set to "+data.mode,{"mode":data.mode})
        return {"operating_mode":data.mode,"autonomous_public_publishing":False}

    @app.get("/api/integrations/status")
    def integration_status():
        try:
            import keyring
            youtube_credentials_present=bool(keyring.get_password("gaming-clip-factory-ai","youtube-oauth-credentials"))
        except Exception:
            youtube_credentials_present=False
        with connect(cfg.database_path) as c:
            values={r["key"]:r["value"] for r in c.execute("SELECT key,value FROM settings WHERE key IN ('youtube_uploads_enabled','twitch_discovery_enabled','twitch_discovery_queries','twitch_discovery_interval_minutes','twitch_discovery_next_at')")}
        return {
            "twitch":{"configured":bool(os.getenv("GCF_TWITCH_CLIENT_ID") and os.getenv("GCF_TWITCH_CLIENT_SECRET")),"scheduled_enabled":values.get("twitch_discovery_enabled")=="true","game_queries":json.loads(values.get("twitch_discovery_queries","[]")),"interval_minutes":int(values.get("twitch_discovery_interval_minutes","60")),"next_run":values.get("twitch_discovery_next_at")},
            "youtube":{"credentials_present":youtube_credentials_present,"upload_gate_configured":os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()=="true","uploads_enabled":values.get("youtube_uploads_enabled")=="true","scheduled_public_gate_configured":os.getenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","").lower()=="true","public_publishing_enabled":False},
        }

    @app.get("/api/discovery/runs")
    def discovery_runs():
        with connect(cfg.database_path) as c:
            return [dict(r) for r in c.execute("SELECT * FROM discovery_runs ORDER BY started_at DESC LIMIT 100")]

    @app.get("/api/discovery/twitch/status")
    def twitch_discovery_status():
        with connect(cfg.database_path) as c:
            values={r["key"]:r["value"] for r in c.execute("SELECT key,value FROM settings WHERE key LIKE 'twitch_discovery_%'")}
        try: queries=json.loads(values.get("twitch_discovery_queries","[]"))
        except ValueError: queries=[]
        return {"configured":bool(os.getenv("GCF_TWITCH_CLIENT_ID") and os.getenv("GCF_TWITCH_CLIENT_SECRET")),
                "enabled":values.get("twitch_discovery_enabled")=="true","game_queries":queries,
                "interval_minutes":int(values.get("twitch_discovery_interval_minutes","60")),
                "next_run":values.get("twitch_discovery_next_at")}

    @app.put("/api/discovery/twitch/schedule")
    def set_twitch_schedule(data: TwitchScheduleIn):
        if data.enabled and not (os.getenv("GCF_TWITCH_CLIENT_ID") and os.getenv("GCF_TWITCH_CLIENT_SECRET")):
            raise HTTPException(503,detail="Twitch discovery is not configured. Set GCF_TWITCH_CLIENT_ID and GCF_TWITCH_CLIENT_SECRET in local .env first.")
        t=now()
        with session(cfg.database_path) as c:
            enabled="true" if data.enabled else "false"
            values={"twitch_discovery_enabled":enabled,"twitch_discovery_queries":json.dumps(data.game_queries),
                    "twitch_discovery_interval_minutes":str(data.interval_minutes),
                    "twitch_discovery_next_at":t if data.enabled else ""}
            for key,value in values.items():
                c.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,value,t))
            event(c,"INFO","discovery.schedule_changed","Twitch discovery schedule updated.",{"enabled":data.enabled,"queries":data.game_queries,"interval_minutes":data.interval_minutes})
        return {"enabled":data.enabled,"game_queries":data.game_queries,"interval_minutes":data.interval_minutes,"next_run":t if data.enabled else None}

    @app.post("/api/discovery/twitch",status_code=202)
    def queue_twitch_discovery(data: TwitchDiscoveryIn):
        if not (os.getenv("GCF_TWITCH_CLIENT_ID") and os.getenv("GCF_TWITCH_CLIENT_SECRET")):
            raise HTTPException(503,detail="Twitch discovery is not configured. Add Twitch app credentials to local .env.")
        t=now(); jid=str(uuid4())
        with session(cfg.database_path) as c:
            paused=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if paused and paused["value"]=="true": raise HTTPException(423,detail="Global pause is enabled.")
            active=c.execute("SELECT id FROM jobs WHERE job_type='DISCOVER_TWITCH_CLIPS' AND status IN ('QUEUED','RUNNING') LIMIT 1").fetchone()
            if active: return {"id":active["id"],"status":"ALREADY_QUEUED"}
            c.execute("INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at) VALUES(?,'DISCOVER_TWITCH_CLIPS','QUEUED',?,?,?)",(jid,json.dumps(data.model_dump()),t,t))
            event(c,"INFO","discovery.queued","Twitch metadata discovery queued.",{"job_id":jid,"queries":data.game_queries})
        return {"id":jid,"status":"QUEUED","provider":"twitch_helix","game_queries":data.game_queries,"rights_status":"UNKNOWN"}

    @app.get("/api/youtube/status")
    def youtube_status():
        return connected_status()

    @app.get("/api/publishing/status")
    def publishing_status():
        with connect(cfg.database_path) as c:
            row=c.execute("SELECT value FROM settings WHERE key='youtube_uploads_enabled'").fetchone()
        try:
            import keyring
            present=bool(keyring.get_password("gaming-clip-factory-ai","youtube-oauth-credentials"))
        except Exception:
            present=False
        return {"credentials_present":present,"upload_environment_gate":os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()=="true",
                "uploads_enabled":bool(row and row["value"]=="true"),"scheduled_public_environment_gate":os.getenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","").lower()=="true",
                "default_privacy":"private","public_publishing_enabled":False}

    @app.post("/api/publishing/enable")
    def enable_publishing(data: PublishingEnableIn):
        t=now()
        if data.enabled:
            if os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()!="true":
                raise HTTPException(403,detail="Uploads are disabled by environment policy. Set GCF_ENABLE_YOUTUBE_UPLOADS=true locally after setup and supervised testing.")
            status=connected_status()
            if not status.get("connected"):
                raise HTTPException(503,detail="YouTube channel is not connected: "+str(status.get("reason") or "connection check failed"))
        with session(cfg.database_path) as c:
            value="true" if data.enabled else "false"
            c.execute("INSERT INTO settings(key,value,updated_at) VALUES('youtube_uploads_enabled',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(value,t))
            event(c,"WARNING" if data.enabled else "INFO","youtube.upload_gate_changed","YouTube upload gate changed.",{"enabled":data.enabled})
        return {"uploads_enabled":data.enabled,"scheduled_public_environment_gate":os.getenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","").lower()=="true"}

    @app.post("/api/publishing/schedule",status_code=202)
    def schedule_publication(data: PublishRequest):
        t=now()
        with session(cfg.database_path) as c:
            paused=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if paused and paused["value"]=="true": raise HTTPException(423,detail="Global pause is enabled.")
            enabled=c.execute("SELECT value FROM settings WHERE key='youtube_uploads_enabled'").fetchone()
            if not enabled or enabled["value"]!="true" or os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()!="true":
                raise HTTPException(403,detail="Publishing is disabled. Connect YouTube and explicitly enable uploads first.")
            if data.publication_mode=="scheduled_public" and os.getenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","").lower()!="true":
                raise HTTPException(403,detail="Scheduled public publishing is disabled by environment policy.")
            output=c.execute("""SELECT ro.*,m.source_id,p.status AS rights_status,p.expires_at
                FROM rendered_outputs ro JOIN media_assets m ON m.id=ro.media_id
                JOIN source_permissions p ON p.source_id=m.source_id WHERE ro.id=?""",(data.rendered_output_id,)).fetchone()
            if not output: raise HTTPException(404,detail="Rendered output not found.")
            if output["qc_status"]!="PASS": raise HTTPException(409,detail="Only QC-passed renders can be published.")
            if output["rights_status"]!="AUTHORIZED" or expiry_passed(output["expires_at"]):
                raise HTTPException(409,detail="Publishing blocked: source does not have current AUTHORIZED permission.")
            output_path=Path(output["output_path"])
            if not output_path.is_file() or output_path.stat().st_size!=output["file_size"]:
                raise HTTPException(409,detail="Rendered output is missing or differs from its QC record.")
            if digest(output_path)!=output["sha256"]:
                raise HTTPException(409,detail="Rendered output hash no longer matches its QC record.")
            duplicate=c.execute("SELECT id,publishing_status FROM published_videos WHERE rendered_output_id=? OR output_path=?",(data.rendered_output_id,output["output_path"])).fetchone()
            if duplicate:
                raise HTTPException(409,detail=f"This output already has a publication record ({duplicate['publishing_status']}); inspect it before retrying.")
            publish_at=data.publish_at if data.publication_mode=="scheduled_public" else None
            pub_id=str(uuid4()); job_id=str(uuid4())
            status="UPLOAD_PENDING"
            c.execute("""INSERT INTO published_videos(id,source_id,media_id,candidate_id,output_path,title,description,template_id,qc_status,publishing_status,youtube_video_id,created_at,updated_at,rendered_output_id,scheduled_publish_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)""",(pub_id,output["source_id"],output["media_id"],output["candidate_id"],output["output_path"],data.title,data.description,"portrait_fit_pad","PASS",status,t,t,data.rendered_output_id,publish_at))
            payload={"published_video_id":pub_id,"publish_at":publish_at,"tags":data.tags,"publication_mode":data.publication_mode}
            c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,'UPLOAD_YOUTUBE','QUEUED',?,?, ?,?,?)",(job_id,output["source_id"],output["media_id"],json.dumps(payload),t,t))
            event(c,"INFO","youtube.upload_queued","QC-passed output queued for private or scheduled YouTube upload.",{"job_id":job_id,"published_video_id":pub_id,"rendered_output_id":data.rendered_output_id,"publication_mode":data.publication_mode})
        return {"id":job_id,"status":"QUEUED","published_video_id":pub_id,"publication_mode":data.publication_mode,"privacy_status":"private"}

    @app.get("/api/publishing/videos")
    def publication_history():
        with connect(cfg.database_path) as c:
            return [dict(r) for r in c.execute("SELECT id,output_path,title,description,qc_status,publishing_status,youtube_video_id,created_at,updated_at,scheduled_publish_at FROM published_videos ORDER BY created_at DESC LIMIT 200")]

    @app.post("/api/youtube/analytics/sync",status_code=202)
    def sync_youtube_analytics(data: AnalyticsSyncIn):
        status=connected_status()
        if not status.get("connected"):
            raise HTTPException(503,detail="YouTube channel is not connected: "+str(status.get("reason") or "connection check failed"))
        t=now(); jid=str(uuid4())
        with session(cfg.database_path) as c:
            paused=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if paused and paused["value"]=="true": raise HTTPException(423,detail="Global pause is enabled.")
            active=c.execute("SELECT id FROM jobs WHERE job_type='SYNC_YOUTUBE_ANALYTICS' AND status IN ('QUEUED','RUNNING') LIMIT 1").fetchone()
            if active: return {"id":active["id"],"status":"ALREADY_QUEUED"}
            c.execute("INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at) VALUES(?,'SYNC_YOUTUBE_ANALYTICS','QUEUED',?,?,?)",(jid,json.dumps({"start_date":data.start_date,"end_date":data.end_date}),t,t))
            event(c,"INFO","youtube.analytics_queued","YouTube Analytics sync queued.",{"job_id":jid,"start_date":data.start_date,"end_date":data.end_date})
        return {"id":jid,"status":"QUEUED","start_date":data.start_date,"end_date":data.end_date}

    @app.get("/api/youtube/analytics")
    def youtube_analytics():
        with connect(cfg.database_path) as c:
            return [dict(r) for r in c.execute("""SELECT a.*,p.title,p.youtube_video_id FROM analytics_snapshots a
                JOIN published_videos p ON p.id=a.published_video_id ORDER BY a.captured_at DESC LIMIT 500""")]

    @app.get("/api/events")
    def events():
        with connect(cfg.database_path) as c: return [dict(r) for r in c.execute("SELECT * FROM system_events ORDER BY created_at DESC LIMIT 100")]
    return app

app=create_app()
