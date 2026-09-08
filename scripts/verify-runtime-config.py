"""Verify that a running production entry uses one origin for page, API and WS."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8018")
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        parser.error("--url must be an explicit loopback HTTP URL with a port")
    base = args.url.rstrip("/")
    with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
        health_response = client.get("/api/health")
        session_response = client.get("/api/session")
        runtime_response = client.get("/api/runtime-config")
        page_response = client.get("/")
        for response in (health_response, session_response, runtime_response, page_response):
            response.raise_for_status()
        runtime = runtime_response.json()
        expected_ws = base.replace("http://", "ws://", 1) + "/ws"
        checks = {
            "health_ok": health_response.json().get("status") == "ok",
            "local_session_authenticated": session_response.json().get("authenticated") is True,
            "frontend_url_matches": runtime.get("frontend_url") == base,
            "api_url_matches": runtime.get("api_base_url") == base + "/api",
            "websocket_url_matches": runtime.get("websocket_base_url") == expected_ws,
            "api_port_matches": runtime.get("api_port") == parsed.port,
            "production_page_present": 'id="root"' in page_response.text,
        }
    report = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base,
        "runtime": runtime,
        "checks": checks,
        "passed": all(checks.values()),
    }
    output = ROOT / "data/verification/runtime-config-smoke.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
