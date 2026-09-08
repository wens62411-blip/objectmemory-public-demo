from pathlib import Path
import subprocess
import sys

import cv2
import numpy as np

from services.vision.capture import FramePacket
from services.vision.engine import VisionEngine


ROOT = Path(__file__).resolve().parents[3]


def test_failed_start_closes_native_hand_resources_and_child_process_exits(tmp_path):
    # A child process catches leaked MediaPipe native worker threads that might
    # otherwise let pytest reach 100% while preventing process termination.
    code = '''
from pathlib import Path
import sys
root=Path(sys.argv[1]); sys.path.insert(0,str(root))
from services.vision.engine import VisionEngine
engine=VisionEngine(
    {"id":"bad","source_type":"video","source":str(root/"does-not-exist.avi")},
    [],[],{"data_dir":sys.argv[2],"show_hands":True},lambda *_args:None)
was_available=engine.hands.available
assert engine.start() is False
assert engine.hands._hands is None
assert engine.hands.available is False
engine.stop()
engine.stop()
print("failed-start-cleanup-ok",was_available)
'''
    result = subprocess.run([sys.executable, "-c", code, str(ROOT), str(tmp_path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "failed-start-cleanup-ok" in result.stdout


def test_simulated_device_health_and_overlay_are_explicit_test_playback(tmp_path, monkeypatch):
    engine = VisionEngine(
        {"id": "sim", "source_type": "video", "source": str(ROOT / "demo/sample-videos/object-memory-demo.avi"), "config": {"simulated": True}},
        [], [], {"data_dir": str(tmp_path), "show_hands": False}, lambda *_args: None,
    )
    texts = []
    original = cv2.putText

    def record_text(image, text, *args, **kwargs):
        texts.append(text)
        return original(image, text, *args, **kwargs)

    monkeypatch.setattr(cv2, "putText", record_text)
    frame = np.full((240, 480, 3), 240, dtype=np.uint8)
    engine._process(FramePacket(frame, 1.0, 1.0, 1))
    assert engine.health()["source_label"] == "模拟设备 · 测试回放"
    assert engine.health()["simulated"] is True
    assert any("SIMULATED DEVICE - TEST PLAYBACK" in text for text in texts)
    engine.stop()

