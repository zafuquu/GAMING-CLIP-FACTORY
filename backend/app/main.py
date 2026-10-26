import json, sqlite3, hashlib, subprocess, re, os, importlib.util
from pathlib import Path, PurePosixPath
from uuid import uuid4
from datetime import datetime, timezone, date, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, AnyHttpUrl, Field, model_validator
from .config import Settings
from .db import initialize, connect, session, now, event, recover_interrupted_jobs
from .worker import Worker
from .integrations.youtube import connected_status, youtube_service, youtube_discovery_available, youtube_oauth_available
from .vision import vision_status

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
    render_profile: str = "fit_pad"
    @model_validator(mode="after")
    def check_decision(self):
        if self.decision not in {"approve","reject"}:
            raise ValueError("decision must be approve or reject")
        if self.render_profile not in {"fit_pad","blur_fill"}:
            raise ValueError("render_profile must be fit_pad or blur_fill.")
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
class FolderWatchIn(BaseModel):
    source_id: str
    relative_path: str = Field(min_length=1,max_length=500)
    enabled: bool = True
    recursive: bool = False
    auto_analyze: bool = True
    scan_interval_seconds: int = Field(default=60,ge=15,le=3600)
    @model_validator(mode="after")
    def normalize_relative_path(self):
        raw=self.relative_path.strip().replace(chr(92), "/")
        if "\\x00" in raw or ":" in raw:
            raise ValueError("relative_path cannot contain NUL characters or a drive prefix.")
        path=PurePosixPath(raw)
        if path.is_absolute() or not path.parts or any(part in {"",".",".."} for part in path.parts):
            raise ValueError("relative_path must be a safe, non-empty folder path under the configured media root.")
        if any(part.startswith(".") for part in path.parts):
            raise ValueError("Hidden/system folders cannot be watched.")
        self.relative_path=path.as_posix()
        return self
class FolderWatchUpdateIn(BaseModel):
    enabled: bool | None = None
    recursive: bool | None = None
    auto_analyze: bool | None = None
    scan_interval_seconds: int | None = Field(default=None,ge=15,le=3600)
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
class ReconcileVideoIn(BaseModel):
    youtube_video_id: str = Field(min_length=6,max_length=40,pattern=r"^[A-Za-z0-9_-]+$")

class RescheduleIn(BaseModel):
    publish_at: str
    @model_validator(mode="after")
    def check_publish_at(self):
        try: parsed=datetime.fromisoformat(self.publish_at.replace("Z","+00:00"))
        except ValueError as exc: raise ValueError("publish_at must be ISO 8601 with an explicit timezone.") from exc
        if parsed.tzinfo is None or parsed<=datetime.now(timezone.utc):
            raise ValueError("publish_at must be timezone-aware and in the future.")
        self.publish_at=parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        return self

class AnalyticsSyncIn(BaseModel):
    start_date: str | None = None
    end_date: str | None = None
    @model_validator(mode="after")
    def check_dates(self):
        # YouTube Analytics windows use UTC dates consistently with the worker scheduler.
        utc_yesterday=datetime.now(timezone.utc).date()-timedelta(days=1)
        end=self.end_date or utc_yesterday.isoformat()
        start=self.start_date or (utc_yesterday-timedelta(days=6)).isoformat()
        try:
            start_d=date.fromisoformat(start); end_d=date.fromisoformat(end)
        except ValueError as exc: raise ValueError("Analytics dates must use YYYY-MM-DD.") from exc
        if end_d<start_d: raise ValueError("end_date cannot precede start_date.")
        if end_d>utc_yesterday: raise ValueError("end_date must be yesterday or earlier in UTC; current-day metrics may be incomplete.")
        self.start_date=start_d.isoformat(); self.end_date=end_d.isoformat()
        return self

class AnalyticsScheduleIn(BaseModel):
    enabled: bool
    interval_minutes: int = Field(default=1440,ge=360,le=1440)
    window_days: int = Field(default=7,ge=1,le=90)

class CaptionSegmentIn(BaseModel):
    start_seconds: float = Field(ge=0,le=3600)
    end_seconds: float = Field(gt=0,le=3600)
    text: str = Field(min_length=1,max_length=500)
    @model_validator(mode="after")
    def validate_segment(self):
        if self.end_seconds<=self.start_seconds:
            raise ValueError("Caption end_seconds must be after start_seconds.")
        self.text=self.text.strip()
        if not self.text:
            raise ValueError("Caption text cannot be empty.")
        return self

