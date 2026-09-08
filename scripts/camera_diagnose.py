from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "diagnostics" / "camera-report.json"
DEFAULT_SCREENSHOT = PROJECT_ROOT / "data" / "diagnostics" / "camera-probe.jpg"
BACKENDS = {
    "DSHOW": getattr(cv2, "CAP_DSHOW", 700),
    "MSMF": getattr(cv2, "CAP_MSMF", 1400),
    "ANY": getattr(cv2, "CAP_ANY", 0),
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return value


def platform_description() -> str:
    """Describe the local OS without a potentially unbounded Windows WMI call."""
    if sys.platform == "win32":
        try:
            version = sys.getwindowsversion()
            # NT version numbers are diagnostic facts, not Windows marketing names.
            return f"Windows NT {version.major}.{version.minor}.{version.build}"
        except (AttributeError, OSError):
            return "Windows (version unavailable)"
    try:
        version = os.uname()
        return f"{version.sysname} {version.release} {version.machine}"
    except (AttributeError, OSError):
        return sys.platform


def parse_indices(value: str) -> list[int]:
    values: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if not 0 <= start <= 64 or not 0 <= end <= 64:
                raise ValueError("摄像头索引必须为0到64，不能使用超大范围")
            values.update(range(min(start, end), max(start, end) + 1))
        else:
            value = int(part)
            if not 0 <= value <= 64:
                raise ValueError("摄像头索引必须为0到64")
            values.add(value)
    result = sorted(value for value in values if 0 <= value <= 64)
    if not result:
        raise ValueError("至少提供一个0到64之间的摄像头索引")
    return result


def opencv_backends() -> list[dict[str, Any]]:
    registry = getattr(cv2, "videoio_registry", None)
    if registry is None:
        return []
    getter = getattr(registry, "getCameraBackends", None) or getattr(registry, "getBackends", None)
    if getter is None:
        return []
    values = []
    for backend_id in getter():
        try:
            name = registry.getBackendName(backend_id)
        except Exception:
            name = f"BACKEND_{backend_id}"
        values.append({"id": int(backend_id), "name": str(name)})
    return values


def windows_permission() -> dict[str, Any]:
    if os.name != "nt":
        return {"status": "NOT_WINDOWS", "desktop_apps_allowed": None}
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _kind = winreg.QueryValueEx(key, "Value")
        allowed = str(value).lower() != "deny"
        return {"status": "ALLOWED" if allowed else "DENIED", "desktop_apps_allowed": allowed}
    except OSError as exc:
        return {"status": "UNKNOWN", "desktop_apps_allowed": None, "detail": str(exc)}


def windows_camera_devices() -> tuple[list[dict[str, Any]], str | None]:
    """Read Plug-and-Play camera inventory without changing device state."""
    if os.name != "nt":
        return [], "当前系统不是Windows，未执行PnP设备查询。"
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        return [], "找不到PowerShell，无法读取Windows摄像头设备清单。"
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$items=@(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Class -in @('Camera','Image') } | "
        "Select-Object FriendlyName,InstanceId,Class,Status);"
        "$items | ConvertTo-Json -Depth 3 -Compress"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            return [], (completed.stderr or "Windows设备查询失败").strip()
        text = completed.stdout.strip()
        if not text:
            return [], None
        payload = json.loads(text)
        rows = payload if isinstance(payload, list) else [payload]
        return [
            {
                "name": row.get("FriendlyName") or "未命名摄像头",
                "instance_id": row.get("InstanceId"),
                "class": row.get("Class"),
                "status": row.get("Status"),
            }
            for row in rows
            if isinstance(row, dict)
        ], None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return [], str(exc)


def probe_one(device_index: int, backend_name: str, frame_target: int, screenshot: Path | None = None) -> dict[str, Any]:
    """Open and release exactly one index/backend combination."""
    backend_id = BACKENDS[backend_name]
    capture: cv2.VideoCapture | None = None
    opened_at = time.perf_counter()
    result: dict[str, Any] = {
        "device_index": device_index,
        "backend": backend_name,
        "backend_id": backend_id,
        "opened": False,
        "frame_read_success": False,
        "first_frame_ms": None,
        "successful_frames": 0,
        "failed_frames": 0,
        "width": 0,
        "height": 0,
        "actual_fps": 0.0,
        "reported_fps": 0.0,
        "status_code": "ERROR",
        "error": None,
    }
    first_frame_at: float | None = None
    last_frame_at: float | None = None
    last_frame = None
    try:
        capture = cv2.VideoCapture(device_index, backend_id) if backend_id != cv2.CAP_ANY else cv2.VideoCapture(device_index)
        result["opened"] = bool(capture.isOpened())
        result["open_ms"] = round((time.perf_counter() - opened_at) * 1000, 1)
        if not result["opened"]:
            result["status_code"] = "NO_DEVICE_OR_BUSY"
            result["error"] = "OpenCV无法打开该索引和后端；可能是索引不存在、设备占用或驱动异常。"
            return result
        try:
            result["actual_backend"] = capture.getBackendName()
        except Exception:
            result["actual_backend"] = backend_name
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        deadline = time.monotonic() + max(8.0, min(30.0, frame_target / 3.0))
        max_attempts = max(frame_target * 4, frame_target + 30)
        for _attempt in range(max_attempts):
            if result["successful_frames"] >= frame_target or time.monotonic() >= deadline:
                break
            ok, frame = capture.read()
            current = time.perf_counter()
            if ok and frame is not None and frame.size:
                if first_frame_at is None:
                    first_frame_at = current
                    result["first_frame_ms"] = round((current - opened_at) * 1000, 1)
                last_frame_at = current
                last_frame = frame
                result["successful_frames"] += 1
                result["height"], result["width"] = map(int, frame.shape[:2])
            else:
                result["failed_frames"] += 1
                time.sleep(0.03)
        result["reported_fps"] = round(float(capture.get(cv2.CAP_PROP_FPS) or 0), 3)
        if result["successful_frames"] >= frame_target and first_frame_at is not None:
            duration = max(0.000001, (last_frame_at or first_frame_at) - first_frame_at)
            result["actual_fps"] = round(max(0, result["successful_frames"] - 1) / duration, 3)
            result["frame_read_success"] = True
            result["status_code"] = "READY"
            if screenshot is not None and last_frame is not None:
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                encoded, payload = cv2.imencode(".jpg", last_frame)
                if encoded:
                    screenshot.write_bytes(payload.tobytes())
                else:
                    result["screenshot_error"] = "真实帧读取成功，但JPEG截图写入失败。"
        else:
            result["status_code"] = "FRAME_READ_FAILED"
            result["error"] = f"设备已打开，但只读取到 {result['successful_frames']}/{frame_target} 帧。"
        return result
    except Exception as exc:
        result["status_code"] = "ERROR"
        result["error"] = f"OpenCV调用异常：{exc}"
        return result
    finally:
        if capture is not None:
            capture.release()
        result["released"] = True


def _probe_in_child(index: int, backend: str, frames: int, screenshot: Path, timeout: float, cancel_event: threading.Event | None = None) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--probe-child",
        "--device-index",
        str(index),
        "--backend",
        backend,
        "--frames",
        str(frames),
        "--screenshot",
        str(screenshot),
    ]
    started = time.perf_counter()
    try:
        child = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + timeout
        while True:
            cancelled = bool(cancel_event and cancel_event.is_set())
            if cancelled or time.monotonic() >= deadline:
                # This Popen handle belongs only to this diagnostic invocation;
                # never enumerate, signal or terminate another camera process.
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=3)
                if not cancelled:
                    raise subprocess.TimeoutExpired(command, timeout)
                return {"device_index": index, "backend": backend, "backend_id": BACKENDS[backend], "opened": False, "frame_read_success": False, "status_code": "CANCELLED", "error": "本次探测已取消；自己的探测子进程已退出。", "released": True}
            try:
                stdout, stderr = child.communicate(timeout=min(.2, max(.01, deadline-time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                continue
        if child.returncode != 0:
            return {
                "device_index": index,
                "backend": backend,
                "backend_id": BACKENDS[backend],
                "opened": False,
                "frame_read_success": False,
                "status_code": "ERROR",
                "error": (stderr or stdout or "诊断子进程失败").strip()[-2000:],
                "released": True,
            }
        lines = [line for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise ValueError("诊断子进程没有返回结果")
        result = json.loads(lines[-1])
        stderr = stderr.strip()
        if stderr:
            result["opencv_log"] = stderr[-2000:]
        return result
    except subprocess.TimeoutExpired:
        return {
            "device_index": index,
            "backend": backend,
            "backend_id": BACKENDS[backend],
            "opened": False,
            "frame_read_success": False,
            "status_code": "ERROR",
            "error": f"超过 {timeout:g} 秒仍未完成，后端可能卡住。诊断进程已终止并由操作系统释放设备。",
            "released": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except (OSError, ValueError) as exc:
        return {
            "device_index": index,
            "backend": backend,
            "backend_id": BACKENDS[backend],
            "opened": False,
            "frame_read_success": False,
            "status_code": "ERROR",
            "error": f"无法执行诊断子进程：{exc}",
            "released": True,
        }


def summarize(results: list[dict[str, Any]], devices: list[dict[str, Any]], permission: dict[str, Any]) -> dict[str, Any]:
    successes = [row for row in results if row.get("frame_read_success")]
    opened_without_frames = [row for row in results if row.get("opened") and not row.get("frame_read_success")]
    if successes:
        recommended = max(successes, key=lambda row: (row.get("successful_frames", 0), row.get("actual_fps", 0)))
        status = "READY"
        message = f"成功读取摄像头索引 {recommended['device_index']}，推荐 {recommended['backend']} 后端。"
        candidates: list[str] = []
    elif permission.get("status") == "DENIED":
        recommended = None
        status = "PERMISSION_DENIED"
        message = "Windows摄像头权限已关闭，请允许桌面应用访问摄像头。"
        candidates = ["PERMISSION_DENIED"]
    elif not devices:
        recommended = None
        status = "NO_DEVICE"
        message = "Windows没有报告可用摄像头设备。"
        candidates = ["NO_DEVICE"]
    elif opened_without_frames:
        recommended = None
        status = "FRAME_READ_FAILED"
        message = "摄像头可以打开，但无法连续读取30帧；请检查驱动、权限和设备连接。"
        candidates = ["FRAME_READ_FAILED", "DRIVER_ERROR"]
    else:
        recommended = None
        status = "BUSY_OR_DRIVER_ERROR"
        message = "Windows检测到摄像头，但OpenCV无法打开。可能被其他程序占用、驱动异常，或设备索引与PnP顺序不一致。"
        candidates = ["BUSY", "DRIVER_ERROR", "INDEX_MISMATCH"]
    return {
        "status_code": status,
        "message": message,
        "tested_combinations": len(results),
        "successful_combinations": len(successes),
        "successful_indices": sorted({int(row["device_index"]) for row in successes}),
        "recommended": recommended,
        "possible_causes": candidates,
    }


def run_diagnostics(indices: list[int], frames: int, output: Path, screenshot: Path, timeout: float = 20.0, *, cancel_event: threading.Event | None = None, progress=None, overall_timeout: float = 120.0, quick: bool = False) -> dict[str, Any]:
    output = output.resolve()
    screenshot = screenshot.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + overall_timeout
    # Quick mode answers whether frames can actually be read. PnP inventory is
    # optional context and can consume 12 seconds when Windows WMI is stalled.
    # Keep it in explicit full diagnosis, not ahead of the default frame probe.
    devices, device_query_error = ([], "快速诊断未查询 PnP 清单；需要设备清单时运行完整诊断。") if quick else windows_camera_devices()
    permission = windows_permission()
    results: list[dict[str, Any]] = []
    screenshot_saved = False
    status = "COMPLETED"
    with tempfile.TemporaryDirectory(prefix="object-memory-camera-") as temp_name:
        temp = Path(temp_name)
        for index in indices:
            for backend in ("DSHOW", "MSMF", "ANY"):
                if cancel_event is not None and cancel_event.is_set():
                    status = "CANCELLED"
                    break
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    status = "TIMED_OUT"
                    break
                if progress:
                    progress({"completed": len(results), "current_index": index, "current_backend": backend, "message": f"正在探测索引 {index} / {backend}；单项最多 {min(timeout, remaining):g} 秒。"})
                candidate = temp / f"camera-{index}-{backend}.jpg"
                result = _probe_in_child(index, backend, frames, candidate, min(timeout, remaining), cancel_event)
                results.append(result)
                if not screenshot_saved and result.get("frame_read_success") and candidate.is_file():
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(candidate, screenshot)
                    screenshot_saved = True
                if progress:
                    progress({"completed": len(results), "latest_result": result})
                if quick and result.get("frame_read_success"):
                    break
            if status != "COMPLETED":
                break
    if cancel_event is not None and cancel_event.is_set():
        status = "CANCELLED"
    elif time.monotonic() >= deadline:
        status = "TIMED_OUT"
    summary = summarize(results, devices, permission)
    if device_query_error and summary['status_code'] == 'NO_DEVICE':
        summary.update({'status_code': 'SOURCE_UNAVAILABLE',
                        'message': '当前探测未读取到画面，且设备清单未确认；不能据此断言没有摄像头。',
                        'possible_causes': ['INDEX_MISMATCH', 'BUSY', 'DRIVER_ERROR', 'DEVICE_INVENTORY_UNVERIFIED']})
    if status != "COMPLETED":
        summary.update({"status_code": status, "message": "诊断已取消，已完成的结果仅供参考。" if status == "CANCELLED" else "诊断达到总时限并已停止；未探测的编号不能视为不存在设备。"})
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "opencv_version": cv2.__version__,
        "platform": platform_description(),
        "available_backends": opencv_backends(),
        "windows_devices": devices,
        "windows_device_query_error": device_query_error,
        "permission": permission,
        "probe_settings": {"indices": indices, "frames_per_backend": frames, "timeout_seconds": timeout, "overall_timeout_seconds": overall_timeout, "profile": "quick" if quick else "full"},
        "results": results,
        "summary": summary,
        "screenshot_path": str(screenshot) if screenshot_saved else None,
    }
    pending = output.with_suffix(output.suffix + ".part")
    pending.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_safe), encoding="utf-8")
    os.replace(pending, output)
    return report


def _print_human(report: dict[str, Any], output: Path) -> None:
    print("ObjectMemory 摄像头诊断")
    print(f"Python: {report['python_version']}")
    print(f"OpenCV: {report['opencv_version']}")
    print("可用采集后端: " + ", ".join(item["name"] for item in report["available_backends"]))
    devices = report["windows_devices"]
    print("Windows摄像头: " + ("；".join(item["name"] for item in devices) if devices else "未查询到"))
    for item in report["results"]:
        resolution = f"{item.get('width', 0)}x{item.get('height', 0)}"
        print(
            f"[{item['status_code']}] index={item['device_index']} backend={item['backend']} "
            f"frames={item.get('successful_frames', 0)}/{report['probe_settings']['frames_per_backend']} "
            f"resolution={resolution} fps={item.get('actual_fps', 0)} error={item.get('error') or '-'}"
        )
    print(report["summary"]["message"])
    print(f"JSON报告: {output}")
    if report.get("screenshot_path"):
        print(f"真实摄像头截图: {report['screenshot_path']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ObjectMemory Windows摄像头诊断")
    def bounded(cast, minimum, maximum):
        def parse(value):
            try: number = cast(value)
            except (TypeError, ValueError): raise argparse.ArgumentTypeError("请输入有效数字")
            if not minimum <= number <= maximum:
                raise argparse.ArgumentTypeError(f"必须为 {minimum} 到 {maximum} 之间的数值")
            return number
        return parse
    def indices(value):
        if len(value)>80: raise argparse.ArgumentTypeError("索引表达式不得超过80个字符")
        try: parse_indices(value)
        except ValueError as exc: raise argparse.ArgumentTypeError(str(exc))
        return value
    parser.add_argument("--indices", type=indices, default=None, help="默认只检查0；显式 --profile full 时默认0-10，也可指定0,2")
    parser.add_argument("--profile", choices=("quick", "full"), default="quick", help="quick找到可用后端即停止；full测试指定索引的全部后端")
    parser.add_argument("--frames", type=bounded(int, 30, 120), default=30, help="每个索引/后端读取的成功帧数，30-120")
    parser.add_argument("--timeout", type=bounded(float, 3, 30), default=8.0, help="每组探测超时，默认8秒（3-30）")
    parser.add_argument("--overall-timeout", type=bounded(float, 5, 120), default=45.0, help="整轮采集时限，默认45秒（5-120）")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--screenshot", type=Path, default=DEFAULT_SCREENSHOT)
    parser.add_argument("--probe-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--device-index", type=bounded(int, 0, 64), default=0, help=argparse.SUPPRESS)
    parser.add_argument("--backend", choices=tuple(BACKENDS), default="ANY", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    frames = args.frames
    if args.probe_child:
        result = probe_one(args.device_index, args.backend, frames, args.screenshot)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    indices = parse_indices(args.indices or ("0-10" if args.profile == "full" else "0"))
    report = run_diagnostics(indices, frames, args.output, args.screenshot, args.timeout, overall_timeout=args.overall_timeout, quick=args.profile == "quick")
    _print_human(report, args.output.resolve())
    return 0 if report["summary"]["status_code"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
