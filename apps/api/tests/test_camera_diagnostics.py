from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("camera_diagnose", ROOT / "scripts" / "camera_diagnose.py")
camera_diagnose = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(camera_diagnose)


def test_windows_platform_description_does_not_query_wmi(monkeypatch):
    monkeypatch.setattr(camera_diagnose.sys, "platform", "win32")
    monkeypatch.setattr(camera_diagnose.sys, "getwindowsversion", lambda: SimpleNamespace(
        major=10, minor=0, build=26200), raising=False)
    monkeypatch.setattr(camera_diagnose.platform, "platform", lambda: pytest.fail("unbounded WMI lookup"))
    assert camera_diagnose.platform_description() == "Windows NT 10.0.26200"


def test_platform_version_failure_is_reported_without_wmi_fallback(monkeypatch):
    monkeypatch.setattr(camera_diagnose.sys, "platform", "win32")
    def unavailable():
        raise OSError("version unavailable")
    monkeypatch.setattr(camera_diagnose.sys, "getwindowsversion", unavailable, raising=False)
    monkeypatch.setattr(camera_diagnose.platform, "platform", lambda: pytest.fail("unbounded WMI fallback"))
    assert camera_diagnose.platform_description() == "Windows (version unavailable)"


class _Capture:
    def __init__(self):
        self.reads = 0
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        self.reads += 1
        return True, np.full((48, 64, 3), self.reads, dtype=np.uint8)

    def set(self, *_args):
        return True

    def get(self, prop):
        return 30 if prop == camera_diagnose.cv2.CAP_PROP_FPS else 0

    def getBackendName(self):
        return "TEST"

    def release(self):
        self.released = True


def test_diagnostic_probe_reads_at_least_30_real_frames_and_releases(monkeypatch, tmp_path):
    capture = _Capture()
    monkeypatch.setattr(camera_diagnose.cv2, "VideoCapture", lambda *_args: capture)
    screenshot = tmp_path / "真实摄像头.jpg"
    result = camera_diagnose.probe_one(0, "ANY", 30, screenshot)
    assert result["status_code"] == "READY"
    assert result["frame_read_success"] is True
    assert result["successful_frames"] == 30
    assert result["width"] == 64 and result["height"] == 48
    assert capture.reads >= 30
    assert capture.released is True
    assert result["released"] is True
    assert screenshot.is_file() and screenshot.stat().st_size > 100


def test_diagnostic_summary_does_not_claim_busy_when_device_opened_without_frames():
    summary = camera_diagnose.summarize(
        [{"device_index": 0, "opened": True, "frame_read_success": False}],
        [{"name": "Camera"}],
        {"status": "ALLOWED"},
    )
    assert summary["status_code"] == "FRAME_READ_FAILED"
    assert "BUSY" not in summary["possible_causes"]


def test_diagnostic_summary_separates_permission_and_no_device():
    denied = camera_diagnose.summarize([], [{"name": "Camera"}], {"status": "DENIED"})
    missing = camera_diagnose.summarize([], [], {"status": "ALLOWED"})
    assert denied["status_code"] == "PERMISSION_DENIED"
    assert missing["status_code"] == "NO_DEVICE"


def test_diagnostic_index_parser_rejects_huge_or_out_of_bounds_ranges():
    for value in ('0-999999999', '-1', '65', '0,65'):
        with pytest.raises(ValueError):
            camera_diagnose.parse_indices(value)


@pytest.mark.parametrize('cancel', [True, False])
def test_probe_cancel_and_timeout_release_only_its_own_real_child(monkeypatch, tmp_path, cancel):
    """Real OS child lifecycle check; child sleeps and never opens hardware."""
    real_popen = subprocess.Popen
    children = []
    def sleeping_child(_command, **options):
        child = real_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **options)
        children.append(child)
        return child
    monkeypatch.setattr(camera_diagnose.subprocess, 'Popen', sleeping_child)
    event = threading.Event()
    timer = threading.Timer(.1, event.set) if cancel else None
    if timer:
        timer.start()
    try:
        started = time.monotonic()
        result = camera_diagnose._probe_in_child(0, 'ANY', 30, tmp_path/'unused.jpg', .3, event)
        assert time.monotonic() - started < 3
        assert result['status_code'] == ('CANCELLED' if cancel else 'ERROR')
        assert result['frame_read_success'] is False
        assert result['released'] is True
        assert len(children) == 1 and children[0].poll() is not None
    finally:
        if timer:
            timer.cancel()
        for child in children:
            if child.poll() is None:
                child.kill(); child.wait(timeout=3)


