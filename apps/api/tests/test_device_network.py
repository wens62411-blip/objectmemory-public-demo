"""Host-network parsing is fixture tested; it cannot certify board connectivity."""
import subprocess

from fastapi.testclient import TestClient

from apps.api.app import device_network as network
from apps.api.app.main import create_app


def test_connected_only_and_no_sensitive_adapter_identifiers():
    text = """Name : WLAN
State : connected
SSID : My phone
AP BSSID : private-mac
Band : 5 GHz
Channel : 149
Name : WLAN2
State : disconnected
SSID : Other
Band : 2.4 GHz"""
    assert network.parse_interfaces(text) == [{"ssid": "My phone", "band": "5 GHz", "channel": 149}]


def test_chinese_and_no_ambiguous_channel_inference():
    assert network.parse_interfaces("名称 : WLAN\n状态 : 已连接\nSSID : 测试热点\n通道 : 1") == [
        {"ssid": "测试热点", "band": None, "channel": 1}]
    assert network._band("2.4 GHz") == "2.4 GHz"
    assert network._band("invented") is None


def test_same_ssid_dual_band_is_not_rejected_and_neighbours_excluded():
    scan = "SSID 1 : Mine\nBand : 5 GHz\nBand : 2.4 GHz\nSSID 2 : Neighbour\nBand : 6 GHz"
    assert network.matching_bands(scan, {"Mine"}) == {"Mine": ["2.4 GHz", "5 GHz"]}


def test_probe_timeout_does_not_disclose_raw_error(monkeypatch):
    def fail(*args):
        raise subprocess.TimeoutExpired("secret/raw/command", 2.5)
    monkeypatch.setattr(network.os, "name", "nt")
    monkeypatch.setattr(network, "_netsh", fail)
    value = network.read_network_status()
    assert value["status"] == "unavailable"
    assert "secret" not in str(value)
    assert value["board_connectivity_verified"] is False


def test_probe_cache_does_not_repeat_os_work(monkeypatch):
    calls = []
    monkeypatch.setattr(network, "_cached", None)
    monkeypatch.setattr(network, "read_network_status", lambda: calls.append(1) or {"status": "test"})
    assert network.network_status() == network.network_status()
    assert calls == [1]


def test_endpoint_admin_only_and_test_mode_never_reads_os(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "network_status", lambda: (_ for _ in ()).throw(AssertionError("OS access")))
    with TestClient(create_app(tmp_path, testing=True, runtime_mode="TEST")) as client:
        assert client.get("/api/firmware/network-status").status_code == 401
        client.get("/api/session")
        response = client.get("/api/firmware/network-status")
        assert response.status_code == 200
        assert response.json()["status"] == "disabled"
        assert response.json()["board_connectivity_verified"] is False
