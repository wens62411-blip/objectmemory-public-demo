"""Bounded, read-only host Wi-Fi diagnostics. Never read saved credentials."""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request

router = APIRouter()
_lock = threading.Lock()
_cached: dict | None = None
_cached_at = 0.0


def _pairs(text: str):
    for line in text.splitlines():
        match = re.match(r"^\s*([^:：]+?)\s*[:：]\s*(.*?)\s*$", line)
        if match:
            yield match[1].strip().lower(), match[2]


def _band(value: str) -> str | None:
    match = re.fullmatch(r"(2[.,]4|5|6)\s*(?:ghz|千兆赫兹)", value.lower())
    return match[1].replace(",", ".") + " GHz" if match else None


def parse_interfaces(text: str) -> list[dict]:
    """Keep only connected SSIDs; exclude MACs, GUIDs and unrelated networks."""
    records, current = [], {}
    for key, value in _pairs(text):
        if key in {"name", "名称"}:
            if current:
                records.append(current)
            current = {}
        if key in {"state", "状态"}:
            current["connected"] = value.lower() in {"connected", "已连接", "连接"}
        elif key == "ssid":
            current["ssid"] = value
        elif key in {"band", "频段", "波段"}:
            current["band"] = _band(value)
        elif key in {"channel", "信道", "频道", "通道"} and value.isdigit():
            current["channel"] = int(value)
    if current:
        records.append(current)
    return [{"ssid": row["ssid"], "band": row.get("band"), "channel": row.get("channel")}
            for row in records if row.get("connected") and row.get("ssid")]


def matching_bands(text: str, ssids: set[str]) -> dict[str, list[str]]:
    found: dict[str, set[str]] = {ssid: set() for ssid in ssids}
    selected = None
    for key, value in _pairs(text):
        if re.fullmatch(r"ssid\s+\d+", key):
            selected = value if value in ssids else None
        elif selected is not None and key in {"band", "频段", "波段"}:
            band = _band(value)
            if band:
                found[selected].add(band)
    return {ssid: sorted(bands) for ssid, bands in found.items()}


def _netsh(*args: str) -> str:
    result = subprocess.run(["netsh", "wlan", "show", *args], capture_output=True,
                            timeout=2.5, check=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if len(result.stdout) > 512 * 1024:
        raise ValueError("network_diagnostic_output_limit")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return result.stdout.decode("mbcs", errors="replace")


def read_network_status() -> dict:
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "status": "unavailable",
              "connections": [], "board_supported_bands": ["2.4 GHz"],
              "board_connectivity_verified": False,
              "message": "此检查只读取电脑的 Wi-Fi；不代表板卡已连网或后台可达。"}
    if os.name != "nt":
        result["reason"] = "当前平台未提供 Wi-Fi 频段诊断，请在热点设置中确认 2.4 GHz。"
        return result
    try:
        connections = parse_interfaces(_netsh("interfaces"))
        result.update(status="connected" if connections else "not_connected", connections=connections)
        bands = matching_bands(_netsh("networks", "mode=bssid"), {r["ssid"] for r in connections}) if connections else {}
        for row in connections:
            row["observed_same_ssid_bands"] = bands.get(row["ssid"], [])
            # A 5 GHz PC connection does not rule out a dual-band access point.
            row["observed_2_4ghz"] = row["band"] == "2.4 GHz" or "2.4 GHz" in row["observed_same_ssid_bands"]
    except (OSError, subprocess.SubprocessError, ValueError):
        result["reason"] = "未能完整读取频段；可能没有扫描权限或扫描超时，请手动确认热点设置。"
    return result


def network_status() -> dict:
    global _cached, _cached_at
    # One bounded OS probe shared by all pages. Never multiply subprocesses by subscribers.
    with _lock:
        now = time.monotonic()
        if _cached is None or now - _cached_at >= 10:
            _cached = read_network_status()
            _cached_at = time.monotonic()
        return dict(_cached)


@router.get("/api/firmware/network-status")
def device_network_status(request: Request):
    # The application's administrator middleware protects the entire firmware namespace.
    mode = request.app.state.firmware_service.runtime_mode
    if mode != "REAL":
        return {"runtime_mode": mode, "source_type": "host_network_diagnostics", "is_simulated": True,
                "status": "disabled", "connections": [], "board_connectivity_verified": False,
                "message": "仅 REAL 模式读取电脑网络信息。"}
    return {**network_status(), "runtime_mode": mode,
            "source_type": "host_network_diagnostics", "is_simulated": False}
