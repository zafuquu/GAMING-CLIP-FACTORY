"""Opt-in local text-to-speech through the installed Piper CLI.

No network calls, voice downloads, or bundled voice models. The owner supplies
an executable and model whose license is suitable for the intended use.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import uuid
import wave
from pathlib import Path

class NarrationNotConfigured(RuntimeError):
    pass

class NarrationError(RuntimeError):
    pass

MAX_SCRIPT_CHARS=1500

def _configured_file(env_name: str, *, executable: bool=False) -> Path | None:
    raw=os.getenv(env_name,"").strip()
    if not raw:
        return None
    path=Path(raw).expanduser()
    if not path.is_absolute():
        path=Path.cwd()/path
    path=path.resolve()
    if not path.is_file():
        raise NarrationNotConfigured(f"{env_name} must point to an existing file.")
    if executable and os.name!="nt" and not os.access(path,os.X_OK):
        raise NarrationNotConfigured(f"{env_name} must point to an executable file.")
    return path

def get_piper_config() -> dict:
    executable=_configured_file("GCF_PIPER_EXECUTABLE_PATH",executable=True)
    if executable is None:
        found=shutil.which("piper") or shutil.which("piper.exe")
        if found:
            executable=Path(found).resolve()
    if executable is None:
        raise NarrationNotConfigured("Set GCF_PIPER_EXECUTABLE_PATH to an installed local Piper executable.")
    model=_configured_file("GCF_PIPER_MODEL_PATH")
    if model is None or model.suffix.lower()!=".onnx":
        raise NarrationNotConfigured("Set GCF_PIPER_MODEL_PATH to a local Piper .onnx voice model.")
    config_path=_configured_file("GCF_PIPER_CONFIG_PATH")
    if config_path is None:
        adjacent=Path(str(model)+".json")
        if adjacent.is_file():
            config_path=adjacent.resolve()
    if config_path is None or config_path.suffix.lower()!=".json":
        raise NarrationNotConfigured("Set GCF_PIPER_CONFIG_PATH or place the matching .onnx.json beside the voice model.")
    try:
        timeout=max(10,min(900,int(os.getenv("GCF_PIPER_TIMEOUT_SECONDS","180"))))
        max_duration=max(5,min(300,int(os.getenv("GCF_NARRATION_MAX_DURATION_SECONDS","60"))))
    except (TypeError,ValueError) as exc:
        raise NarrationNotConfigured("Piper timeout and maximum narration duration must be integers.") from exc
    speaker_raw=os.getenv("GCF_PIPER_SPEAKER_ID","").strip()
    if speaker_raw:
        try: speaker_id=int(speaker_raw)
        except ValueError as exc: raise NarrationNotConfigured("GCF_PIPER_SPEAKER_ID must be an integer when set.") from exc
        if speaker_id<0: raise NarrationNotConfigured("GCF_PIPER_SPEAKER_ID cannot be negative.")
    else:
        speaker_id=None
    return {
        "executable":executable,
        "model":model,
        "config":config_path,
        "speaker_id":speaker_id,
        "timeout_seconds":timeout,
        "max_duration_seconds":max_duration,
        "enabled":os.getenv("GCF_ENABLE_NARRATION","").lower()=="true",
    }

def narration_status() -> dict:
    enabled=os.getenv("GCF_ENABLE_NARRATION","").lower()=="true"
    try:
        config=get_piper_config()
        ready=True
        reason=None
    except NarrationNotConfigured as exc:
        config={}
        ready=False
        reason=str(exc)
    return {
        "enabled":enabled,
        "ready":enabled and ready,
        "executable_configured":bool(config.get("executable")),
        "model_configured":bool(config.get("model")),
        "voice_config_configured":bool(config.get("config")),
        "speaker_id":config.get("speaker_id"),
        "timeout_seconds":config.get("timeout_seconds"),
        "max_duration_seconds":config.get("max_duration_seconds"),
        "reason":reason if not ready else ("Set GCF_ENABLE_NARRATION=true to enable synthesis." if not enabled else None),
        "network_calls":False,
        "model_downloads":False,
    }

def validate_script(text: str) -> str:
    if not isinstance(text,str):
        raise ValueError("Narration script must be text.")
    clean=" ".join(text.replace("\r"," ").replace("\n"," ").replace("\t"," ").split())
    if not clean:
        raise ValueError("Enter an owner-reviewed narration script before generating audio.")
    if len(clean)>MAX_SCRIPT_CHARS:
        raise ValueError(f"Narration script is limited to {MAX_SCRIPT_CHARS} characters.")
    if any(ord(ch)<32 for ch in clean):
        raise ValueError("Narration script contains unsupported control characters.")
    return clean

def synthesize_piper(text: str, output_path: str | Path) -> dict:
    if os.getenv("GCF_ENABLE_NARRATION","").lower()!="true":
        raise NarrationNotConfigured("Local narration is disabled. Set GCF_ENABLE_NARRATION=true after configuring a suitable Piper voice.")
    script=validate_script(text)
    config=get_piper_config()
    destination=Path(output_path).expanduser().resolve()
    if destination.suffix.lower()!=".wav":
        raise ValueError("Narration output must use the WAV extension.")
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():
        raise FileExistsError("Narration output paths must be unique; existing audio is never overwritten.")
    temp_path=destination.with_name(destination.stem+"."+uuid.uuid4().hex+".partial.wav")
    args=[str(config["executable"]),"--model",str(config["model"]),"--config",str(config["config"]),"--output_file",str(temp_path)]
    if config["speaker_id"] is not None:
        args.extend(["--speaker",str(config["speaker_id"])])
    try:
        completed=subprocess.run(
            args,
            input=script+"\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config["timeout_seconds"],
            shell=False,
            cwd=str(config["executable"].parent),
        )
    except subprocess.TimeoutExpired as exc:
        raise NarrationError(f"Local Piper synthesis exceeded its {config['timeout_seconds']} second timeout.") from exc
    except OSError as exc:
        raise NarrationError(f"Could not launch the configured local Piper executable ({type(exc).__name__}).") from exc
    try:
        if completed.returncode!=0:
            # Do not echo the script or a command line that could contain user text.
            detail=(completed.stderr or completed.stdout or "").strip()[-800:]
            raise NarrationError(f"Piper exited with code {completed.returncode}: {detail}")
        if not temp_path.is_file() or temp_path.stat().st_size<=44:
            raise NarrationError("Piper returned success but did not write a nonempty WAV file.")
        try:
            with wave.open(str(temp_path),"rb") as audio:
                channels=audio.getnchannels()
                rate=audio.getframerate()
                sample_width=audio.getsampwidth()
                frames=audio.getnframes()
                duration=frames/float(rate) if rate else 0
        except (wave.Error,EOFError,OSError) as exc:
            raise NarrationError("Piper output was not a valid PCM WAV file.") from exc
        if channels<1 or rate<8000 or sample_width not in {1,2,3,4} or frames<=0:
            raise NarrationError("Piper output has invalid or unsupported WAV audio metadata.")
        if duration<=0 or duration>config["max_duration_seconds"]:
            raise NarrationError(f"Narration audio duration {duration:.1f}s exceeds the configured limit of {config['max_duration_seconds']}s.")
        if destination.exists():
            raise FileExistsError("Narration destination appeared while synthesis was running; refusing to overwrite it.")
        os.replace(temp_path,destination)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    sha=hashlib.sha256()
    with destination.open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):
            sha.update(block)
    return {
        "path":str(destination),
        "sha256":sha.hexdigest(),
        "file_size":destination.stat().st_size,
        "duration_seconds":round(duration,3),
        "sample_rate":rate,
        "channels":channels,
        "sample_width_bytes":sample_width,
        "model_name":config["model"].name,
        "config_name":config["config"].name,
        "speaker_id":config["speaker_id"],
    }
