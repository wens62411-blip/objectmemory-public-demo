"""Preparation safety units; network/download calls here are explicit test doubles."""
import hashlib
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[3] / "scripts" / "prepare-appearance-model.py"
    spec = importlib.util.spec_from_file_location("appearance_preparation_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    payload = b"bounded-unit-model-not-real-weights"
    manifest = {**module.MANIFEST, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    monkeypatch.setattr(module, "MANIFEST", manifest)
    return module, payload


def test_prepare_download_atomic_hash_checked_and_no_second_download(preparation, monkeypatch):
    module, payload = preparation
    destination = module.ROOT / "data" / "models" / "model.onnx"
    calls = []
    def fetch(url, path, maximum_bytes, **kwargs):
        assert not destination.exists()
        assert "8b1f705a3a7f6f062f6bdd21986c1583d3ef105d" in url
        assert maximum_bytes == len(payload)
        calls.append(url)
        path.write_bytes(payload)
    monkeypatch.setattr(module, "powershell_fetch", fetch)
    assert module.download_model(destination, "powershell")
    assert destination.read_bytes() == payload
    assert not list(destination.parent.glob("*.partial"))
    assert not module.download_model(destination, "powershell")
    assert len(calls) == 1


def test_prepare_preserves_existing_unrecognized_model(preparation, monkeypatch):
    module, _ = preparation
    destination = module.ROOT / "data" / "models" / "user-model.onnx"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"unknown user model")
    monkeypatch.setattr(module, "powershell_fetch", lambda *a, **k: pytest.fail("must not download"))
    with pytest.raises(ValueError, match="preserved"):
        module.download_model(destination, "powershell")
    assert destination.read_bytes() == b"unknown user model"


def test_prepare_rejects_bad_hash_without_publishing_partial(preparation, monkeypatch):
    module, payload = preparation
    destination = module.ROOT / "data" / "models" / "model.onnx"
    monkeypatch.setattr(module, "powershell_fetch", lambda url, path, *a, **k: path.write_bytes(b"x" * len(payload)))
    with pytest.raises(ValueError, match="SHA-256"):
        module.download_model(destination, "powershell")
    assert not destination.exists()
    assert not list(destination.parent.glob("*.partial"))


def test_prepare_rejects_outside_data_models(preparation, monkeypatch):
    module, _ = preparation
    monkeypatch.setattr(module, "powershell_fetch", lambda *a, **k: pytest.fail("must not download"))
    with pytest.raises(ValueError, match="below project"):
        module.download_model(module.ROOT / "elsewhere.onnx", "powershell")


def test_status_is_atomic_and_describes_failure_without_false_ready(preparation):
    module, _ = preparation
    module.publish_status("failed", 0, error="controlled_failure")
    value = module.json.loads((module.ROOT / "data/models/appearance-status.json").read_text())
    assert value["status"] == "failed" and value["error"] == "controlled_failure"
    assert value["downloaded_bytes"] == 0 and value["total_bytes"] > 0
    assert not list((module.ROOT / "data/models").glob("*.partial"))


def test_prepare_status_uses_new_application_preprocessing_version(preparation):
    module, _ = preparation
    expected = '8b1f705a3a7f6f062f6bdd21986c1583d3ef105d:onnx-fp32-cls-rgb-bicubic-fit224-blackpad-l2-v2'
    assert module.MANIFEST['model_version'] == expected
    module.publish_status('loading', module.MANIFEST['bytes'])
    value = module.json.loads((module.ROOT / 'data/models/appearance-status.json').read_text())
    assert value['model_version'] == expected
    assert value['status'] == 'loading', 'A preprocessing version is not a completed runtime load'