class CaptionEditIn(BaseModel):
    segments: list[CaptionSegmentIn] = Field(max_length=300)
    @model_validator(mode="after")
    def validate_order(self):
        ordered=sorted(self.segments,key=lambda seg:seg.start_seconds)
        for previous,current in zip(ordered,ordered[1:]):
            if current.start_seconds<previous.end_seconds:
                raise ValueError("Caption timings cannot overlap. Adjust the segments and retry.")
        self.segments=ordered
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
            w=Worker(cfg.database_path,cfg.ffmpeg_path,cfg.ffprobe_path,cfg.timeout_seconds,media_root=cfg.media_root); w.start(); workers["worker"]=w
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

    @app.get("/api/automation/folder-watches")
    def list_folder_watches():
        with connect(cfg.database_path) as c:
            rows=[]
            sql="""SELECT w.*,s.title AS source_title,s.source_url,p.status AS rights_status,p.expires_at,
                (SELECT COUNT(*) FROM watched_media_files f WHERE f.watch_id=w.id) AS files_seen,
                (SELECT COUNT(*) FROM watched_media_files f WHERE f.watch_id=w.id AND f.status='PENDING_STABILITY') AS files_waiting,
                (SELECT COUNT(*) FROM watched_media_files f WHERE f.watch_id=w.id AND f.status='IMPORTED') AS files_imported,
                (SELECT COUNT(*) FROM watched_media_files f WHERE f.watch_id=w.id AND f.status='SKIPPED_RIGHTS') AS files_blocked_rights,
                (SELECT COUNT(*) FROM watched_media_files f WHERE f.watch_id=w.id AND f.status='DUPLICATE') AS files_duplicate
                FROM media_folder_watches w JOIN sources s ON s.id=w.source_id
                JOIN source_permissions p ON p.source_id=s.id ORDER BY w.created_at DESC"""
            for r in c.execute(sql):
                item=dict(r)
                item["enabled"]=bool(item["enabled"])
                item["recursive"]=bool(item["recursive"])
                item["auto_analyze"]=bool(item["auto_analyze"])
                if item["rights_status"]=="AUTHORIZED" and expiry_passed(item.get("expires_at")):
                    item["rights_status"]="EXPIRED"
                rows.append(item)
            return rows

    @app.post("/api/automation/folder-watches",status_code=201)
    def create_folder_watch(data: FolderWatchIn):
        t=now()
        root=cfg.media_root.resolve()
        path=(root/data.relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise HTTPException(400,detail="Watched folder must remain inside the configured media root.")
        with session(cfg.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
            pause=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if pause and pause["value"]=="true":
                raise HTTPException(423,detail="Global pause is enabled.")
            source=c.execute("""SELECT s.id,p.status,p.expires_at FROM sources s
                JOIN source_permissions p ON p.source_id=s.id WHERE s.id=?""",(data.source_id,)).fetchone()
            if not source:
                raise HTTPException(404,detail="Source not found.")
            if source["status"]!="AUTHORIZED" or expiry_passed(source["expires_at"]):
                raise HTTPException(409,detail="Folder watches require a currently AUTHORIZED, unexpired source. Public metadata alone is not permission.")
            existing=c.execute("SELECT id,relative_path,source_id FROM media_folder_watches").fetchall()
            new_parts=PurePosixPath(data.relative_path).parts
            for item in existing:
                old_parts=PurePosixPath(item["relative_path"]).parts
                if new_parts[:len(old_parts)]==old_parts or old_parts[:len(new_parts)]==new_parts:
                    if str(item["source_id"])!=data.source_id:
                        raise HTTPException(409,detail="This folder overlaps another active watch assigned to a different source.")
                    if item["relative_path"]==data.relative_path:
                        raise HTTPException(409,detail="This source/folder watch already exists.")
                    raise HTTPException(409,detail="Overlapping watched folders are not allowed; create one parent watch instead.")
            path.mkdir(parents=True,exist_ok=True)
            watch_id=str(uuid4())
            c.execute("""INSERT INTO media_folder_watches(id,source_id,relative_path,enabled,auto_analyze,recursive,scan_interval_seconds,next_scan_at,last_scan_at,last_error,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,NULL,'',?,?)""",
                (watch_id,data.source_id,data.relative_path,int(data.enabled),int(data.auto_analyze),int(data.recursive),data.scan_interval_seconds,t,t,t))
            event(c,"INFO","folder_watch.created","Local folder watch created for an authorized source.",{"watch_id":watch_id,"source_id":data.source_id,"relative_path":data.relative_path,"auto_analyze":data.auto_analyze})
        return {"id":watch_id,"source_id":data.source_id,"relative_path":data.relative_path,"enabled":data.enabled,
                "recursive":data.recursive,"auto_analyze":data.auto_analyze,"scan_interval_seconds":data.scan_interval_seconds,
                "next_scan_at":t,"folder_path":str(path)}

    @app.put("/api/automation/folder-watches/{watch_id}")
    def update_folder_watch(watch_id: str,data: FolderWatchUpdateIn):
        updates=data.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(400,detail="Provide at least one folder-watch setting to update.")
        t=now()
        with session(cfg.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("""SELECT w.*,p.status AS rights_status,p.expires_at FROM media_folder_watches w
                JOIN source_permissions p ON p.source_id=w.source_id WHERE w.id=?""",(watch_id,)).fetchone()
            if not row:
                raise HTTPException(404,detail="Folder watch not found.")
            if updates.get("enabled") is True and (row["rights_status"]!="AUTHORIZED" or expiry_passed(row["expires_at"])):
                raise HTTPException(409,detail="Cannot enable a folder watch without current AUTHORIZED permission.")
            values={k:v for k,v in updates.items() if v is not None}
            if not values:
                raise HTTPException(400,detail="Provide a non-null folder-watch setting to update.")
            if values.get("enabled") is True:
                target_parts=PurePosixPath(row["relative_path"]).parts
                siblings=c.execute("SELECT id,relative_path FROM media_folder_watches WHERE id<>?",(watch_id,)).fetchall()
                for sibling in siblings:
                    other_parts=PurePosixPath(sibling["relative_path"]).parts
                    if target_parts[:len(other_parts)]==other_parts or other_parts[:len(target_parts)]==target_parts:
                        raise HTTPException(409,detail="Cannot enable an overlapping folder watch. Remove or move the conflicting watch first.")
            columns={"enabled":"enabled","recursive":"recursive","auto_analyze":"auto_analyze","scan_interval_seconds":"scan_interval_seconds"}
            assignments=[columns[k]+"=?" for k in values]
            args=[int(v) if k in {"enabled","recursive","auto_analyze"} else int(v) for k,v in values.items()]
            assignments.extend(["next_scan_at=?","updated_at=?"])
            args.extend([t,t,watch_id])
            c.execute("UPDATE media_folder_watches SET "+", ".join(assignments)+" WHERE id=?",args)
            event(c,"INFO","folder_watch.updated","Local folder watch settings updated.",{"watch_id":watch_id,**values})
        return {"id":watch_id,**updates,"updated_at":t}

    @app.post("/api/automation/folder-watches/{watch_id}/scan",status_code=202)
    def trigger_folder_watch_scan(watch_id: str):
        t=now()
        with session(cfg.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT id,enabled,source_id FROM media_folder_watches WHERE id=?",(watch_id,)).fetchone()
            if not row:
                raise HTTPException(404,detail="Folder watch not found.")
            pause=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if pause and pause["value"]=="true":
                raise HTTPException(423,detail="Global pause is enabled.")
            if not row["enabled"]:
                raise HTTPException(409,detail="Enable this folder watch before requesting a scan.")
            c.execute("UPDATE media_folder_watches SET next_scan_at=?,updated_at=? WHERE id=?",(t,t,watch_id))
            event(c,"INFO","folder_watch.scan_requested","Owner requested an immediate local folder scan.",{"watch_id":watch_id})
        return {"id":watch_id,"status":"SCAN_QUEUED","next_scan_at":t}

    @app.delete("/api/automation/folder-watches/{watch_id}",status_code=200)
    def delete_folder_watch(watch_id: str):
        t=now()
        with session(cfg.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT id,relative_path,source_id FROM media_folder_watches WHERE id=?",(watch_id,)).fetchone()
            if not row:
                raise HTTPException(404,detail="Folder watch not found.")
            c.execute("DELETE FROM media_folder_watches WHERE id=?",(watch_id,))
            event(c,"WARNING","folder_watch.deleted","Folder watch removed. Already imported media is retained.",{"watch_id":watch_id,"relative_path":row["relative_path"],"source_id":row["source_id"],"deleted_at":t})
        return {"id":watch_id,"deleted":True,"media_retained":True}

    @app.get("/api/media")
    def media():
        with connect(cfg.database_path) as c:
            result=[]
            for r in c.execute("SELECT m.*,s.title AS source_title,p.status AS rights_status,p.expires_at,t.status AS transcript_status FROM media_assets m JOIN sources s ON s.id=m.source_id JOIN source_permissions p ON p.source_id=s.id LEFT JOIN transcripts t ON t.media_id=m.id ORDER BY m.created_at DESC"):
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
            c.execute("BEGIN IMMEDIATE")
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

    @app.post("/api/media/{mid}/transcribe",status_code=202)
    def transcribe_media(mid: str):
        if os.getenv("GCF_ENABLE_TRANSCRIPTION","").lower()!="true":
            raise HTTPException(403,detail="Local transcription is disabled. Configure GCF_ENABLE_TRANSCRIPTION=true in the local .env file.")
        if importlib.util.find_spec("faster_whisper") is None:
            raise HTTPException(503,detail="Optional dependency missing. Run scripts/setup-ml-windows.bat or install backend/requirements-ml.txt.")
        t=now(); jid=str(uuid4())
        with session(cfg.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
            paused=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if paused and paused["value"]=="true": raise HTTPException(423,detail="Global pause is enabled.")
            r=c.execute("SELECT m.source_id,p.status,p.expires_at FROM media_assets m JOIN source_permissions p ON p.source_id=m.source_id WHERE m.id=?",(mid,)).fetchone()
            if not r: raise HTTPException(404,detail="Media asset not found.")
            if r["status"]!="AUTHORIZED" or expiry_passed(r["expires_at"]): raise HTTPException(409,detail="Rights gate blocked transcription.")
            active=c.execute("SELECT id,status FROM jobs WHERE job_type='TRANSCRIBE_MEDIA' AND media_id=? AND status IN ('QUEUED','RUNNING')",(mid,)).fetchone()
            if active: return {"id":active["id"],"status":"ALREADY_QUEUED"}
            c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,created_at,updated_at) VALUES(?,'TRANSCRIBE_MEDIA','QUEUED',?,?,?,?)",(jid,r["source_id"],mid,t,t))
            event(c,"INFO","transcription.queued","Local transcription queued by owner.",{"job_id":jid,"media_id":mid})
        return {"id":jid,"status":"QUEUED","local_only":True}

    @app.get("/api/media/{mid}/transcript")
    def transcript(mid: str):
        with connect(cfg.database_path) as c:
            row=c.execute("SELECT * FROM transcripts WHERE media_id=?",(mid,)).fetchone()
            if not row: raise HTTPException(404,detail="Transcript not available for this media.")
            data=dict(row)
            data["segments"]=[dict(r) for r in c.execute("SELECT segment_index,start_seconds,end_seconds,text,confidence FROM transcript_segments WHERE transcript_id=? ORDER BY segment_index",(row["id"],))]
            return data

    @app.put("/api/candidates/{cid}/captions")
    def edit_candidate_captions(cid: str, data: CaptionEditIn):
        t=now()
        with session(cfg.database_path) as c:
            row=c.execute("""SELECT cm.start_seconds,cm.end_seconds,cm.state,p.status AS rights_status,p.expires_at
                FROM candidate_moments cm JOIN media_assets m ON m.id=cm.media_id
                JOIN source_permissions p ON p.source_id=m.source_id WHERE cm.id=?""",(cid,)).fetchone()
            if not row:
                raise HTTPException(404,detail="Candidate not found.")
            if row["rights_status"]!="AUTHORIZED" or expiry_passed(row["expires_at"]):
                raise HTTPException(409,detail="Caption editing blocked: current AUTHORIZED permission is required.")
            if row["state"]!="NEEDS_REVIEW":
                raise HTTPException(409,detail=f"Captions can only be edited while a candidate is in NEEDS_REVIEW (state={row['state']}).")
            duration=float(row["end_seconds"])-float(row["start_seconds"])
            for segment in data.segments:
                if segment.end_seconds>duration+0.001:
                    raise HTTPException(422,detail=f"Caption end time {segment.end_seconds:.3f}s exceeds candidate duration {duration:.3f}s.")
            c.execute("DELETE FROM caption_segments WHERE candidate_id=?",(cid,))
            base=float(row["start_seconds"])
            for index,segment in enumerate(data.segments):
                c.execute("""INSERT INTO caption_segments(id,candidate_id,segment_index,start_seconds,end_seconds,text,confidence,created_at)
                    VALUES(?,?,?,?,?,?,NULL,?)""",
                    (str(uuid4()),cid,index,round(base+segment.start_seconds,3),round(base+segment.end_seconds,3),segment.text,t))
            event(c,"INFO","captions.edited","Owner saved caption timing/text corrections.",{"candidate_id":cid,"segment_count":len(data.segments)})
        return {"candidate_id":cid,"caption_segment_count":len(data.segments),"state":"NEEDS_REVIEW","saved":True}

    @app.get("/api/candidates")
    def candidates(state: str | None = None):
        with connect(cfg.database_path) as c:
            sql = "SELECT c.*,m.file_name,s.title AS source_title,p.status AS rights_status,p.expires_at FROM candidate_moments c JOIN media_assets m ON m.id=c.media_id JOIN sources s ON s.id=m.source_id JOIN source_permissions p ON p.source_id=s.id"
            rows=[dict(r) for r in c.execute(sql+(" WHERE c.state=?" if state else "")+" ORDER BY COALESCE(c.ranking_score,0) DESC,c.created_at DESC",((state,) if state else ()))]
            for row in rows:
                if row["rights_status"] == "AUTHORIZED" and expiry_passed(row.get("expires_at")):
                    row["rights_status"] = "EXPIRED"
                plan=c.execute("SELECT * FROM content_plans WHERE candidate_id=?",(row["id"],)).fetchone()
                if plan:
                    plan_data=dict(plan)
                    plan_data["tags"]=json.loads(plan_data.pop("tags_json") or "[]")
                    plan_data["evidence"]=json.loads(plan_data.pop("evidence_json") or "{}")
                    row["content_plan"]=plan_data
                else:
                    row["content_plan"]=None
                visual=c.execute("SELECT * FROM candidate_vision_assessments WHERE candidate_id=?",(row["id"],)).fetchone()
                if visual:
                    visual_data=dict(visual)
                    visual_data["evidence"]=json.loads(visual_data.pop("evidence_json") or "[]")
                    visual_data["quality_issues"]=json.loads(visual_data.pop("quality_issues_json") or "[]")
                    visual_data["frame_times_seconds"]=json.loads(visual_data.pop("frame_times_json") or "[]")
                    visual_data["raw_response"]=json.loads(visual_data.pop("raw_response_json") or "{}")
                    row["vision_assessment"]=visual_data
                else:
                    row["vision_assessment"]=None
                caption_rows=c.execute("SELECT segment_index,start_seconds,end_seconds,text,confidence FROM caption_segments WHERE candidate_id=? ORDER BY segment_index",(row["id"],)).fetchall()
                row["caption_segments"]=[{"segment_index":cap["segment_index"],
                    "start_seconds":round(max(0.0,float(cap["start_seconds"])-float(row["start_seconds"])),3),
                    "end_seconds":round(max(0.0,float(cap["end_seconds"])-float(row["start_seconds"])),3),
                    "text":cap["text"],"confidence":cap["confidence"]} for cap in caption_rows]
                row["caption_segment_count"]=len(caption_rows)
            return rows

    @app.get("/api/vision/status")
    def get_vision_status():
        return vision_status()

    @app.post("/api/candidates/{cid}/analyze-vision",status_code=202)
    def queue_candidate_vision(cid: str):
        status=vision_status()
        if not status.get("ready"):
            raise HTTPException(403,detail="Local vision analysis is not ready. Enable GCF_ENABLE_VISION_ANALYSIS=true and configure a valid local Ollama-compatible endpoint/model.")
        if not cfg.ffmpeg_path:
            raise HTTPException(503,detail="FFmpeg is required to extract candidate frames.")
        t=now(); jid=str(uuid4())
        with session(cfg.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
            paused=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if paused and paused["value"]=="true": raise HTTPException(423,detail="Global pause is enabled.")
            row=c.execute("""SELECT cm.id,cm.media_id,cm.state,m.source_id,p.status AS rights_status,p.expires_at
                FROM candidate_moments cm JOIN media_assets m ON m.id=cm.media_id
                JOIN source_permissions p ON p.source_id=m.source_id WHERE cm.id=?""",(cid,)).fetchone()
            if not row: raise HTTPException(404,detail="Candidate not found.")
            if row["rights_status"]!="AUTHORIZED" or expiry_passed(row["expires_at"]):
                raise HTTPException(409,detail="Rights gate blocked vision analysis: current AUTHORIZED permission is required.")
            if row["state"]!="NEEDS_REVIEW":
                raise HTTPException(409,detail=f"Vision analysis only accepts candidates in NEEDS_REVIEW (state={row['state']}).")
            active=c.execute("SELECT id FROM jobs WHERE job_type='ANALYZE_CANDIDATE_VISION' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ? LIMIT 1",("%"+cid+"%",)).fetchone()
            if active: return {"id":active["id"],"status":"ALREADY_QUEUED"}
            c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,'ANALYZE_CANDIDATE_VISION','QUEUED',?,?, ?,?,?)",
                      (jid,row["source_id"],row["media_id"],json.dumps({"candidate_id":cid}),t,t))
            event(c,"INFO","vision.queued","Local vision assessment queued; candidate remains human-review-only.",{"job_id":jid,"candidate_id":cid,"model":status["model"]})
        return {"id":jid,"status":"QUEUED","candidate_id":cid,"model":status["model"],"automatic_approval":False}

    @app.post("/api/candidates/{cid}/decision", status_code=202)
    def candidate_decision(cid: str, data: CandidateDecisionIn):
        if data.decision == "approve" and (not cfg.ffmpeg_path or not cfg.ffprobe_path):
            raise HTTPException(503, detail="Rendering is unavailable: configure FFmpeg and FFprobe first.")
        t=now()
        with session(cfg.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
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
                (jid,row["source_id"],row["media_id"],json.dumps({"candidate_id":cid,"owner_notes":data.notes,"render_profile":data.render_profile}),t,t))
            event(c,"INFO","candidate.approved","Candidate approved; render/QC job queued.",{"candidate_id":cid,"job_id":jid,"render_profile":data.render_profile})
        return {"candidate_id":cid,"state":"APPROVED","job_id":jid,"job_status":"QUEUED","render_profile":data.render_profile}
    @app.get("/api/renders")
    def renders():
        with connect(cfg.database_path) as c:
            rows=[]
            for r in c.execute("SELECT ro.*,m.file_name,s.title AS source_title,cp.title AS suggested_title,cp.description AS suggested_description,cp.tags_json AS suggested_tags_json,cp.chosen_format AS content_format,cp.hook_text AS content_hook FROM rendered_outputs ro JOIN media_assets m ON m.id=ro.media_id JOIN sources s ON s.id=m.source_id LEFT JOIN content_plans cp ON cp.candidate_id=ro.candidate_id ORDER BY ro.created_at DESC"):
                item=dict(r); item["qc_report"]=json.loads(item.pop("qc_report_json")); item["suggested_tags"]=json.loads(item.pop("suggested_tags_json") or "[]"); rows.append(item)
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
            values={r["key"]:r["value"] for r in c.execute("SELECT key,value FROM settings WHERE key IN ('youtube_uploads_enabled','twitch_discovery_enabled','twitch_discovery_queries','twitch_discovery_interval_minutes','twitch_discovery_next_at','youtube_discovery_enabled','youtube_discovery_queries','youtube_discovery_interval_minutes','youtube_discovery_next_at','youtube_analytics_schedule_enabled','youtube_analytics_schedule_interval_minutes','youtube_analytics_schedule_window_days','youtube_analytics_schedule_next_at')")}
        try: twitch_queries=json.loads(values.get("twitch_discovery_queries","[]"))
        except (ValueError,TypeError): twitch_queries=[]
        try: youtube_queries=json.loads(values.get("youtube_discovery_queries","[]"))
        except (ValueError,TypeError): youtube_queries=[]
        return {
            "twitch":{"configured":bool(os.getenv("GCF_TWITCH_CLIENT_ID") and os.getenv("GCF_TWITCH_CLIENT_SECRET")),"scheduled_enabled":values.get("twitch_discovery_enabled")=="true","game_queries":twitch_queries,"interval_minutes":int(values.get("twitch_discovery_interval_minutes","60")),"next_run":values.get("twitch_discovery_next_at")},
            "transcription":{"enabled":os.getenv("GCF_ENABLE_TRANSCRIPTION","").lower()=="true","dependency_installed":importlib.util.find_spec("faster_whisper") is not None,"model_path_configured":bool(os.getenv("GCF_WHISPER_MODEL_PATH")),"model_download_allowed":os.getenv("GCF_WHISPER_ALLOW_MODEL_DOWNLOAD","").lower()=="true","device":os.getenv("GCF_WHISPER_DEVICE","cpu")},
            "vision":vision_status(),
            "youtube":{"credentials_present":youtube_credentials_present,"oauth_ready":youtube_oauth_available(),"analytics_schedule_enabled":values.get("youtube_analytics_schedule_enabled")=="true","analytics_schedule_interval_minutes":int(values.get("youtube_analytics_schedule_interval_minutes","1440")),"analytics_schedule_window_days":int(values.get("youtube_analytics_schedule_window_days","7")),"analytics_schedule_next_at":values.get("youtube_analytics_schedule_next_at"),"public_discovery_configured":youtube_discovery_available(),"scheduled_discovery_enabled":values.get("youtube_discovery_enabled")=="true","discovery_queries":youtube_queries,"discovery_interval_minutes":int(values.get("youtube_discovery_interval_minutes","60")),"discovery_next_run":values.get("youtube_discovery_next_at"),"api_key_discovery_configured":bool(os.getenv("GCF_YOUTUBE_API_KEY","").strip()),"upload_gate_configured":os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()=="true","uploads_enabled":values.get("youtube_uploads_enabled")=="true","scheduled_public_gate_configured":os.getenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","").lower()=="true","public_publishing_enabled":False},
        }


    @app.get("/api/discovery/youtube/status")
    def youtube_discovery_status():
        with connect(cfg.database_path) as c:
            values={r["key"]:r["value"] for r in c.execute("SELECT key,value FROM settings WHERE key LIKE 'youtube_discovery_%'")}
        try: queries=json.loads(values.get("youtube_discovery_queries","[]"))
        except (ValueError,TypeError): queries=[]
        return {
            "configured":youtube_discovery_available(),
            "api_key_configured":bool(os.getenv("GCF_YOUTUBE_API_KEY","").strip()),
            "enabled":values.get("youtube_discovery_enabled")=="true",
            "game_queries":queries,
            "interval_minutes":int(values.get("youtube_discovery_interval_minutes","60")),
            "next_run":values.get("youtube_discovery_next_at"),
        }

    @app.put("/api/discovery/youtube/schedule")
    def set_youtube_discovery_schedule(data: TwitchScheduleIn):
        if data.enabled and not youtube_discovery_available():
            raise HTTPException(503,detail="YouTube metadata discovery requires GCF_YOUTUBE_API_KEY or a stored YouTube OAuth connection.")
        t=now()
        with session(cfg.database_path) as c:
            values={
                "youtube_discovery_enabled":"true" if data.enabled else "false",
                "youtube_discovery_queries":json.dumps(data.game_queries),
                "youtube_discovery_interval_minutes":str(data.interval_minutes),
                "youtube_discovery_next_at":t if data.enabled else "",
            }
            for key,value in values.items():
                c.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,value,t))
            event(c,"INFO","discovery.schedule_changed","YouTube metadata discovery schedule updated.",{"enabled":data.enabled,"queries":data.game_queries,"interval_minutes":data.interval_minutes})
        return {"enabled":data.enabled,"game_queries":data.game_queries,"interval_minutes":data.interval_minutes,"next_run":t if data.enabled else None}

    @app.post("/api/discovery/youtube",status_code=202)
    def queue_youtube_discovery(data: TwitchDiscoveryIn):
        if not youtube_discovery_available():
            raise HTTPException(503,detail="YouTube metadata discovery requires GCF_YOUTUBE_API_KEY or a stored YouTube OAuth connection.")
        t=now(); jid=str(uuid4())
        with session(cfg.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
            paused=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if paused and paused["value"]=="true":
                raise HTTPException(423,detail="Global pause is enabled.")
            active=c.execute("SELECT id FROM jobs WHERE job_type='DISCOVER_YOUTUBE_VIDEOS' AND status IN ('QUEUED','RUNNING') LIMIT 1").fetchone()
            if active:
                return {"id":active["id"],"status":"ALREADY_QUEUED"}
            c.execute("INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at) VALUES(?,'DISCOVER_YOUTUBE_VIDEOS','QUEUED',?,?,?)",(jid,json.dumps(data.model_dump()),t,t))
            event(c,"INFO","discovery.queued","YouTube metadata search queued.",{"job_id":jid,"queries":data.game_queries})
        return {"id":jid,"status":"QUEUED","provider":"youtube_data_api","game_queries":data.game_queries,"rights_status":"UNKNOWN","media_downloaded":False}

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
            c.execute("BEGIN IMMEDIATE")
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
            c.execute("BEGIN IMMEDIATE")
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
                VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)""",(pub_id,output["source_id"],output["media_id"],output["candidate_id"],output["output_path"],data.title,data.description,"portrait_"+str(output["render_profile"] or "fit_pad"),"PASS",status,t,t,data.rendered_output_id,publish_at))
            payload={"published_video_id":pub_id,"publish_at":publish_at,"tags":data.tags,"publication_mode":data.publication_mode}
            c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,'UPLOAD_YOUTUBE','QUEUED',?,?, ?,?,?)",(job_id,output["source_id"],output["media_id"],json.dumps(payload),t,t))
            event(c,"INFO","youtube.upload_queued","QC-passed output queued for private or scheduled YouTube upload.",{"job_id":job_id,"published_video_id":pub_id,"rendered_output_id":data.rendered_output_id,"publication_mode":data.publication_mode})
        return {"id":job_id,"status":"QUEUED","published_video_id":pub_id,"publication_mode":data.publication_mode,"privacy_status":"private"}

    @app.get("/api/publishing/videos")
    def publication_history():
        with connect(cfg.database_path) as c:
            return [dict(r) for r in c.execute("SELECT id,output_path,title,description,qc_status,publishing_status,youtube_video_id,created_at,updated_at,scheduled_publish_at,remote_processing_status,processing_checked_at,processing_failure_reason FROM published_videos ORDER BY created_at DESC LIMIT 200")]


    @app.post("/api/publishing/videos/{published_id}/recheck-processing",status_code=202)
    def recheck_youtube_processing(published_id: str):
        channel=connected_status()
        if not channel.get("connected"):
            raise HTTPException(503,detail="YouTube channel is not connected: "+str(channel.get("reason") or "connection check failed"))
        t=now(); jid=str(uuid4())
        with session(cfg.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
            paused=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if paused and paused["value"]=="true":
                raise HTTPException(423,detail="Global pause is enabled.")
            row=c.execute("SELECT id,youtube_video_id,publishing_status,scheduled_publish_at FROM published_videos WHERE id=?",(published_id,)).fetchone()
            if not row: raise HTTPException(404,detail="Publication record not found.")
            if not row["youtube_video_id"]: raise HTTPException(409,detail="No YouTube video ID is recorded for this publication.")
            if row["publishing_status"] not in {"PROCESSING_TIMEOUT","PROCESSING_STATUS_ERROR","PROCESSING"}:
                raise HTTPException(409,detail=f"Processing recheck is unavailable in state {row['publishing_status']}.")
            active=c.execute("SELECT id FROM jobs WHERE job_type='CHECK_YOUTUBE_PROCESSING' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ? LIMIT 1",("%"+published_id+"%",)).fetchone()
            if active: return {"id":active["id"],"status":"ALREADY_QUEUED"}
            c.execute("INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at) VALUES(?,'CHECK_YOUTUBE_PROCESSING','QUEUED',?,?,?)",
                      (jid,json.dumps({"published_video_id":published_id,"poll_attempt":0,"max_polls":20}),t,t))
            c.execute("UPDATE published_videos SET publishing_status='PROCESSING',processing_failure_reason='',updated_at=? WHERE id=?",(t,published_id))
            event(c,"INFO","youtube.processing_recheck_queued","Owner requested another bounded YouTube processing poll.",{"job_id":jid,"published_video_id":published_id,"youtube_video_id":row["youtube_video_id"]})
        return {"id":jid,"status":"QUEUED","published_video_id":published_id,"public_release_requires_current_rights":True}

    @app.post("/api/publishing/videos/{published_id}/cancel-public-release")
    def cancel_public_release(published_id: str):
        t=now()
        with session(cfg.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT id,publishing_status,scheduled_publish_at,remote_processing_status,youtube_video_id FROM published_videos WHERE id=?",(published_id,)).fetchone()
            if not row: raise HTTPException(404,detail="Publication record not found.")
            if not row["scheduled_publish_at"] or row["publishing_status"] not in {"UPLOAD_PENDING","PROCESSING","SCHEDULED"}:
                raise HTTPException(409,detail="This publication has no cancellable scheduled-public release.")
            running=c.execute("""SELECT id,job_type FROM jobs WHERE job_type IN ('UPLOAD_YOUTUBE','CHECK_YOUTUBE_PROCESSING','PUBLISH_YOUTUBE')
                AND status='RUNNING' AND payload_json LIKE ? LIMIT 1""",("%"+published_id+"%",)).fetchone()
            if running:
                raise HTTPException(409,detail="A YouTube upload, processing poll, or publication operation is in progress; retry cancellation after it finishes.")
            c.execute("UPDATE jobs SET status='CANCELLED',error='Scheduled public release cancelled by owner; video remains private.',updated_at=?,finished_at=? WHERE job_type='PUBLISH_YOUTUBE' AND status='QUEUED' AND payload_json LIKE ?",(t,t,"%"+published_id+"%"))
            # If upload has not started, clear the publication intent from its persisted payload as well.
            upload_jobs=c.execute("SELECT id,payload_json FROM jobs WHERE job_type='UPLOAD_YOUTUBE' AND status='QUEUED' AND payload_json LIKE ?",("%"+published_id+"%",)).fetchall()
            for job in upload_jobs:
                try: upload_payload=json.loads(job["payload_json"] or "{}")
                except ValueError: upload_payload={}
                upload_payload["publish_at"]=None
                upload_payload["publication_mode"]="private"
                c.execute("UPDATE jobs SET payload_json=?,updated_at=? WHERE id=?",(json.dumps(upload_payload),t,job["id"]))
            final_state="READY_PRIVATE" if row["remote_processing_status"]=="succeeded" else ("UPLOAD_PENDING" if row["publishing_status"]=="UPLOAD_PENDING" else "PROCESSING")
            c.execute("UPDATE published_videos SET scheduled_publish_at=NULL,publishing_status=?,updated_at=? WHERE id=?",(final_state,t,published_id))
            event(c,"WARNING","youtube.scheduled_release_cancelled","Scheduled public release cancelled; the remote video remains private.",{"published_video_id":published_id,"state":final_state})
        return {"published_video_id":published_id,"scheduled_publish_at":None,"publishing_status":final_state,"public_release_cancelled":True}

    @app.post("/api/publishing/videos/{published_id}/reschedule")
    def reschedule_public_release(published_id: str, data: RescheduleIn):
        if os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()!="true" or os.getenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","").lower()!="true":
            raise HTTPException(403,detail="Scheduled public release is disabled by local environment policy.")
        t=now(); scheduled=datetime.fromisoformat(data.publish_at.replace("Z","+00:00"))
        with session(cfg.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
            paused=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if paused and paused["value"]=="true": raise HTTPException(423,detail="Global pause is enabled.")
            uploads=c.execute("SELECT value FROM settings WHERE key='youtube_uploads_enabled'").fetchone()
            if not uploads or uploads["value"]!="true": raise HTTPException(403,detail="YouTube uploads are disabled in database settings.")
            row=c.execute("""SELECT pv.*,p.status AS rights_status,p.expires_at,ro.qc_status AS render_qc,ro.sha256 AS render_sha,
                ro.file_size AS render_size FROM published_videos pv
                JOIN media_assets m ON m.id=pv.media_id JOIN source_permissions p ON p.source_id=m.source_id
                LEFT JOIN rendered_outputs ro ON ro.id=pv.rendered_output_id WHERE pv.id=?""",(published_id,)).fetchone()
            if not row: raise HTTPException(404,detail="Publication record not found.")
            if row["publishing_status"] not in {"UPLOAD_PENDING","PROCESSING","SCHEDULED"}:
                raise HTTPException(409,detail="This publication cannot be rescheduled in its current state.")
            running=c.execute("""SELECT id FROM jobs WHERE job_type IN ('UPLOAD_YOUTUBE','PUBLISH_YOUTUBE')
                AND status='RUNNING' AND payload_json LIKE ? LIMIT 1""",("%"+published_id+"%",)).fetchone()
            if running: raise HTTPException(409,detail="An external YouTube operation is running; wait for it to finish before rescheduling.")
            if row["rights_status"]!="AUTHORIZED" or expiry_passed(row["expires_at"]):
                raise HTTPException(409,detail="Cannot schedule public release without current AUTHORIZED permission.")
            if row["expires_at"]:
                expiry=datetime.fromisoformat(row["expires_at"].replace("Z","+00:00"))
                if expiry.tzinfo is None: expiry=expiry.replace(tzinfo=timezone.utc)
                if expiry<=scheduled: raise HTTPException(409,detail="Source permission expires before the proposed publication time.")
            if row["render_qc"]!="PASS" or row["qc_status"]!="PASS":
                raise HTTPException(409,detail="Only a QC-passed render can be scheduled for public release.")
            output=Path(row["output_path"])
            if not output.is_file() or output.stat().st_size!=row["render_size"] or digest(output)!=row["render_sha"]:
                raise HTTPException(409,detail="The output file no longer matches its QC record.")
            c.execute("UPDATE published_videos SET scheduled_publish_at=?,updated_at=? WHERE id=?",(scheduled.isoformat(timespec="seconds"),t,published_id))
            upload_jobs=c.execute("SELECT id,payload_json FROM jobs WHERE job_type='UPLOAD_YOUTUBE' AND status='QUEUED' AND payload_json LIKE ?",("%"+published_id+"%",)).fetchall()
            for job in upload_jobs:
                try: payload=json.loads(job["payload_json"] or "{}")
                except ValueError: payload={}
                payload["publish_at"]=scheduled.isoformat(timespec="seconds")
                payload["publication_mode"]="scheduled_public"
                c.execute("UPDATE jobs SET payload_json=?,updated_at=? WHERE id=?",(json.dumps(payload),t,job["id"]))
            if row["youtube_video_id"] and row["remote_processing_status"]=="succeeded":
                existing=c.execute("SELECT id,payload_json FROM jobs WHERE job_type='PUBLISH_YOUTUBE' AND status='QUEUED' AND payload_json LIKE ? LIMIT 1",("%"+published_id+"%",)).fetchone()
                payload={"published_video_id":published_id,"publish_at":scheduled.isoformat(timespec="seconds")}
                due=scheduled.astimezone(timezone.utc).isoformat(timespec="seconds")
                if existing:
                    c.execute("UPDATE jobs SET payload_json=?,next_attempt_at=?,updated_at=?,error='' WHERE id=?",(json.dumps(payload),due,t,existing["id"]))
                else:
                    jid=str(uuid4())
                    c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at,next_attempt_at) VALUES(?,'PUBLISH_YOUTUBE','QUEUED',?,?, ?,?,?,?)",
                              (jid,row["source_id"],row["media_id"],json.dumps(payload),t,t,due))
                c.execute("UPDATE published_videos SET publishing_status='SCHEDULED' WHERE id=?",(published_id,))
            event(c,"INFO","youtube.scheduled_release_rescheduled","Scheduled public release time updated after rechecking rights and QC.",{"published_video_id":published_id,"publish_at":scheduled.isoformat(timespec="seconds")})
        return {"published_video_id":published_id,"scheduled_publish_at":scheduled.isoformat(timespec="seconds"),"rescheduled":True}

    @app.get("/api/youtube/analytics/schedule")
    def youtube_analytics_schedule_status():
        with connect(cfg.database_path) as c:
            values={r["key"]:r["value"] for r in c.execute(
                "SELECT key,value FROM settings WHERE key LIKE 'youtube_analytics_schedule_%'")}
        return {
            "oauth_ready":youtube_oauth_available(),
            "enabled":values.get("youtube_analytics_schedule_enabled")=="true",
            "interval_minutes":int(values.get("youtube_analytics_schedule_interval_minutes","1440")),
            "window_days":int(values.get("youtube_analytics_schedule_window_days","7")),
            "next_run":values.get("youtube_analytics_schedule_next_at") or None,
        }

    @app.put("/api/youtube/analytics/schedule")
    def set_youtube_analytics_schedule(data: AnalyticsScheduleIn):
        if data.enabled:
            status=connected_status()
            if not status.get("connected"):
                raise HTTPException(503,detail="Automatic Analytics requires a connected YouTube channel with current OAuth scopes: "+str(status.get("reason") or "channel not connected"))
        t=now()
        with session(cfg.database_path) as c:
            values={
                "youtube_analytics_schedule_enabled":"true" if data.enabled else "false",
                "youtube_analytics_schedule_interval_minutes":str(data.interval_minutes),
                "youtube_analytics_schedule_window_days":str(data.window_days),
                "youtube_analytics_schedule_next_at":t if data.enabled else "",
            }
            for key,value in values.items():
                c.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,value,t))
            event(c,"INFO","youtube.analytics_schedule_changed","YouTube Analytics scheduler configuration changed.",
                  {"enabled":data.enabled,"interval_minutes":data.interval_minutes,"window_days":data.window_days})
        return {"enabled":data.enabled,"interval_minutes":data.interval_minutes,"window_days":data.window_days,
                "next_run":t if data.enabled else None}

    @app.post("/api/youtube/analytics/sync",status_code=202)
    def sync_youtube_analytics(data: AnalyticsSyncIn):
        status=connected_status()
        if not status.get("connected"):
            raise HTTPException(503,detail="YouTube channel is not connected: "+str(status.get("reason") or "connection check failed"))
        t=now(); jid=str(uuid4())
        with session(cfg.database_path) as c:
            c.execute("BEGIN IMMEDIATE")
            paused=c.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()
            if paused and paused["value"]=="true": raise HTTPException(423,detail="Global pause is enabled.")
            active=c.execute("SELECT id FROM jobs WHERE job_type='SYNC_YOUTUBE_ANALYTICS' AND status IN ('QUEUED','RUNNING') LIMIT 1").fetchone()
            if active: return {"id":active["id"],"status":"ALREADY_QUEUED"}
            c.execute("INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at) VALUES(?,'SYNC_YOUTUBE_ANALYTICS','QUEUED',?,?,?)",(jid,json.dumps({"start_date":data.start_date,"end_date":data.end_date}),t,t))
            event(c,"INFO","youtube.analytics_queued","YouTube Analytics sync queued.",{"job_id":jid,"start_date":data.start_date,"end_date":data.end_date})
        return {"id":jid,"status":"QUEUED","start_date":data.start_date,"end_date":data.end_date}

    @app.get("/api/youtube/analytics/summary")
    def youtube_analytics_summary(start_date: str | None=None, end_date: str | None=None):
        # Keep the report window in UTC to match recurring worker snapshots.
        yesterday=datetime.now(timezone.utc).date()-timedelta(days=1)
        try:
            report_end=date.fromisoformat(end_date) if end_date else yesterday
            report_start=date.fromisoformat(start_date) if start_date else report_end-timedelta(days=6)
        except ValueError as exc:
            raise HTTPException(422,detail="start_date and end_date must use YYYY-MM-DD.") from exc
        if report_end>yesterday:
            raise HTTPException(422,detail="Analytics comparison only supports completed days; end_date must be yesterday or earlier.")
        if report_start>report_end:
            raise HTTPException(422,detail="start_date cannot be after end_date.")
        with connect(cfg.database_path) as c:
            rows=[dict(r) for r in c.execute("""SELECT a.id,a.published_video_id,a.captured_at,a.metrics_json,
                p.title,p.youtube_video_id,COALESCE(s.game,'Unknown game') AS game,
                COALESCE(cp.chosen_format,'unknown') AS content_format
                FROM analytics_snapshots a
                JOIN published_videos p ON p.id=a.published_video_id
                LEFT JOIN sources s ON s.id=p.source_id
                LEFT JOIN content_plans cp ON cp.candidate_id=p.candidate_id
                ORDER BY a.captured_at DESC""")]
        latest_by_video={}
        for row in rows:
            pid=row["published_video_id"]
            if pid in latest_by_video:
                continue
            try: snapshot=json.loads(row["metrics_json"] or "{}")
            except (ValueError,TypeError): continue
            if snapshot.get("start_date")!=report_start.isoformat() or snapshot.get("end_date")!=report_end.isoformat():
                continue
            metrics=snapshot.get("metrics")
            if not isinstance(metrics,dict):
                continue
            row["metrics"]=metrics
            latest_by_video[pid]=row
        metric_sums=("views","likes","comments","shares","subscribersGained","subscribersLost","estimatedMinutesWatched")
        metric_averages=("averageViewDuration","averageViewPercentage")
        grouped={}
        video_rows=[]
        def numeric(value):
            if isinstance(value,bool) or value is None: return None
            try: n=float(value)
            except (TypeError,ValueError): return None
            return n if n>=0 else None
        for row in latest_by_video.values():
            metrics=row["metrics"]
            game=str(row.get("game") or "Unknown game")
            content_format=str(row.get("content_format") or "unknown")
            item={"published_video_id":row["published_video_id"],"youtube_video_id":row["youtube_video_id"],
                  "title":row["title"],"game":game,"format":content_format,"captured_at":row["captured_at"]}
            for key in (*metric_sums,*metric_averages):
                if key in metrics:
                    item[key]=numeric(metrics.get(key))
            video_rows.append(item)
            group=grouped.setdefault((game,content_format),{"game":game,"format":content_format,"sample_size":0,"_sum":{},"_avg":{}})
            group["sample_size"]+=1
            for key in metric_sums:
                n=numeric(metrics.get(key))
                if n is not None: group["_sum"].setdefault(key,[]).append(n)
            for key in metric_averages:
                n=numeric(metrics.get(key))
                if n is not None: group["_avg"].setdefault(key,[]).append(n)
        groups=[]
        for group in grouped.values():
            result={"game":group["game"],"format":group["format"],"sample_size":group["sample_size"]}
            for key,values in group["_sum"].items():
                total=sum(values)
                result[key+"_total"]=int(total) if key not in {"estimatedMinutesWatched"} else round(total,2)
                result[key+"_reported_videos"]=len(values)
            for key,values in group["_avg"].items():
                result[key+"_mean"]=round(sum(values)/len(values),2) if values else None
                result[key+"_reported_videos"]=len(values)
            result["minimum_sample_met"]=group["sample_size"]>=3
            groups.append(result)
        groups.sort(key=lambda g:(-(g.get("averageViewPercentage_mean") if g.get("averageViewPercentage_mean") is not None and g["minimum_sample_met"] else -1),-g["sample_size"],g["game"],g["format"]))
        supported=[g for g in groups if g["minimum_sample_met"] and g.get("averageViewPercentage_mean") is not None]
        best=supported[0] if supported else None
        return {
            "window":{"start_date":report_start.isoformat(),"end_date":report_end.isoformat(),"days":(report_end-report_start).days+1},
            "matched_video_count":len(video_rows),
            "groups":groups,
            "best_supported_group":best,
            "recommendation_status":"COMPARABLE_SAMPLE" if best else "INSUFFICIENT_SAMPLE",
            "recommendation_note":(
                "Highest mean average-view percentage among same-window groups with at least three videos. This is descriptive, not proof of causality."
                if best else "No game/format group has at least three comparable videos with average-view percentage data in this exact reporting window."
            ),
            "videos":video_rows[:100],
        }

    @app.get("/api/youtube/analytics")
    def youtube_analytics():
        with connect(cfg.database_path) as c:
            return [dict(r) for r in c.execute("""SELECT a.*,p.title,p.youtube_video_id FROM analytics_snapshots a
                JOIN published_videos p ON p.id=a.published_video_id ORDER BY a.captured_at DESC LIMIT 500""")]


    @app.get("/api/renders/{render_id}/thumbnail")
    def render_thumbnail(render_id: str):
        with connect(cfg.database_path) as c:
            row=c.execute("SELECT thumbnail_path,thumbnail_sha256,thumbnail_width,thumbnail_height FROM rendered_outputs WHERE id=?",(render_id,)).fetchone()
        if not row: raise HTTPException(404,detail="Rendered output not found.")
        if not row["thumbnail_path"]: raise HTTPException(404,detail="Local poster frame was not generated for this render.")
        path=Path(row["thumbnail_path"]).resolve()
        if not path.is_file(): raise HTTPException(404,detail="Poster image is missing.")
        try: path.relative_to((cfg.database_path.parent/"outputs").resolve())
        except ValueError: raise HTTPException(403,detail="Poster path is outside the managed output directory.")
        if row["thumbnail_width"]!=720 or row["thumbnail_height"]!=1280:
            raise HTTPException(409,detail="Poster metadata does not match the expected 720x1280 dimensions.")
        if row["thumbnail_sha256"] and digest(path)!=row["thumbnail_sha256"]:
            raise HTTPException(409,detail="Poster hash does not match the registered artifact.")
        return FileResponse(path,media_type="image/jpeg",filename=path.name)

    @app.get("/api/renders/{render_id}/file")
    def render_file(render_id: str):
        with connect(cfg.database_path) as c:
            row=c.execute("SELECT output_path,qc_status FROM rendered_outputs WHERE id=?",(render_id,)).fetchone()
        if not row: raise HTTPException(404,detail="Rendered output not found.")
        # Allow previewing REVIEW outputs so the owner can inspect QC flags; publishing still requires PASS.
        path=Path(row["output_path"]).resolve()
        if not path.is_file(): raise HTTPException(404,detail="Rendered file is missing.")
        try: path.relative_to((cfg.database_path.parent/"outputs").resolve())
        except ValueError: raise HTTPException(403,detail="Output path is outside the managed output directory.")
        return FileResponse(path,media_type="video/mp4",filename=path.name)

    @app.post("/api/publishing/videos/{published_id}/reconcile")
    def reconcile_publication(published_id: str, data: ReconcileVideoIn):
        channel=connected_status()
        if not channel.get("connected"):
            raise HTTPException(503,detail="YouTube channel is not connected: "+str(channel.get("reason") or "connection check failed"))
        service=youtube_service()
        channel_items=service.channels().list(part="id",mine=True,maxResults=1).execute().get("items",[])
        if not channel_items: raise HTTPException(503,detail="OAuth could not identify the connected channel.")
        channel_id=channel_items[0]["id"]
        remote=service.videos().list(part="snippet,status,processingDetails",id=data.youtube_video_id).execute().get("items",[])
        if not remote: raise HTTPException(404,detail="That video ID is not visible to the connected channel.")
        video=remote[0]
        if video.get("snippet",{}).get("channelId")!=channel_id:
            raise HTTPException(403,detail="The supplied video does not belong to the connected YouTube channel.")
        remote_privacy=str((video.get("status") or {}).get("privacyStatus","unknown")).lower()
        remote_processing=str((video.get("processingDetails") or {}).get("processingStatus","unknown")).lower()
        t=now()
        with session(cfg.database_path) as c:
            row=c.execute("""SELECT pv.*,p.status AS rights_status,p.expires_at,
                ro.qc_status AS render_qc,ro.sha256 AS render_sha,ro.file_size AS render_size
                FROM published_videos pv JOIN media_assets m ON m.id=pv.media_id
                JOIN source_permissions p ON p.source_id=m.source_id
                LEFT JOIN rendered_outputs ro ON ro.id=pv.rendered_output_id WHERE pv.id=?""",(published_id,)).fetchone()
            if not row: raise HTTPException(404,detail="Publication record not found.")
            if row["publishing_status"]!="RECONCILE_REQUIRED":
                raise HTTPException(409,detail="This publication is not awaiting reconciliation.")
            existing=c.execute("SELECT id FROM published_videos WHERE youtube_video_id=? AND id<>?",(data.youtube_video_id,published_id)).fetchone()
            if existing: raise HTTPException(409,detail="That YouTube video ID is already assigned to another local publication.")
            rights_current=row["rights_status"]=="AUTHORIZED" and not expiry_passed(row["expires_at"])
            scheduled=row["scheduled_publish_at"]
            scheduled_dt=None
            if scheduled:
                try: scheduled_dt=datetime.fromisoformat(scheduled.replace("Z","+00:00"))
                except ValueError: scheduled_dt=None
                if scheduled_dt and scheduled_dt.tzinfo is None: scheduled_dt=scheduled_dt.replace(tzinfo=timezone.utc)
            output_current=False
            try:
                output=Path(row["output_path"])
                output_current=bool(row["render_qc"]=="PASS" and row["qc_status"]=="PASS" and
                    row["render_size"] is not None and output.is_file() and output.stat().st_size==row["render_size"] and
                    row["render_sha"] and digest(output)==row["render_sha"])
            except (OSError,ValueError,TypeError):
                output_current=False
            try: expiry_dt=datetime.fromisoformat(row["expires_at"].replace("Z","+00:00")) if row["expires_at"] else None
            except (ValueError,TypeError): expiry_dt=None
            if expiry_dt and expiry_dt.tzinfo is None: expiry_dt=expiry_dt.replace(tzinfo=timezone.utc)
            rights_cover_schedule=bool(rights_current and scheduled_dt and (expiry_dt is None or expiry_dt>scheduled_dt))
            uploads_setting=c.execute("SELECT value FROM settings WHERE key='youtube_uploads_enabled'").fetchone()
            uploads_enabled=bool(uploads_setting and uploads_setting["value"]=="true" and os.getenv("GCF_ENABLE_YOUTUBE_UPLOADS","").lower()=="true")
            public_enabled=os.getenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","").lower()=="true"
            schedule_valid=bool(scheduled_dt and rights_cover_schedule and output_current and uploads_enabled and public_enabled)
            release_due=bool(schedule_valid and scheduled_dt<=datetime.now(timezone.utc))
            remote_is_public=remote_privacy=="public"
            remote_is_private=remote_privacy=="private"
            # Treat unexpected visibility as unsafe. The only state we accept as PUBLISHED is a public video
            # whose currently configured schedule, rights, output integrity and remote processing all agree.
            allowed_public=bool(remote_is_public and release_due and remote_processing=="succeeded")
            if not remote_is_private and not allowed_public:
                try:
                    service.videos().update(part="status",body={"id":data.youtube_video_id,"status":{"privacyStatus":"private"}}).execute()
                    remote_privacy="private"
                except Exception as exc:
                    raise HTTPException(502,detail=f"Could not force the reconciled video to private ({type(exc).__name__}); the publication remains RECONCILE_REQUIRED.") from exc
            if not rights_current:
                final_state="BLOCKED_RIGHTS"
                c.execute("UPDATE jobs SET status='CANCELLED',error='Rights are expired, revoked or not authorized.',updated_at=?,finished_at=? WHERE job_type='PUBLISH_YOUTUBE' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ?",(t,t,"%"+published_id+"%"))
                c.execute("UPDATE published_videos SET publishing_status=?,youtube_video_id=?,scheduled_publish_at=NULL,remote_processing_status=?,processing_checked_at=?,processing_failure_reason='Rights are expired, revoked or not authorized.',updated_at=? WHERE id=?",
                          (final_state,data.youtube_video_id,remote_processing,t,t,published_id))
            elif not output_current:
                final_state="BLOCKED_QC"
                c.execute("UPDATE jobs SET status='CANCELLED',error='Rendered output no longer matches the QC-approved artifact.',updated_at=?,finished_at=? WHERE job_type='PUBLISH_YOUTUBE' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ?",(t,t,"%"+published_id+"%"))
                c.execute("UPDATE published_videos SET publishing_status=?,youtube_video_id=?,scheduled_publish_at=NULL,remote_processing_status=?,processing_checked_at=?,processing_failure_reason='Rendered output no longer matches the QC-approved artifact.',updated_at=? WHERE id=?",
                          (final_state,data.youtube_video_id,remote_processing,t,t,published_id))
            elif allowed_public:
                final_state="PUBLISHED"
                c.execute("UPDATE published_videos SET publishing_status=?,youtube_video_id=?,remote_processing_status='succeeded',processing_checked_at=?,processing_failure_reason='',updated_at=? WHERE id=?",
                          (final_state,data.youtube_video_id,t,t,published_id))
                c.execute("UPDATE jobs SET status='CANCELLED',error='Remote publication is already complete; reconciled.',updated_at=?,finished_at=? WHERE job_type='PUBLISH_YOUTUBE' AND status='QUEUED' AND payload_json LIKE ?",(t,t,"%"+published_id+"%"))
            elif scheduled and not rights_cover_schedule:
                final_state="BLOCKED_RIGHTS"
                c.execute("UPDATE jobs SET status='CANCELLED',error='Source permission does not cover the scheduled release time.',updated_at=?,finished_at=? WHERE job_type='PUBLISH_YOUTUBE' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ?",(t,t,"%"+published_id+"%"))
                c.execute("UPDATE published_videos SET publishing_status=?,youtube_video_id=?,scheduled_publish_at=NULL,remote_processing_status=?,processing_checked_at=?,processing_failure_reason='Source permission does not cover the scheduled release time.',updated_at=? WHERE id=?",
                          (final_state,data.youtube_video_id,remote_processing,t,t,published_id))
            elif remote_processing in {"failed","terminated"}:
                final_state="PROCESSING_FAILED" if remote_processing=="failed" else "PROCESSING_TERMINATED"
                failure=(video.get("processingDetails") or {}).get("processingFailureReason") or remote_processing
                c.execute("UPDATE jobs SET status='CANCELLED',error=?,updated_at=?,finished_at=? WHERE job_type='PUBLISH_YOUTUBE' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ?",(str(failure),t,t,"%"+published_id+"%"))
                c.execute("UPDATE published_videos SET publishing_status=?,youtube_video_id=?,remote_processing_status=?,processing_checked_at=?,processing_failure_reason=?,scheduled_publish_at=NULL,updated_at=? WHERE id=?",
                          (final_state,data.youtube_video_id,remote_processing,t,str(failure),t,published_id))
            elif remote_processing!="succeeded":
                # Recovered private upload still processing: poll again, then schedule only after fresh checks.
                final_state="PROCESSING"
                c.execute("UPDATE published_videos SET publishing_status=?,youtube_video_id=?,remote_processing_status=?,processing_checked_at=?,processing_failure_reason='',updated_at=? WHERE id=?",
                          (final_state,data.youtube_video_id,remote_processing,t,t,published_id))
                active=c.execute("SELECT id FROM jobs WHERE job_type='CHECK_YOUTUBE_PROCESSING' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ? LIMIT 1",("%"+published_id+"%",)).fetchone()
                if not active:
                    c.execute("INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at) VALUES(?,'CHECK_YOUTUBE_PROCESSING','QUEUED',?,?,?)",
                              (str(uuid4()),json.dumps({"published_video_id":published_id,"poll_attempt":0,"max_polls":20}),t,t))
            elif schedule_valid:
                final_state="SCHEDULED"
                c.execute("UPDATE published_videos SET publishing_status=?,youtube_video_id=?,remote_processing_status='succeeded',processing_checked_at=?,processing_failure_reason='',updated_at=? WHERE id=?",
                          (final_state,data.youtube_video_id,t,t,published_id))
                active=c.execute("SELECT id FROM jobs WHERE job_type='PUBLISH_YOUTUBE' AND status='QUEUED' AND payload_json LIKE ? LIMIT 1",("%"+published_id+"%",)).fetchone()
                due=max(scheduled_dt,datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
                payload=json.dumps({"published_video_id":published_id,"publish_at":scheduled_dt.isoformat()})
                if active:
                    c.execute("UPDATE jobs SET payload_json=?,next_attempt_at=?,updated_at=?,error='' WHERE id=?",(payload,due,t,active["id"]))
                else:
                    c.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at,next_attempt_at) VALUES(?,'PUBLISH_YOUTUBE','QUEUED',?,?, ?,?,?,?)",
                              (str(uuid4()),row["source_id"],row["media_id"],payload,t,t,due))
            else:
                # No currently authorized schedule or public policy: leave the video private and clear stale intent.
                final_state="UPLOADED_PRIVATE"
                c.execute("UPDATE jobs SET status='CANCELLED',error='No valid, enabled public-release policy remains.',updated_at=?,finished_at=? WHERE job_type='PUBLISH_YOUTUBE' AND status IN ('QUEUED','RUNNING') AND payload_json LIKE ?",(t,t,"%"+published_id+"%"))
                c.execute("UPDATE published_videos SET publishing_status=?,youtube_video_id=?,scheduled_publish_at=NULL,remote_processing_status='succeeded',processing_checked_at=?,processing_failure_reason='',updated_at=? WHERE id=?",
                          (final_state,data.youtube_video_id,t,t,published_id))
            for pending_job in c.execute("SELECT id,payload_json FROM jobs WHERE status='RECONCILE_REQUIRED'").fetchall():
                try:
                    if json.loads(pending_job["payload_json"] or "{}").get("published_video_id")==published_id:
                        c.execute("UPDATE jobs SET status='COMPLETED',error='Reconciled against the connected YouTube channel.',updated_at=?,finished_at=? WHERE id=?",(t,t,pending_job["id"]))
                except (ValueError,TypeError):
                    continue
            event(c,"WARNING" if final_state not in {"PUBLISHED","SCHEDULED","UPLOADED_PRIVATE","PROCESSING"} else "INFO",
                  "youtube.publication_reconciled","Uncertain YouTube operation reconciled against the connected channel.",
                  {"published_video_id":published_id,"youtube_video_id":data.youtube_video_id,"remote_privacy_status":remote_privacy,
                   "remote_processing_status":remote_processing,"local_status":final_state,"rights_current":rights_current,
                   "output_current":output_current,"public_gate":public_enabled})
        return {"published_video_id":published_id,"youtube_video_id":data.youtube_video_id,"remote_privacy_status":remote_privacy,
                "remote_processing_status":remote_processing,"local_status":final_state,"reconciled":True}

    @app.get("/api/events")
    def events():
        with connect(cfg.database_path) as c: return [dict(r) for r in c.execute("SELECT * FROM system_events ORDER BY created_at DESC LIMIT 100")]
    return app

app=create_app()
