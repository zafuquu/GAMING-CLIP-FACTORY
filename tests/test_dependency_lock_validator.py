from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts import validate_dependency_locks as validator


HASH = hashlib.sha256(b"test-only-lock-artifact").hexdigest()


def lock_stanza(name: str, version: str, digest: str = HASH) -> str:
    return f"{name}=={version} \\\n    --hash=sha256:{digest}\n"


def make_valid_repo(root: Path) -> None:
    backend = root / "backend"
    frontend = root / "frontend"
    backend.mkdir(parents=True)
    frontend.mkdir(parents=True)

    (backend / "requirements.txt").write_text(
        "demo-base>=1.0,<2.0\n", encoding="utf-8"
    )
    (backend / "requirements-ml.txt").write_text(
        "-r requirements.txt\nfaster-whisper>=1.1,<2.0\n", encoding="utf-8"
    )

    (frontend / "package.json").write_text(
        json.dumps(
            {
                "name": "demo-dashboard",
                "version": "1.0.0",
                "dependencies": {"react": "^19.0.0"},
                "devDependencies": {"typescript": "~5.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (frontend / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "demo-dashboard",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {
                    "": {
                        "name": "demo-dashboard",
                        "version": "1.0.0",
                        "dependencies": {"react": "^19.0.0"},
                        "devDependencies": {"typescript": "~5.0.0"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    for os_key in ("linux", "win"):
        for py_key in ("py311", "py312"):
            base_lock = backend / f"requirements-lock-{os_key}-{py_key}.txt"
            ml_lock = backend / f"requirements-ml-lock-{os_key}-{py_key}.txt"
            base_lock.write_text(lock_stanza("demo-base", "1.5.0"), encoding="utf-8")
            ml_lock.write_text(
                lock_stanza("demo-base", "1.5.0")
                + lock_stanza("faster-whisper", "1.2.0"),
                encoding="utf-8",
            )


def invoke_validator(root: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(validator, "ROOT", root)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_dependency_locks.py",
            "--os-key",
            "win",
            "--python-key",
            "py312",
        ],
    )
    return validator.main()


def test_lock_validator_accepts_consistent_hashed_lock_matrix(tmp_path, monkeypatch, capsys):
    make_valid_repo(tmp_path)

    assert invoke_validator(tmp_path, monkeypatch) == 0
    assert "PASS:" in capsys.readouterr().out


def test_lock_parser_rejects_package_without_sha256_hash(tmp_path):
    lock = tmp_path / "requirements-lock.txt"
    lock.write_text("demo-base==1.5.0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no SHA-256 hash"):
        validator.parse_lock(lock)


def test_manifest_validator_rejects_incompatible_exact_pin(tmp_path):
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("demo-base>=2.0,<3.0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="does not satisfy"):
        validator.validate_requirements(manifest, {"demo-base": "1.5.0"})


def test_validator_rejects_optional_ml_lock_that_changes_base_pin(
    tmp_path, monkeypatch
):
    make_valid_repo(tmp_path)
    ml_lock = tmp_path / "backend" / "requirements-ml-lock-win-py312.txt"
    ml_lock.write_text(
        lock_stanza("demo-base", "1.6.0")
        + lock_stanza("faster-whisper", "1.2.0"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="preserve the standard lock"):
        invoke_validator(tmp_path, monkeypatch)


def test_validator_rejects_stale_npm_lock_root(tmp_path, monkeypatch):
    make_valid_repo(tmp_path)
    package_lock = tmp_path / "frontend" / "package-lock.json"
    lock_data = json.loads(package_lock.read_text(encoding="utf-8"))
    lock_data["packages"][""]["dependencies"]["react"] = "^18.0.0"
    package_lock.write_text(json.dumps(lock_data), encoding="utf-8")

    with pytest.raises(ValueError, match="root field 'dependencies'"):
        invoke_validator(tmp_path, monkeypatch)
