#!/usr/bin/env python3
"""Validate that direct manifests are represented by the correct hashed lock matrix.

This deliberately uses only the standard library so it can run before installing app
dependencies. The actual install still uses pip's --require-hashes and npm ci.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_LINE = re.compile(r"^\s*([A-Za-z0-9_.-]+)==([^\s\\]+)")
REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?(.*)$")
VERSION_PARTS = re.compile(r"^\s*(\d+(?:\.\d+)*)(.*)$")
SPECIFIER = re.compile(r"\s*(===|==|!=|~=|>=|<=|>|<)\s*([A-Za-z0-9_.+!-]+)\s*")
HASH = re.compile(r"--hash=sha256:[0-9a-fA-F]{64}")
NAME_SEPARATORS = re.compile(r"[-_.]+")


def canonical(name: str) -> str:
    return NAME_SEPARATORS.sub("-", name).lower()


def version_key(value: str) -> tuple[int, ...]:
    match = VERSION_PARTS.match(value)
    if not match:
        raise ValueError(f"Unsupported version syntax in lock validator: {value!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def matches_specifier(pinned: str, expression: str) -> bool:
    if not expression.strip():
        return True
    pin_key = version_key(pinned)
    for clause in expression.split(","):
        if not clause.strip():
            continue
        match = SPECIFIER.fullmatch(clause)
        if not match:
            raise ValueError(f"Unsupported requirement specifier: {clause!r}")
        op, target = match.groups()
        if op in {"===", "=="} and "*" not in target and pinned != target:
            return False
        if op == "!=" and pinned == target:
            return False
        if op in {">=", "<=", ">", "<", "~="}:
            target_key = version_key(target)
            if op == ">=" and not pin_key >= target_key:
                return False
            if op == "<=" and not pin_key <= target_key:
                return False
            if op == ">" and not pin_key > target_key:
                return False
            if op == "<" and not pin_key < target_key:
                return False
            if op == "~=":
                if not pin_key >= target_key:
                    return False
                upper_parts = target_key[:-1]
                if len(target_key) == 1:
                    upper = (target_key[0] + 1,)
                else:
                    upper = tuple(target_key[i] for i in range(len(target_key) - 1))
                    upper = upper[:-1] + (upper[-1] + 1,)
                if not pin_key < upper:
                    return False
    return True


def parse_manifest(path: Path) -> dict[str, tuple[str, str]]:
    requirements: dict[str, tuple[str, str]] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            # Includes and pip options are checked through their own source/lock pair.
            continue
        match = REQ_LINE.match(line)
        if not match:
            raise ValueError(f"{path}:{line_no}: could not parse requirement {raw!r}")
        name, specifier = match.groups()
        key = canonical(name)
        if key in requirements and requirements[key] != (name, specifier):
            raise ValueError(f"{path}:{line_no}: duplicate conflicting requirement {name!r}")
        requirements[key] = (name, specifier.strip())
    return requirements


def parse_lock(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    stanza_name: str | None = None
    stanza_has_hash = False
    line_count = 0

    def finish_stanza() -> None:
        if stanza_name is not None and not stanza_has_hash:
            raise ValueError(f"{path}: package {stanza_name} has no SHA-256 hash")

    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line_count += 1
        match = PACKAGE_LINE.match(raw)
        if match:
            finish_stanza()
            name, version = match.groups()
            key = canonical(name)
            if key in pins and pins[key] != version:
                raise ValueError(f"{path}:{line_no}: conflicting pins for {name}")
            pins[key] = version
            stanza_name = name
            stanza_has_hash = bool(HASH.search(raw))
        elif stanza_name is not None and HASH.search(raw):
            stanza_has_hash = True

    finish_stanza()
    if not line_count or not pins:
        raise ValueError(f"{path}: empty or unparseable lockfile")
    return pins


def validate_requirements(manifest_path: Path, lock_pins: dict[str, str]) -> None:
    for key, (name, specifier) in parse_manifest(manifest_path).items():
        pinned = lock_pins.get(key)
        if pinned is None:
            raise ValueError(f"{manifest_path.name}: {name} is missing from the lockfile")
        if not matches_specifier(pinned, specifier):
            raise ValueError(
                f"{manifest_path.name}: lock pins {name}=={pinned}, "
                f"which does not satisfy {specifier!r}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--os-key", choices=("linux", "win"), required=True)
    parser.add_argument("--python-key", choices=("py311", "py312"), required=True)
    args = parser.parse_args()

    backend = ROOT / "backend"
    frontend = ROOT / "frontend"
    standard_path = backend / f"requirements-lock-{args.os_key}-{args.python_key}.txt"
    ml_path = backend / f"requirements-ml-lock-{args.os_key}-{args.python_key}.txt"
    standard = parse_lock(standard_path)
    ml = parse_lock(ml_path)

    validate_requirements(backend / "requirements.txt", standard)
    validate_requirements(backend / "requirements.txt", ml)
    validate_requirements(backend / "requirements-ml.txt", ml)

    missing_base = sorted(set(standard) - set(ml))
    changed_base = sorted(key for key, version in standard.items() if ml.get(key) != version)
    if missing_base or changed_base:
        raise ValueError(
            "Optional ML lock must preserve the standard lock's exact packages and pins; "
            f"missing={missing_base[:12]}, mismatched={changed_base[:12]}"
        )

    package_json = json.loads((frontend / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((frontend / "package-lock.json").read_text(encoding="utf-8"))
    root_package = package_lock.get("packages", {}).get("")
    if not isinstance(root_package, dict):
        raise ValueError("frontend/package-lock.json has no root package entry")
    for field in ("name", "version", "dependencies", "devDependencies"):
        if root_package.get(field) != package_json.get(field):
            raise ValueError(
                f"frontend/package-lock.json root {field!r} is stale vs package.json"
            )

    print(
        f"PASS: {args.os_key}/{args.python_key} standard lock ({len(standard)} pins), "
        f"ML lock ({len(ml)} pins), direct manifest ranges, SHA-256 hashes, "
        "and npm lock root metadata are consistent."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"DEPENDENCY LOCK VALIDATION FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
