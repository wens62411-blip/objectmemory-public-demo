"""Opt-in, one bound USB board. No unbound port is ever opened by a timer."""
from __future__ import annotations

import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4

from fastapi import HTTPException

from .firmware import (AutoUsbBindingRequest, AutoUsbRecoveryRequest, iso_now, parse_serial_protocol_line, MAX_SERIAL_LINE,
                       redact_firmware_log, DEFAULT_BOARD_MODEL, firmware_board)


def usb_helper(root):
    spec = importlib.util.spec_from_file_location("om_usb_contract", Path(root) / "scripts/firmware-usb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixed_bundle(helper, root, expected=None, board_model=DEFAULT_BOARD_MODEL):
    firmware_board(board_model)
    if board_model == DEFAULT_BOARD_MODEL:
        return helper.bundle(root, expected)
    return helper.bundle(root, expected, board_model=board_model)


class AutoUsbService:
    POLL_SECONDS = 2.0

    def __init__(self, firmware):
        self.fw = firmware
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker = None
        self.seen = False
        self.credentials = None  # No Wi-Fi password/SSID/pairing code on disk.
        self.notice = "默认关闭；只在管理员绑定板卡后检查该 USB 位置。"
        with self.fw.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS firmware_usb_binding(slot INTEGER PRIMARY KEY CHECK(slot=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS firmware_usb_receipts(
                  mac TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                  flashed_at TEXT, linked_at TEXT, job_id TEXT, error TEXT,
                  PRIMARY KEY(mac,manifest_sha256));
                CREATE TABLE IF NOT EXISTS firmware_usb_recoveries(
                  id TEXT PRIMARY KEY, mac TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
                  board_model TEXT NOT NULL, action TEXT NOT NULL, authorized_at TEXT NOT NULL,
                  binding_authorized_at TEXT NOT NULL, previous_receipt TEXT NOT NULL,
                  status TEXT NOT NULL, job_id TEXT);
            """)
            receipt_columns = {row[1] for row in connection.execute("PRAGMA table_info(firmware_usb_receipts)")}
            if "board_model" not in receipt_columns:
                connection.execute("ALTER TABLE firmware_usb_receipts ADD COLUMN board_model TEXT NOT NULL DEFAULT 'ai_thinker_esp32cam'")
            connection.execute("UPDATE firmware_usb_receipts SET status='interrupted',error='服务重启；禁止自动重试擦写' WHERE status='reserved'")
            connection.execute("UPDATE firmware_usb_recoveries SET status='interrupted' WHERE status IN ('reserved','running')")
        binding = self._binding()
        if self.fw.runtime_mode == "REAL" and binding and binding.get("enabled"):
            self._start_listener()

    def _binding(self):
        with self.fw.connect() as connection:
            row = connection.execute("SELECT data FROM firmware_usb_binding WHERE slot=1").fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, binding):
        with self.fw.connect() as connection:
            connection.execute("INSERT INTO firmware_usb_binding(slot,data) VALUES(1,?) ON CONFLICT(slot) DO UPDATE SET data=excluded.data",
                               (json.dumps(binding, ensure_ascii=False),))

    def _receipt(self, binding):
        board_model = binding.get("board_model", DEFAULT_BOARD_MODEL)
        firmware_board(board_model)
        with self.fw.connect() as connection:
            row = connection.execute("SELECT * FROM firmware_usb_receipts WHERE mac=? AND manifest_sha256=? AND board_model=?",
                                     (binding["mac_address"], binding["manifest_sha256"], board_model)).fetchone()
        return dict(row) if row else None

    def status(self):
        binding = self._binding()
        receipt = self._receipt(binding) if binding else None
        device = self.fw.get_device(binding["device_id"]) if binding else None
        verified = bool(self.fw.runtime_mode == "REAL" and receipt and receipt.get("status") == "linked"
                        and device and device.get("hardware_verified") and not device.get("simulated")
                        and device.get("verification_method") == "usb_serial_omready" and device.get("serial_verified_at")
                        and not device.get("token_revoked_at") and device.get("mac_address", "").lower() == binding["mac_address"])
        return {"runtime_mode": self.fw.runtime_mode, "source_type": "esp32_real" if verified else "esp32_unverified",
                "is_simulated": self.fw.runtime_mode != "REAL", "physical_source_verified": verified,
                "target_board_type": binding.get("board_model", DEFAULT_BOARD_MODEL) if binding else DEFAULT_BOARD_MODEL,
                "enabled": bool(binding and binding.get("enabled")), "binding": binding,
                "receipt": receipt,
                "recovery": self._recovery_status(binding, receipt),
                "listener_running": bool(self.worker and self.worker.is_alive()), "message": self.notice,
                "credentials_retained": self.credentials is not None,
                "physical_flash_performed": bool(self.fw.runtime_mode == "REAL" and receipt and receipt.get("flashed_at")),
                "max_flash_attempts_per_authorization": 1,
                "hardware_acceptance": "真实开发板刷写、启动、视频仍需连接硬件验收；绑定本身不代表刷写成功。"}

    def _recovery_status(self, binding, receipt):
        last = None
        if binding:
            with self.fw.connect() as connection:
                row = connection.execute(
                    "SELECT id,action,status,authorized_at,job_id FROM firmware_usb_recoveries "
                    "WHERE mac=? AND manifest_sha256=? AND board_model=? ORDER BY authorized_at DESC,id DESC LIMIT 1",
                    (binding["mac_address"], binding["manifest_sha256"], binding.get("board_model", DEFAULT_BOARD_MODEL)),
                ).fetchone()
                last = dict(row) if row else None
                active = bool(connection.execute("SELECT 1 FROM firmware_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone())
        else:
            active = False
        state, actions, message = "unavailable", [], "当前收据不允许恢复。"
        if not binding:
            state, message = "not_bound", "请先明确绑定目标板卡。"
        elif self.fw.runtime_mode != "REAL" or not binding.get("enabled"):
            state, message = "disabled", "恢复仅适用于 REAL 模式的已启用绑定。"
        elif active or (last and last["status"] in {"reserved", "running"}):
            state, message = "busy", "已有固件任务进行中，不会重复启动恢复。"
        elif not receipt:
            state, message = "new_install", "没有已有安装收据，请使用首次绑定安装。"
        elif receipt.get("flashed_at"):
            state, actions, message = "reprovision_available", ["reprovision"], "已有刷写成功收据；只核验同板并重新配网，不刷写。"
        elif receipt.get("status") in {"failed", "interrupted", "reserved"}:
            state, actions, message = "overwrite_authorization_required", ["retry_install"], "无法确定旧写入结果；须明确授权覆盖同板同固定版本一次。"
        return {"state": state, "allowed_actions": actions, "requires_explicit_authorization": True,
                "requires_overwrite_authorization": actions == ["retry_install"],
                "binding_authorized_at": binding.get("authorized_at") if binding else None,
                "last_attempt": last, "message": message}

    def recover(self, request: AutoUsbRecoveryRequest):
        """Administrator route only; grant one explicit, durably recorded attempt."""
        # Count validation/reservation in shutdown's lifecycle guard too.
        with self.fw._operation_guard():
            self._require_real()
            if not self.fw.build_lock.acquire(blocking=False):
                raise HTTPException(409, "已有固件操作进行中，请等待其结束。")
            try:
                with self.lock:
                    binding = self._binding()
                    if not binding or not binding.get("enabled") or self.stop.is_set():
                        raise HTTPException(409, "请先启用并核实目标板卡绑定。")
                    for key in ("board_model", "mac_address", "manifest_sha256"):
                        if binding.get(key) != getattr(request, key):
                            raise HTTPException(409, "恢复请求与当前板型、MAC 或固定清单不匹配；未访问 USB。")
                    if binding.get("authorized_at") != request.binding_authorized_at:
                        raise HTTPException(409, "绑定已经变化，请刷新后重新确认恢复。")
                    receipt = self._receipt(binding)
                    recovery = self._recovery_status(binding, receipt)
                    if request.action not in recovery["allowed_actions"]:
                        raise HTTPException(409, recovery["message"])
                    backend_url = request.backend_url or binding["backend_url"]
                    self.fw._verify_backend_listener(backend_url)
                    helper = usb_helper(self.fw.project_root)
                    try:
                        fixed_bundle(helper, self.fw.project_root, binding["manifest_sha256"], binding["board_model"])
                        port = helper.matching_port(self.fw.discover_ports(), binding["bridge"])
                        if port != request.port:
                            raise ValueError("选中端口已改变，请刷新并重新确认；不自动改选端口。")
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        raise HTTPException(409, "恢复前固定产物或 USB 身份检查失败：" + redact_firmware_log(error)) from error
                    previous = {key: receipt.get(key) for key in ("status", "flashed_at", "linked_at", "job_id")}
                    recovery_id = uuid4().hex
                    stamp = iso_now()
                    # Consume the displayed binding revision for every explicit
                    # recovery, including reconfiguration. A repeated click
                    # using the old snapshot cannot enqueue a second job.
                    updated_binding = {**binding, "backend_url": backend_url, "authorized_at": stamp}
                    with self.fw.connect() as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        # A recovery never deletes/recreates a successful receipt.
                        connection.execute(
                            "INSERT INTO firmware_usb_recoveries(id,mac,manifest_sha256,board_model,action,authorized_at,"
                            "binding_authorized_at,previous_receipt,status) VALUES(?,?,?,?,?,?,?,?,'reserved')",
                            (recovery_id, binding["mac_address"], binding["manifest_sha256"], binding["board_model"],
                             request.action, stamp, request.binding_authorized_at, json.dumps(previous)),
                        )
                        if request.action == "retry_install":
                            connection.execute("UPDATE firmware_usb_receipts SET status='reserved' WHERE mac=? AND manifest_sha256=? AND board_model=? AND flashed_at IS NULL",
                                               (binding["mac_address"], binding["manifest_sha256"], binding["board_model"]))
                        elif receipt["status"] not in {"flashed", "linked"}:
                            connection.execute("UPDATE firmware_usb_receipts SET status='flashed' WHERE mac=? AND manifest_sha256=? AND board_model=? AND flashed_at IS NOT NULL",
                                               (binding["mac_address"], binding["manifest_sha256"], binding["board_model"]))
                        if updated_binding != binding:
                            connection.execute("UPDATE firmware_usb_binding SET data=? WHERE slot=1", (json.dumps(updated_binding, ensure_ascii=False),))
                    self.seen = True
                    self.credentials = None
                    credentials = {"ssid": request.ssid, "password": request.password}
                    try:
                        job = self.fw._create_job("usb_recover", self._recovery_worker, recovery_id,
                                                  updated_binding, port, credentials, request.action == "reprovision")
                        with self.fw.connect() as connection:
                            connection.execute("UPDATE firmware_usb_recoveries SET job_id=? WHERE id=?", (job["id"], recovery_id))
                            connection.execute("UPDATE firmware_usb_receipts SET job_id=? WHERE mac=? AND manifest_sha256=? AND board_model=?",
                                               (job["id"], binding["mac_address"], binding["manifest_sha256"], binding["board_model"]))
                    except Exception:
                        with self.fw.connect() as connection:
                            connection.execute("UPDATE firmware_usb_recoveries SET status='interrupted' WHERE id=?", (recovery_id,))
                            if request.action == "retry_install":
                                connection.execute("UPDATE firmware_usb_receipts SET status='interrupted' WHERE mac=? AND manifest_sha256=? AND board_model=? AND status='reserved' AND flashed_at IS NULL",
                                                   (binding["mac_address"], binding["manifest_sha256"], binding["board_model"]))
                        raise
                    self.notice = "已授权一次恢复：" + ("只重新配网，不重复刷写。" if request.action == "reprovision" else "仅覆盖当前绑定板与固定版本，不自动重试。")
                    return {**job, "recovery_id": recovery_id, "recovery_action": request.action}
            finally:
                self.fw.build_lock.release()

    def _recovery_worker(self, job_id, recovery_id, binding, port, credentials, already_flashed):
        # The caller holds build_lock until reservation and job ownership are
        # committed, so even an immediately scheduled thread cannot race them.
        with self.fw.build_lock:
            with self.fw.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                recovery = connection.execute("SELECT * FROM firmware_usb_recoveries WHERE id=?", (recovery_id,)).fetchone()
                receipt = connection.execute("SELECT * FROM firmware_usb_receipts WHERE mac=? AND manifest_sha256=? AND board_model=?",
                                             (binding["mac_address"], binding["manifest_sha256"], binding["board_model"])).fetchone()
                if (not recovery or recovery["status"] != "reserved" or recovery["job_id"] != job_id
                        or not receipt or receipt["job_id"] != job_id
                        or recovery["mac"] != binding["mac_address"] or recovery["manifest_sha256"] != binding["manifest_sha256"]
                        or recovery["board_model"] != binding["board_model"]
                        or (recovery["action"] == "reprovision") != already_flashed):
                    raise RuntimeError("恢复授权已消费、已中断或不属于本任务；未访问 USB。")
                connection.execute("UPDATE firmware_usb_recoveries SET status='running' WHERE id=?", (recovery_id,))
        try:
            result = self._install_worker(job_id, binding, port, credentials, already_flashed)
        except Exception:
            with self.fw.connect() as connection:
                connection.execute("UPDATE firmware_usb_recoveries SET status='failed' WHERE id=?", (recovery_id,))
            raise
        else:
            with self.fw.connect() as connection:
                connection.execute("UPDATE firmware_usb_recoveries SET status='succeeded' WHERE id=?", (recovery_id,))
            return {**result, "recovery_id": recovery_id}
        finally:
            credentials.clear()

    def _require_real(self):
        self.fw._ensure_accepting_work()
        if self.fw.runtime_mode != "REAL":
            raise HTTPException(409, "自动 USB 接板仅允许在 REAL 模式由管理员启用。")

    def bind(self, request: AutoUsbBindingRequest):
        self._require_real()
        self.fw._verify_backend_listener(request.backend_url)
        # Validate artifact completeness before even resetting a selected board.
        helper = usb_helper(self.fw.project_root)
        try:
            fixed_bundle(helper, self.fw.project_root, board_model=request.board_model)
        except FileNotFoundError:
            # A clean source checkout intentionally contains no local binaries.
            # Build the selected fixed profile in the background job, before USB
            # is opened. Existing but invalid bundles still fail closed below.
            pass
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(409, "固定固件清单不完整或无效，请先点击“只编译固件”：" + str(error)) from error
        records = self.fw.discover_ports()
        selected = next((row for row in records if row["device"] == request.port), None)
        if not selected:
            raise HTTPException(422, "请选择当前实际连接的 USB 串口。")
        try:
            bridge = helper.bridge_identity(selected)
            helper.matching_port(records, bridge)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        with self.lock:
            with self.fw.connect() as connection:
                active = connection.execute("SELECT 1 FROM firmware_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone()
            if active:
                raise HTTPException(409, "请等待当前固件任务结束后再绑定。")
            return self.fw._create_job("usb_bind", self._bind_worker, request.model_dump(), bridge)

    def _tool(self, payload):
        firmware_board(payload.get("board_model", DEFAULT_BOARD_MODEL))
        result = subprocess.run([str(self.fw.firmware_python), "-B", str(self.fw.project_root / "scripts/firmware-usb.py")],
                                input=json.dumps(payload), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", shell=False,
                                timeout=180 if payload["operation"] == "flash" else 40,
                                cwd=str(self.fw.project_root))
        lines = result.stdout.splitlines()
        evidence = [line[6:] for line in lines if line.startswith("OMUSB:")]
        if result.returncode != 0 or len(evidence) != 1:
            raise RuntimeError("USB 工具失败；未自动重试：" + redact_firmware_log("\n".join(lines[-4:])))
        return json.loads(evidence[0])

    def _bind_worker(self, job_id, request, bridge):
        with self.fw.build_lock, self.lock:
            self.fw._verify_backend_listener(request["backend_url"])
            helper = usb_helper(self.fw.project_root)
            board_model = request["board_model"]
            board = firmware_board(board_model)
            try:
                digest, manifest, _ = fixed_bundle(helper, self.fw.project_root, board_model=board_model)
            except FileNotFoundError:
                self.fw._update_job(job_id, stage="首次安装：自动编译所选板型固件", progress=5)
                code, result = self.fw._builder_process(job_id, board_model=board_model)
                if code != 0 or result is None:
                    raise RuntimeError("自动编译失败；尚未打开串口或刷写，请检查构建日志后重试。")
                digest, manifest, _ = fixed_bundle(helper, self.fw.project_root, board_model=board_model)
            # Compilation may take minutes: recheck the current listener and
            # shutdown state before touching the previously selected bridge.
            if self.stop.is_set():
                raise RuntimeError("固件服务已关闭；没有访问 USB。")
            self.fw._verify_backend_listener(request["backend_url"])
            port = helper.matching_port(self.fw.discover_ports(), bridge)
            if port != request["port"]:
                raise RuntimeError("选中端口已变化，请重新确认板卡。")
            self.fw._update_job(job_id, stage="只读识别 ESP32 芯片与 MAC（不会刷写）", progress=20)
            identity = self._tool({"operation": "identify", "port": port, "bridge": bridge, "board_model": board_model})
            if (identity.get("chip") != board["chip"] or identity.get("physical_flash_performed") is not False
                    or identity.get("board_model", DEFAULT_BOARD_MODEL) != board_model):
                raise RuntimeError("没有取得独立的 ESP32 只读身份结果。")
            if self.stop.is_set():
                raise RuntimeError("固件服务已经开始关闭，本次不保存新的自动安装授权。")
            binding = {"enabled": True, "board_model": board_model, "bridge": bridge,
                       "mac_address": identity["mac_address"], "manifest_sha256": digest,
                       "firmware_version": manifest["firmware_version"], "authorized_at": iso_now(),
                       "backend_url": request["backend_url"], "device_id": "omcam-" + identity["mac_address"].replace(":", ""),
                       "device_name": request["device_name"], "room_name": request["room_name"]}
            self._save(binding)
            self.credentials = {key: request[key] for key in ("ssid", "password")}
            self.seen = False
            self.notice = "已绑定目标板与本地固定固件，首次安装将自动开始；其他端口不会被探测。"
            self._start_listener()
            return {"binding": binding, "physical_flash_performed": False, "message": self.notice}

    def disable(self):
        self._require_real()
        with self.lock:
            binding = self._binding()
            if binding:
                binding["enabled"] = False
                self._save(binding)
            self.credentials = None
            self.notice = "自动接板已停用。已开始的刷写不会被强行中断；不会再启动新任务。"
        return self.status()

    def _start_listener(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            self.worker = self.fw._start_background_worker(self._listen, name="firmware-bound-usb-listener")

    def _listen(self):
        while not self.stop.wait(self.POLL_SECONDS):
            try:
                self.tick()
            except Exception as error:
                self.notice = "自动接板已暂停：" + redact_firmware_log(error)

    def close(self):
        self.stop.set()
        self.credentials = None

    def tick(self):
        with self.lock:
            binding = self._binding()
            if self.stop.is_set() or self.fw.runtime_mode != "REAL" or not binding or not binding.get("enabled"):
                return
            helper = usb_helper(self.fw.project_root)
            records = self.fw.discover_ports()
            matching = []
            for record in records:
                with contextlib.suppress(ValueError):
                    if helper.bridge_identity(record) == binding["bridge"]:
                        matching.append(record)
            if not matching:
                self.seen = False
                self.notice = "等待已绑定物理板连接；不扫描其他 USB 桥。"
                return
            try:
                port = helper.matching_port(records, binding["bridge"])
            except ValueError:
                self.notice = "存在多个 USB 串口或身份不唯一；不会自动刷写，请只保留已绑定板。"
                return
            if self.seen:
                return
            with self.fw.connect() as connection:
                if connection.execute("SELECT 1 FROM firmware_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                    return
            self.fw._verify_backend_listener(binding["backend_url"])
            fixed_bundle(helper, self.fw.project_root, binding["manifest_sha256"], binding["board_model"])
            # A starting server or temporarily unavailable local address has not
            # consumed this connection. Retry these read-only checks next tick;
            # only a validated installation attempt may suppress future ticks.
            self.seen = True
            receipt = self._receipt(binding)
            if receipt and receipt.get("status") not in {"flashed", "linked"}:
                self.notice = "该固定版本已有失败或中断尝试；不会重插循环擦写。请先核实设备状态，再明确授权恢复；当前不会重新刷写。"
                return
            if not receipt and self.credentials is None:
                self.notice = "后台重启后已清除内存中的 Wi-Fi 信息；请管理员重新绑定配置。尚未自动刷写。"
                return
            credentials = dict(self.credentials or {})
            if not receipt:
                # Commit before any write. Restart or repeated ticks cannot replay
                # an uncertain physical operation, even when job creation fails.
                with self.fw.connect() as connection:
                    connection.execute("INSERT INTO firmware_usb_receipts(mac,manifest_sha256,board_model,status) VALUES(?,?,?,'reserved')",
                                       (binding["mac_address"], binding["manifest_sha256"], binding["board_model"]))
            job = self.fw._create_job("usb_auto", self._install_worker, binding, port, credentials, bool(receipt))
            with self.fw.connect() as connection:
                connection.execute("UPDATE firmware_usb_receipts SET job_id=? WHERE mac=? AND manifest_sha256=? AND board_model=?",
                                   (job["id"], binding["mac_address"], binding["manifest_sha256"], binding["board_model"]))
            self.notice = "已启动固定固件安装。" if not receipt else "同版本已有刷写收据；只验证启动与恢复连接，不重复刷写。"

    def _install_worker(self, job_id, binding, port, credentials, already_flashed):
        try:
            with self.fw.build_lock:
                current = self._binding()
                if not current or not current.get("enabled") or current != binding or self.stop.is_set():
                    raise RuntimeError("绑定已停用或修改，未开始硬件操作。")
                self.fw._verify_backend_listener(binding["backend_url"])
                board = firmware_board(binding["board_model"])
                fixed_bundle(usb_helper(self.fw.project_root), self.fw.project_root, binding["manifest_sha256"], binding["board_model"])
                receipt = self._receipt(binding)
                if not receipt:
                    raise RuntimeError("缺少与本板型、MAC 和固定清单匹配的安装收据；未访问 USB。")
                # Re-read under build_lock: a queued argument may predate a
                # completed attempt. Durable evidence, not that stale boolean,
                # decides whether this version may be written again.
                if receipt.get("flashed_at") and receipt.get("status") in {"flashed", "linked"}:
                    already_flashed = True
                elif receipt.get("status") != "reserved" or already_flashed:
                    raise RuntimeError("当前安装收据不允许再次刷写；未访问 USB。")
                operation = "identify" if already_flashed else "flash"
                self.fw._update_job(job_id, stage="核验同一板卡并" + ("读取启动证据" if already_flashed else "刷写固定产物"), progress=10)
                evidence = self._tool({"operation": operation, "port": port, "bridge": binding["bridge"],
                                       "mac_address": binding["mac_address"], "manifest_sha256": binding["manifest_sha256"],
                                       "board_model": binding["board_model"]})
                if (evidence.get("mac_address") != binding["mac_address"] or evidence.get("chip") != board["chip"]
                        or evidence.get("board_model", DEFAULT_BOARD_MODEL) != binding["board_model"]):
                    raise RuntimeError("USB ROM 身份不匹配。")
                if not already_flashed:
                    if evidence.get("physical_flash_performed") is not True or evidence.get("manifest_sha256") != binding["manifest_sha256"]:
                        raise RuntimeError("固定固件没有返回成功刷写证据。")
                    with self.fw.connect() as connection:
                        connection.execute("UPDATE firmware_usb_receipts SET status='flashed',flashed_at=? WHERE mac=? AND manifest_sha256=? AND board_model=?",
                                           (iso_now(), binding["mac_address"], binding["manifest_sha256"], binding["board_model"]))
                device = self.fw.get_device(binding["device_id"])
                # A remembered registry row does not prove the board still has
                # usable NVS settings. Explicit new credentials always win;
                # only a credential-free replug reuses its existing enrollment.
                if not credentials and device and device.get("mac_address", "").lower() == binding["mac_address"] and not device.get("simulated"):
                    linked = self._read_ready(job_id, binding, port)
                else:
                    if not credentials:
                        raise RuntimeError("固件已刷入，但配置未保留；请管理员重新配网。不会重复刷写。")
                    enrollment = self.fw.create_enrollment(binding["device_name"], binding["room_name"])
                    payload = {key: binding[key] for key in ("backend_url", "device_id", "device_name", "room_name", "board_model")}
                    linked = self.fw._provision_worker(job_id, {**payload, **credentials, "port": port,
                                "pairing_code": enrollment["pairing_code"], "timeout_seconds": 120},
                                expected_mac=binding["mac_address"], expected_bridge=binding["bridge"])
                    device = self.fw.get_device(binding["device_id"])
                    if not device or device.get("mac_address", "").lower() != binding["mac_address"]:
                        raise RuntimeError("网络注册 MAC 不匹配，不能声称板卡联动完成。")
                with self.fw.connect() as connection:
                    connection.execute("UPDATE firmware_usb_receipts SET status='linked',linked_at=?,error=NULL WHERE mac=? AND manifest_sha256=? AND board_model=?",
                                       (iso_now(), binding["mac_address"], binding["manifest_sha256"], binding["board_model"]))
                self.credentials = None
                self.notice = "已验证同一板卡启动、串口注册和摄像头绑定；视频流需继续以实际读取状态为准。"
                return {"device": linked, "usb_identity": evidence, "physical_flash_performed": not already_flashed,
                        "same_version_skipped_flash": already_flashed, "message": self.notice}
        except Exception as error:
            with self.fw.connect() as connection:
                connection.execute("UPDATE firmware_usb_receipts SET status=CASE WHEN flashed_at IS NULL THEN 'failed' ELSE 'flashed' END,error=? WHERE mac=? AND manifest_sha256=? AND board_model=?",
                                   (redact_firmware_log(error), binding["mac_address"], binding["manifest_sha256"], binding["board_model"]))
            self.notice = "自动安装未完成；失败不循环刷写：" + redact_firmware_log(error)
            raise

    def _read_ready(self, job_id, binding, port):
        deadline = time.monotonic() + 75
        connection = self.fw._open_bound_serial(port, deadline, binding["mac_address"], binding["bridge"], binding["board_model"])
        port = getattr(connection, "port", None) or port
        try:
            while time.monotonic() < deadline:
                if self.stop.is_set():
                    raise RuntimeError("服务正在退出，停止等待自动接板。")
                raw = connection.readline(MAX_SERIAL_LINE + 2)
                if not raw:
                    continue
                parsed = parse_serial_protocol_line(raw)
                if parsed and parsed[0] == "ready":
                    verification = self.fw._verify_serial_ready_and_bind(port, binding, parsed[1])
                    return {"device_id": binding["device_id"], "hardware_verification": verification}
            raise RuntimeError("未读到对应板卡的 OMREADY，不能确认启动或视频连接。")
        finally:
            connection.close()
