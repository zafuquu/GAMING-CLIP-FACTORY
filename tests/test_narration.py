from pathlib import Path
import os
import subprocess
import sys
import wave

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))

import app.narration as narration
from app.narration import NarrationError, NarrationNotConfigured, synthesize_piper, validate_script

def configure_piper(tmp_path,monkeypatch,*,max_seconds="60"):
    exe=tmp_path/("piper.exe" if os.name=="nt" else "piper")
    exe.write_bytes(b"test executable placeholder")
    if os.name!="nt":
        exe.chmod(0o755)
    model=tmp_path/"voice.onnx"
    model.write_bytes(b"fake onnx model fixture")
    config=tmp_path/"voice.onnx.json"
    config.write_text('{"sample_rate":22050}',encoding="utf-8")
    monkeypatch.setenv("GCF_ENABLE_NARRATION","true")
    monkeypatch.setenv("GCF_PIPER_EXECUTABLE_PATH",str(exe))
    monkeypatch.setenv("GCF_PIPER_MODEL_PATH",str(model))
    monkeypatch.setenv("GCF_PIPER_CONFIG_PATH",str(config))
    monkeypatch.setenv("GCF_NARRATION_MAX_DURATION_SECONDS",max_seconds)
    monkeypatch.setenv("GCF_PIPER_TIMEOUT_SECONDS","30")
    return exe,model,config

def write_wave(path: Path,duration_seconds: float,rate: int=22050):
    with wave.open(str(path),"wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\x00\x00"*int(duration_seconds*rate))

def test_validate_script_normalizes_whitespace_and_rejects_empty_or_oversized():
    assert validate_script("  Hello,\n   gamer!  ")=="Hello, gamer!"
    with pytest.raises(ValueError,match="owner-reviewed"):
        validate_script(" \n\t ")
    with pytest.raises(ValueError,match="1500"):
        validate_script("x"*1501)
    with pytest.raises(ValueError,match="control"):
        validate_script("valid\x00invalid")

def test_piper_setup_is_explicit_and_missing_executable_is_reported(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_NARRATION","true")
    monkeypatch.delenv("GCF_PIPER_EXECUTABLE_PATH",raising=False)
    monkeypatch.delenv("GCF_PIPER_MODEL_PATH",raising=False)
    monkeypatch.delenv("GCF_PIPER_CONFIG_PATH",raising=False)
    monkeypatch.setenv("PATH","")
    with pytest.raises(NarrationNotConfigured,match="PIPER_EXECUTABLE_PATH"):
        narration.get_piper_config()

def test_synthesis_invokes_local_piper_and_records_audio_metadata(tmp_path,monkeypatch):
    exe,model,config=configure_piper(tmp_path,monkeypatch)
    invoked={}
    def fake_run(args,**kwargs):
        invoked["args"]=args
        invoked["kwargs"]=kwargs
        output=Path(args[args.index("--output_file")+1])
        write_wave(output,1.25)
        return subprocess.CompletedProcess(args,0,stdout="",stderr="")
    monkeypatch.setattr(narration.subprocess,"run",fake_run)
    destination=tmp_path/"outputs"/"narration.wav"
    result=synthesize_piper("This is an owner-reviewed voiceover.",destination)
    assert destination.is_file()
    assert result["path"]==str(destination.resolve())
    assert result["duration_seconds"]==1.25
    assert result["sample_rate"]==22050
    assert result["channels"]==1
    assert result["model_name"]==model.name
    assert result["config_name"]==config.name
    assert invoked["kwargs"]["shell"] is False
    assert invoked["kwargs"]["timeout"]==30
    assert invoked["kwargs"]["input"]=="This is an owner-reviewed voiceover.\n"
    assert invoked["kwargs"]["cwd"]==str(exe.parent.resolve())
    assert "--model" in invoked["args"] and "--config" in invoked["args"]

def test_synthesis_timeout_cleans_temporary_wav(tmp_path,monkeypatch):
    configure_piper(tmp_path,monkeypatch)
    def fake_run(args,**kwargs):
        output=Path(args[args.index("--output_file")+1])
        write_wave(output,1.0)
        raise subprocess.TimeoutExpired(args,kwargs["timeout"])
    monkeypatch.setattr(narration.subprocess,"run",fake_run)
    with pytest.raises(NarrationError,match="timeout"):
        synthesize_piper("A short reviewed script.",tmp_path/"out.wav")
    assert not (tmp_path/"out.wav").exists()
    assert list(tmp_path.glob("*.partial.wav"))==[]

def test_synthesis_rejects_voiceover_longer_than_configured_limit(tmp_path,monkeypatch):
    configure_piper(tmp_path,monkeypatch,max_seconds="5")
    monkeypatch.setattr(narration.subprocess,"run",lambda args,**kwargs:(
        write_wave(Path(args[args.index("--output_file")+1]),6.0),
        subprocess.CompletedProcess(args,0,stdout="",stderr="")
    )[1])
    destination=tmp_path/"long.wav"
    with pytest.raises(NarrationError,match="exceeds the configured limit"):
        synthesize_piper("This script is too long.",destination)
    assert not destination.exists()
    assert list(tmp_path.glob("*.partial.wav"))==[]

def test_synthesis_never_overwrites_existing_audio(tmp_path,monkeypatch):
    configure_piper(tmp_path,monkeypatch)
    dest=tmp_path/"existing.wav"
    dest.write_bytes(b"keep me")
    monkeypatch.setattr(narration.subprocess,"run",lambda *args,**kwargs:pytest.fail("Piper must not be invoked"))
    with pytest.raises(FileExistsError):
        synthesize_piper("A reviewed script.",dest)
    assert dest.read_bytes()==b"keep me"
