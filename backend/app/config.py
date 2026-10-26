from dataclasses import dataclass
from pathlib import Path
import os, shutil

ROOT = Path(__file__).resolve().parents[2]

@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    media_root: Path
    database_path: Path
    ffmpeg_path: str | None
    ffprobe_path: str | None
    timeout_seconds: int = 120

    @classmethod
    def load(cls):
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env", override=False)
        except ImportError:
            pass
        root = Path(os.getenv("GCF_PROJECT_ROOT") or ROOT).resolve()
        data = Path(os.getenv("GCF_DATA_DIR") or root / "data").resolve()
        media = Path(os.getenv("GCF_MEDIA_ROOT") or data / "media").resolve()
        db = Path(os.getenv("GCF_DATABASE_PATH") or data / "gaming_clip_factory.sqlite3").resolve()
        bin_dir = root / "tools" / "ffmpeg"
        extracted = root / "ffmpeg-9.0.2-essentials_build" / "bin"
        def locate(env, name, candidates):
            value = os.getenv(env)
            if value:
                p = Path(value).expanduser()
                return str(p.resolve()) if p.is_file() else None
            for p in candidates:
                if p.is_file(): return str(p.resolve())
            return shutil.which(name)
        return cls(root, data, media, db,
            locate("GCF_FFMPEG_PATH", "ffmpeg", [bin_dir/"ffmpeg.exe", extracted/"ffmpeg.exe"]),
            locate("GCF_FFPROBE_PATH", "ffprobe", [bin_dir/"ffprobe.exe", extracted/"ffprobe.exe"]))
