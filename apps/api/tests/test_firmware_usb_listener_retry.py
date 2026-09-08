"""Real isolated SQLite with controlled listeners/ports; never opens hardware."""
from fastapi import HTTPException
import pytest

from apps.api.tests.test_firmware_usb_recovery import fw


@pytest.mark.parametrize("detail", ["listener_not_started", "local_address_temporarily_unavailable"])
def test_readonly_listener_failure_does_not_consume_bound_usb_connection(fw, monkeypatch, detail):
    service, binding, jobs = fw
    controller = service.auto_usb
    controller.credentials = {"ssid": "isolated-test-network", "password": "isolated-test-secret"}

    def unavailable(_url):
        raise HTTPException(409, detail)

    monkeypatch.setattr(service, "_verify_backend_listener", unavailable)
    for _ in range(2):
        with pytest.raises(HTTPException):
            controller.tick()
        assert controller.seen is False
        assert controller.credentials is not None
        assert controller._receipt(binding) is None
        assert jobs == []

    monkeypatch.setattr(service, "_verify_backend_listener", lambda url: url)
    controller.tick()
    assert controller.seen is True
    assert len(jobs) == 1 and jobs[0][0] == "usb_auto"
    assert jobs[0][2][2]["ssid"] == "isolated-test-network"
    assert controller._receipt(binding)["status"] == "reserved"
    controller.tick()
    assert len(jobs) == 1  # Ready transition resumes once, never duplicate writes.

