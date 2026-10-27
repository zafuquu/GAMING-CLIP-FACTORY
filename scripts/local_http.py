"""Proxy-free probes for the local Gaming Clip Factory API."""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from typing import Any

BASE_URL = "http://127.0.0.1:8765"
# Ignore HTTP_PROXY/HTTPS_PROXY for loopback service checks. Some Windows CI and
# corporate environments route requests through proxies even for 127.0.0.1.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path: str, timeout: float = 3.0) -> tuple[int, bytes]:
    request = urllib.request.Request(
        BASE_URL + path,
        headers={
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "User-Agent": "GamingClipFactory-LocalProbe/1.0",
        },
    )
    with OPENER.open(request, timeout=timeout) as response:
        return int(response.status), response.read()


def check_health() -> None:
    status, body = get("/api/health")
    payload = json.loads(body.decode("utf-8"))
    if status != 200 or payload.get("status") != "ok":
        raise RuntimeError(f"API health was HTTP {status}, status={payload.get('status')!r}.")
    print("OK: local API health endpoint is responding.")


def check_dashboard() -> None:
    status, body = get("/")
    html = body.decode("utf-8", errors="replace")
    if status != 200 or 'id="root"' not in html:
        raise RuntimeError(f"Dashboard route was HTTP {status} but did not return the React root element.")
    print("OK: production dashboard is being served by the local API.")


def wait_for_app(timeout_seconds: float = 45.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "the local API has not responded yet"
    while time.monotonic() < deadline:
        try:
            check_health()
            check_dashboard()
            return
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
    raise RuntimeError(
        f"API and production dashboard were not ready after {timeout_seconds:.0f}s. "
        f"Last probe: {last_error}"
    )


def check_readiness(require_ready: bool = False) -> None:
    status, body = get("/api/readiness", timeout=5.0)
    payload: dict[str, Any] = json.loads(body.decode("utf-8"))
    checks = payload.get("checks")
    if status != 200 or not isinstance(checks, dict):
        raise RuntimeError(f"Readiness endpoint was HTTP {status} with an invalid response.")
    for name, check in checks.items():
        if not isinstance(check, dict):
            print(f"CHECK {name}: invalid result")
            continue
        detail = check.get("version") or check.get("reason") or (
            "available" if check.get("ready") else "not ready"
        )
        print(f"{'OK    ' if check.get('ready') else 'CHECK '} {name}: {detail}")
    print(f"Overall local media readiness: {payload.get('status', 'unknown')}")
    print(f"Public publishing policy: {payload.get('public_publishing', 'not reported')}")
    if require_ready and payload.get("ready") is not True:
        raise RuntimeError("Full readiness was required but one or more local prerequisite checks failed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("health", "dashboard", "wait", "readiness"))
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "health":
            check_health()
        elif args.command == "dashboard":
            check_dashboard()
        elif args.command == "wait":
            wait_for_app()
        else:
            check_readiness(require_ready=args.require_ready)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