def test_quick_probe_stops_after_first_success_and_report_is_atomic(monkeypatch, tmp_path):
    monkeypatch.setattr(camera_diagnose, 'windows_camera_devices', lambda: pytest.fail('quick probe must not wait for PnP/WMI inventory'))
    monkeypatch.setattr(camera_diagnose, 'windows_permission', lambda: {'status': 'UNKNOWN'})
    calls = []
    def fixture_probe(index, backend, frames, screenshot, *_):
        calls.append((index, backend))
        return {'device_index': index, 'backend': backend, 'frame_read_success': True, 'successful_frames': frames}
    monkeypatch.setattr(camera_diagnose, '_probe_in_child', fixture_probe)
    output = tmp_path/'report.json'
    report = camera_diagnose.run_diagnostics([0], 30, output, tmp_path/'unused.jpg', quick=True)
    assert calls == [(0, 'DSHOW')]
    assert report['status'] == 'COMPLETED'
    assert report['windows_device_query_error']
    assert output.is_file() and not output.with_suffix('.json.part').exists()


def test_quick_failed_probe_does_not_claim_missing_hardware_without_inventory(monkeypatch, tmp_path):
    monkeypatch.setattr(camera_diagnose, 'windows_camera_devices', lambda: pytest.fail('quick probe must not query PnP'))
    monkeypatch.setattr(camera_diagnose, 'windows_permission', lambda: {'status': 'UNKNOWN'})
    monkeypatch.setattr(camera_diagnose, '_probe_in_child', lambda index, backend, *_: {
        'device_index': index, 'backend': backend, 'opened': False, 'frame_read_success': False})
    report = camera_diagnose.run_diagnostics([0], 30, tmp_path/'report.json', tmp_path/'unused.jpg', quick=True)
    assert report['summary']['status_code'] == 'SOURCE_UNAVAILABLE'
    assert 'NO_DEVICE' not in report['summary']['possible_causes']


def test_overall_timeout_does_not_call_unattempted_devices_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(camera_diagnose, 'windows_camera_devices', lambda: ([], None))
    monkeypatch.setattr(camera_diagnose, 'windows_permission', lambda: {'status': 'UNKNOWN'})
    monkeypatch.setattr(camera_diagnose, '_probe_in_child', lambda *_: pytest.fail('deadline already passed'))
    report = camera_diagnose.run_diagnostics([0], 30, tmp_path/'report.json', tmp_path/'unused.jpg', overall_timeout=0)
    assert report['status'] == 'TIMED_OUT'
    assert report['results'] == [] and report['summary']['status_code'] != 'NO_DEVICE'


@pytest.mark.parametrize('arguments,expected_indices,quick', [([], [0], True), (['--profile', 'full'], list(range(11)), False), (['--indices', '2'], [2], True)])
def test_cli_defaults_to_bounded_quick_probe_and_full_is_explicit(monkeypatch, arguments, expected_indices, quick):
    calls = []
    def controller_fixture(*args, **kwargs):
        calls.append((args, kwargs))
        return {'summary': {'status_code': 'READY'}}
    monkeypatch.setattr(camera_diagnose, 'run_diagnostics', controller_fixture)
    monkeypatch.setattr(camera_diagnose, '_print_human', lambda *_: None)
    monkeypatch.setattr(sys, 'argv', ['camera_diagnose.py', *arguments])
    assert camera_diagnose.main() == 0
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == expected_indices and args[1] == 30 and args[4] == 8
    assert kwargs == {'overall_timeout': 45, 'quick': quick}


@pytest.mark.parametrize('arguments', [['--timeout', 'nan'], ['--timeout', '31'], ['--overall-timeout', '9999'], ['--frames', '600'], ['--indices', '0-99999999']])
def test_cli_limits_rejected_before_any_camera_probe(arguments):
    with pytest.raises(SystemExit) as error:
        camera_diagnose.build_parser().parse_args(arguments)
    assert error.value.code == 2
