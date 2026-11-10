# ==========================================================================================
# 文件        : smoke.py
# 所属目录    : scripts/
# 文件用途    : V1 一条命令冒烟: 依赖、命令层、信令、会话、界面、安卓、日志、异常、控制闭环
# 架构定位    : 验收装置, 只调用产品命令层与本地服务, 不改产品代码
# 核心功能    : 逐项判定「已实测通过 / 模拟口径 / 未验证 / 失败」, 证据落构建根, 失败非零退出
# 职责边界    : 不重启机器; 不改网络与服务; 不接真实公网; 不伪造通过
# ==========================================================================================

"""V1 冒烟执行器.

用法:
    powershell -File scripts/smoke.ps1
    python scripts/smoke.py

证据目录: %RDC_BUILD_ROOT%\\evidence\\smoke\\<时间戳>\\
退出码: 任一项失败, 或「能控制」闭环未通过, 返回 1.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import smoke_env
from input_probe import Injector, percentile
from rdc.accounts.service import AccountService
from rdc.clipboard.sync import ClipboardSyncService
from rdc.commands import accounts, api, clipboard, transfer, wall
from rdc.commands.service import CommandService
from rdc.core import logging as rdc_logging
from rdc.core.boundary import guarded
from rdc.core.debug import debug_enabled
from rdc.core.result import CommandResult
from rdc.crypto.identity import DeviceIdentity
from rdc.files import FileTransferService
from rdc.signaling.envelope import build_envelope
from rdc.signaling.server import InMemorySignalingServer

PROJECT_ROOT = smoke_env.PROJECT_ROOT

STATUS_PASS = "已实测通过"
STATUS_SIMULATED = "模拟口径"
STATUS_UNVERIFIED = "未验证"
STATUS_FAIL = "失败"

PEER_DEVICE_ID = "ABCDEFGHIJKLMNOPQRST"
SMOKE_DEVICE_ID = "RDC-SMOKE-DEVICE-01"
ACCOUNT_NAME = "smoke_user"
ACCOUNT_PASSWORD = "SmokePass123"
CONTROL_TEXT = "RDC smoke 2026"
CONTROL_REPLACED = "RDC-SMOKE-OK"


@dataclass
class SmokeItem:
    """单项冒烟结果."""

    key: str
    title: str
    status: str
    message: str
    detail: dict[str, Any]


def _emit(text: str = "") -> None:
    """控制台输出; 产品代码禁用 print, 此处用标准输出写报告."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _result(ok: bool, message: str, status: str = STATUS_PASS, **detail: Any) -> CommandResult:
    """构造单项结果, 用统一返回结构承载, 便于 guarded 兜底."""
    return CommandResult(ok=ok, message=message, data={"status": status, **detail})


def _build_root() -> Path:
    root = os.environ.get("RDC_BUILD_ROOT") or r"D:\RDC-Build"
    return Path(root)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class SmokeRunner:
    """逐项执行并汇总."""

    def __init__(self, evidence_dir: Path) -> None:
        self.evidence_dir = evidence_dir
        self.items: list[SmokeItem] = []

    def add(self, key: str, title: str, status: str, message: str, **detail: Any) -> None:
        self.items.append(SmokeItem(key, title, status, message, detail))

    def execute(self, key: str, title: str, func: Any, *, simulated: bool = False) -> CommandResult:
        """执行一项, 异常经统一边界转为失败, 不中断后续项."""
        result = guarded(key, func)
        status = STATUS_FAIL if not result.ok else STATUS_SIMULATED if simulated else STATUS_PASS
        data = dict(result.data or {})
        embedded = data.pop("status", None)
        if result.ok and embedded:
            status = str(embedded)
        self.add(key, title, status, result.message, **data, ok=result.ok)
        return result


# ---------------------------------------------------------------------------- 环境与命令层


def check_environment() -> CommandResult:
    """依赖、版本、构建根可写与证书存在性."""
    import PIL
    import PySide6

    problems: list[str] = []
    build_root = _build_root()
    try:
        (build_root / "evidence" / "smoke").mkdir(parents=True, exist_ok=True)
    except OSError as error:
        problems.append(f"构建根不可写: {error}")
    cert_dir = build_root / "certs" / "relay"
    certs = {name: (cert_dir / name).is_file() for name in ("ca.crt", "server.crt", "client.crt")}
    if problems:
        return _result(False, "环境自检失败: " + "; ".join(problems), **{"build_root": str(build_root)})
    return _result(
        True,
        "环境自检通过",
        python=sys.version.split()[0],
        pyside6=PySide6.__version__,
        pillow=PIL.__version__,
        build_root=str(build_root),
        certs=certs,
    )


def check_commands() -> CommandResult:
    """账号、绑定、免确认、申请审批、被控、屏幕墙命令逐条可调用."""
    api.configure(CommandService())
    accounts.configure(AccountService())
    wall.configure(wall.WallService())

    registered = accounts.register_account(ACCOUNT_NAME, ACCOUNT_PASSWORD)
    if not registered.ok:
        return _result(False, f"注册失败: {registered.message}")
    login = accounts.login_account(ACCOUNT_NAME, ACCOUNT_PASSWORD)
    if not login.ok or not login.data:
        return _result(False, f"登录失败: {login.message}")
    refreshed = accounts.refresh_session_token(str(login.data["device_token"]))
    if not refreshed.ok:
        return _result(False, f"会话令牌刷新失败: {refreshed.message}")

    accounts.get_service().register_device(SMOKE_DEVICE_ID, "冒烟靶机")
    bound = accounts.bind_device(SMOKE_DEVICE_ID, "123456")
    if not bound.ok:
        return _result(False, f"绑定失败: {bound.message}")

    devices = accounts.list_devices()
    first_device = devices.data["items"][0] if devices.data and devices.data["items"] else {}
    require_confirm_default = bool(first_device.get("require_confirm"))
    trusted = accounts.set_trust_policy(SMOKE_DEVICE_ID, False)
    restored = accounts.set_trust_policy(SMOKE_DEVICE_ID, True)
    requested = accounts.request_access(SMOKE_DEVICE_ID, "control", "冒烟申请")
    if not requested.ok or not requested.data:
        return _result(False, f"申请失败: {requested.message}")
    approved = accounts.approve_access(str(requested.data["request_id"]), 600, True)
    if not approved.ok:
        return _result(False, f"审批失败: {approved.message}")
    grants = accounts.list_grants(SMOKE_DEVICE_ID)
    revoked = accounts.revoke_grant(str(approved.data["grant_id"]))

    hosting = api.start_hosting("smoke")
    refreshed_code = api.refresh_access_code()
    stopped = api.stop_hosting()

    created = wall.start_wall(4)
    if not created.ok or not created.data:
        return _result(False, f"屏幕墙创建失败: {created.message}")
    focused = wall.focus_wall_cell(str(created.data["wall_id"]), 0)
    resized = wall.set_wall_layout(str(created.data["wall_id"]), 9)
    closed = wall.close_wall(str(created.data["wall_id"]))

    steps = {
        "login": login.ok,
        "refresh_token": refreshed.ok,
        "bind": bound.ok,
        "require_confirm_default": require_confirm_default,
        "trust_device": trusted.ok,
        "trust_restore": restored.ok,
        "request": requested.ok,
        "approve": approved.ok,
        "grants": grants.ok,
        "revoke": revoked.ok,
        "hosting": hosting.ok,
        "refresh_code": refreshed_code.ok,
        "stop_hosting": stopped.ok,
        "wall_start": created.ok,
        "wall_focus": focused.ok,
        "wall_layout": resized.ok,
        "wall_close": closed.ok,
    }
    failed = [name for name, ok in steps.items() if not ok]
    if failed:
        return _result(False, "命令调用失败: " + ", ".join(failed), steps=steps)
    return _result(True, "命令层 16 个动作逐条可调用", steps=steps, grant_id=approved.data["grant_id"])


def _authenticate(server: InMemorySignalingServer, identity: DeviceIdentity, label: str) -> str | None:
    """注册 + 挑战 + 签名认证; 成功返回设备令牌, 失败返回 None."""
    responses = server.handle(
        build_envelope(
            identity,
            "register",
            {
                "device_id": identity.device_id,
                "public_key": identity.public_key_b64,
                "capabilities": ["relay"],
                "protocol_version": 1,
                "display_name": label,
            },
        )
    )
    if not responses or responses[0].get("type") != "challenge":
        return None
    nonce = responses[0]["payload"]["nonce"]
    auth = server.handle(
        build_envelope(
            identity,
            "auth",
            {
                "device_id": identity.device_id,
                "signature": identity.sign({"device_id": identity.device_id, "nonce": nonce}),
            },
        )
    )
    if not auth or auth[0].get("type") != "auth_ok":
        return None
    return str(auth[0]["payload"]["token"])


def check_signaling() -> CommandResult:
    """内存信令: 双设备注册、挑战、签名认证与 offer 路由."""
    server = InMemorySignalingServer()
    alice, bob = DeviceIdentity.generate(), DeviceIdentity.generate()
    tokens: dict[str, str] = {}
    for identity, label in ((alice, "alice"), (bob, "bob")):
        token = _authenticate(server, identity, label)
        if token is None:
            return _result(False, f"{label} 注册或认证失败")
        tokens[label] = token

    routed = server.handle(
        build_envelope(
            alice,
            "offer",
            {
                "from_device_id": alice.device_id,
                "to_device_id": bob.device_id,
                "session_id": "smoke-session",
                "candidates": [],
                "media_caps": {"codecs": ["h264"]},
                "access_proof": "c21va2U=",
            },
            session_id="smoke-session",
        ),
        device_id=alice.device_id,
    )
    if len(server.routed) != 1 or not routed:
        return _result(False, "offer 未成功路由", errors=server.errors)
    return _result(
        True,
        "信令注册/挑战/认证/路由通过",
        alice=alice.device_id,
        bob=bob.device_id,
        routed=len(server.routed),
        token_issued=len(tokens) == 2,
    )


def check_session_reconnect() -> CommandResult:
    """会话建立、停止、重连与取消; 内存口径, 不接真实媒体."""
    service = CommandService()
    service.register_peer(PEER_DEVICE_ID, "对端")
    first = service.start_connection(PEER_DEVICE_ID, "123456")
    if not first.ok or not first.data:
        return _result(False, f"首次连接失败: {first.message}")
    task_id = str(first.data["task_id"])
    progress = service.get_connection_progress(task_id)
    session = service.add_session(PEER_DEVICE_ID)
    listed_before = service.list_sessions()
    stopped = service.stop_session(session.session_id)
    listed_after = service.list_sessions()
    second = service.start_connection(PEER_DEVICE_ID, "123456")
    canceled = service.cancel_connection(str(second.data["task_id"])) if second.data else second

    before = len(listed_before.data["items"]) if listed_before.data else -1
    after = len(listed_after.data["items"]) if listed_after.data else -1
    if not (progress.ok and stopped.ok and second.ok and canceled.ok and before == 1 and after == 0):
        return _result(False, "会话建立或重连未通过", before=before, after=after)
    return _result(
        True,
        "会话建立→停止→重连→取消通过",
        before=before,
        after=after,
        reconnect_task=second.data["task_id"] if second.data else None,
        state=session.state,
        status=STATUS_SIMULATED,
        note="内存会话, 未接真实采集/编码/网络媒体",
    )


def check_media_frames() -> CommandResult:
    """中继数据面密文帧转发; 模拟口径, 非视频帧."""
    from poc.tools import run_relay_dataplane_check

    os.environ.setdefault("RDC_BUILD_ROOT", str(_build_root()))
    try:
        result = run_relay_dataplane_check.run()
    except RuntimeError as error:
        return _result(
            False,
            f"中继数据面双进程未通过: {error}",
            status=STATUS_FAIL,
            defect="同一 session 第二次 relay_allocate 会吊销对端已发 relay_token, 发布端握手被拒",
            repro=(
                "同一 session_id 先后为两个 DeviceIdentity 调用 relay_allocate; "
                "第二个令牌签发后第一个令牌 resolve_relay_token 返回 None, protocol-v1 §6 要求令牌绑定设备标识"
            ),
            owner="信令主线 / relay",
        )
    if not result.get("all_passed"):
        return _result(False, "中继数据面转发或限速未通过", status=STATUS_FAIL, detail=result.get("checks"))
    checks = result.get("checks", {})
    return _result(
        True,
        "中继 32 帧密文逐字节一致, 预算限速生效",
        status=STATUS_SIMULATED,
        checks={name: bool(item.get("passed")) for name, item in checks.items()},
        note="密文帧转发, 不是视频帧, 不是画质通道验收",
    )


# ---------------------------------------------------------------------------- 界面与安卓


def check_ui_shell() -> CommandResult:
    """七页界面壳离屏构造与切换, 无后端时显示未连接状态."""
    from PySide6.QtWidgets import QApplication

    from rdc.ui.main_window import NAV_ITEMS, MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    visited: list[str] = []
    for item in NAV_ITEMS:
        window.switch_page(item[0])
        visited.append(item[0])
    disconnected = window.home.connect_status.text()
    window.close()
    app.processEvents()
    if len(visited) != len(NAV_ITEMS):
        return _result(False, "界面页切换不完整", visited=visited)
    return _result(
        True,
        f"离屏构造并切换 {len(visited)} 页, 连接页显示「{disconnected}」",
        pages=visited,
        connect_status=disconnected,
        platform=os.environ.get("QT_QPA_PLATFORM"),
        note="离屏口径; 未做真实显示器交互验证",
    )


def check_android_apk() -> CommandResult:
    """APK 存在性与签名产物; 有模拟器时安装、启动并截图."""
    apk = _build_root() / "artifacts" / "android" / "app-release-control-arm64-v8a-signed.apk"
    if not apk.is_file():
        return _result(False, "控制端 APK 产物缺失", status=STATUS_FAIL, path=str(apk))
    digest = _sha256(apk)

    adb = _build_root() / "tools" / "android" / "sdk" / "platform-tools" / "adb.exe"
    if not adb.is_file():
        return _result(
            True,
            "APK 存在, 但本机无 adb, 未安装",
            status=STATUS_UNVERIFIED,
            path=str(apk),
            sha256=digest,
            reason="adb 缺失",
        )
    devices = subprocess.run([str(adb), "devices"], capture_output=True, text=True, timeout=30, check=False)
    online = [line.split("\t")[0] for line in devices.stdout.splitlines() if line.endswith("\tdevice")]
    if not online:
        return _result(
            True,
            "APK 存在且哈希已记录, 但无已启动的模拟器/真机",
            status=STATUS_UNVERIFIED,
            path=str(apk),
            sha256=digest,
            reason="adb devices 为空; 当前无 x86_64 AVD 启动",
        )

    serial = online[0]
    install = subprocess.run(
        [str(adb), "-s", serial, "install", "-r", str(apk)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if "Success" not in install.stdout:
        return _result(False, f"APK 安装失败: {install.stdout.strip()}", status=STATUS_FAIL, serial=serial)
    subprocess.run(
        [str(adb), "-s", serial, "shell", "am", "start", "-n", "com.rdc.remote/.MainActivity"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    time.sleep(3)
    pid = subprocess.run(
        [str(adb), "-s", serial, "shell", "pidof", "com.rdc.remote"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    screenshot = _build_root() / "evidence" / "smoke" / "android.png"
    screen = subprocess.run(
        [str(adb), "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if screen.stdout:
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(screen.stdout)
    subprocess.run(
        [str(adb), "-s", serial, "shell", "input", "tap", "540", "960"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    started = bool(pid.stdout.strip())
    return _result(
        started,
        "模拟器安装/启动通过" if started else "APK 已安装但进程未启动",
        status=STATUS_SIMULATED if started else STATUS_FAIL,
        serial=serial,
        sha256=digest,
        screenshot=str(screenshot),
        touch_note="触摸注入只到系统 input tap, 应用侧未埋点, 契约入口生效未验证",
        note="x86_64 模拟器口径, 不作性能结论",
    )


# ---------------------------------------------------------------------------- 日志与异常


def check_logging_redaction(evidence_dir: Path) -> CommandResult:
    """结构化日志字段、脱敏、落盘无明文与异常追踪号."""
    log_dir = evidence_dir / "logs"
    os.environ["RDC_LOG_DIR"] = str(log_dir)
    rdc_logging.reset_for_tests()
    rdc_logging.configure()

    secret_password = "PlainPassword-123"
    secret_token = "rdc_plain_token_should_not_leak"
    rdc_logging.log_event(
        "smoke.logging",
        "probe.event",
        level="INFO",
        session_id="smoke-session",
        trace_id="smoke-trace",
        duration_ms=12.5,
        error_code=None,
        password=secret_password,
        token=secret_token,
        ip="192.168.31.80",
    )
    recent = rdc_logging.recent_events(20)
    probe = next((item for item in recent if item.get("event") == "probe.event"), None)
    if probe is None:
        return _result(False, "最近事件缓冲未记录探针")
    required = ("timestamp", "level", "session_id", "trace_id", "module", "event", "duration_ms", "error_code")
    missing = [name for name in required if name not in probe]
    dumped = json.dumps(recent, ensure_ascii=False)
    log_file = log_dir / rdc_logging.LOG_FILE_NAME
    file_text = log_file.read_text(encoding="utf-8") if log_file.is_file() else ""
    leaked = (
        secret_password in dumped or secret_token in dumped or secret_password in file_text or secret_token in file_text
    )
    redacted = probe.get("password") == "***" and probe.get("token") == "***"

    boom = guarded("smoke.boom", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    trace_ok = (not boom.ok) and "追踪号" in boom.message
    if missing or leaked or not redacted or not trace_ok:
        return _result(
            False,
            "日志或脱敏未通过",
            missing=missing,
            leaked=leaked,
            redacted=redacted,
            trace_id_returned=trace_ok,
        )
    return _result(
        True,
        "固定字段齐全、口令/令牌脱敏、落盘无明文、异常返回追踪号",
        missing=missing,
        log_file=str(log_file),
        debug_default_off=not debug_enabled(),
        trace_message=boom.message,
    )


def check_error_paths() -> CommandResult:
    """异常路径必须返回契约错误码."""
    from rdc.core import errors

    api.configure(CommandService())
    accounts.configure(AccountService())
    wall.configure(wall.WallService())
    clipboard.configure(ClipboardSyncService())
    transfer.configure(FileTransferService())

    checks = {
        errors.DEVICE_NOT_FOUND: api.start_connection("not-a-device", "123456").error_code,
        errors.SESSION_NOT_FOUND: api.stop_session("missing").error_code,
        errors.TASK_NOT_FOUND: api.get_connection_progress("missing").error_code,
        errors.TRANSFER_NOT_FOUND: transfer.get_transfer_progress("missing").error_code,
        errors.WALL_NOT_FOUND: wall.close_wall("missing").error_code,
        errors.CLIPBOARD_DISABLED: _disabled_clipboard_error(),
        errors.ACCOUNT_NOT_FOUND: accounts.login_account("nobody", "whatever").error_code,
        errors.CREDENTIAL_INVALID: accounts.list_devices().error_code,
    }
    checks.update(_v15_error_codes())
    mismatched = {name: actual for name, actual in checks.items() if actual != name}
    if mismatched:
        return _result(False, "错误码不一致", mismatched=mismatched, expected=list(checks))
    return _result(True, f"{len(checks)} 条异常路径返回正确错误码", codes=list(checks))


def _disabled_clipboard_error() -> str | None:
    service = ClipboardSyncService()
    service.set_enabled(False)
    clipboard.configure(service)
    return clipboard.push_clipboard("desktop", "text", "x").error_code


def _v15_error_codes() -> dict[str, str | None]:
    """契约 v1.5 新增 4 条错误码的实链取证."""
    from rdc.core import errors

    accounts.configure(AccountService())
    owner = accounts.register_account(ACCOUNT_NAME, ACCOUNT_PASSWORD)
    subject = accounts.register_account("smoke_error_subject", ACCOUNT_PASSWORD)
    target = accounts.register_account("smoke_error_target", ACCOUNT_PASSWORD)
    login = accounts.login_account(ACCOUNT_NAME, ACCOUNT_PASSWORD)
    missing = "夹具失败"
    if not (owner.ok and subject.ok and target.ok and login.ok and login.data and subject.data):
        return {
            errors.SCOPE_INVALID: missing,
            errors.GRANT_NOT_TRANSFERABLE: missing,
            errors.GRANT_SCOPE_EXCEEDED: missing,
            errors.PREVIEW_TOKEN_INVALID: missing,
        }
    subject_id = str(subject.data["account_id"])
    target_id = str(target.data["account_id"])
    service = accounts.get_service()
    service.register_device(SMOKE_DEVICE_ID, "冒烟靶机")
    accounts.bind_device(SMOKE_DEVICE_ID, "123456")

    scope_invalid = accounts.create_grant(SMOKE_DEVICE_ID, subject_id, "admin", 600, False, True)
    fixed = accounts.create_grant(SMOKE_DEVICE_ID, subject_id, "control", 600, False, False)
    parent = accounts.create_grant(SMOKE_DEVICE_ID, subject_id, "control", 600, False, True)
    accounts.logout_account()
    accounts.login_account("smoke_error_subject", ACCOUNT_PASSWORD)
    fixed_id = str(fixed.data["grant_id"]) if fixed.ok and fixed.data else ""
    parent_id = str(parent.data["grant_id"]) if parent.ok and parent.data else ""
    not_transferable = accounts.transfer_grant(fixed_id, target_id, 300, False, "control")
    exceeded = accounts.transfer_grant(parent_id, target_id, 300, False, "full")

    wall_service = wall.WallService(account_id="smoke-error-account")
    wall.configure(wall_service)
    created = wall.start_wall(4)
    preview_code: str | None = missing
    if created.ok and created.data:
        wall_id = str(created.data["wall_id"])
        for cell in range(4):
            wall_service.walls[wall_id].plan["cells"][cell]["device_id"] = SMOKE_DEVICE_ID
        issued = wall.issue_preview_token(wall_id, 0)
        if issued.ok and issued.data:
            rejected = wall_service.validate_preview_token(str(issued.data["token"]), wall_id, 1, "smoke-error-account")
            preview_code = rejected.error_code
    return {
        errors.SCOPE_INVALID: scope_invalid.error_code,
        errors.GRANT_NOT_TRANSFERABLE: not_transferable.error_code,
        errors.GRANT_SCOPE_EXCEEDED: exceeded.error_code,
        errors.PREVIEW_TOKEN_INVALID: preview_code,
    }


# ---------------------------------------------------------------------------- 契约 v1.5 增量


def _failed_names(checks: dict[str, bool]) -> list[str]:
    return [name for name, passed in checks.items() if not passed]


def check_grants_v15() -> CommandResult:
    """契约 41-42: 转授越界、不可转授、二次转授与正常转授."""
    from rdc.core import errors

    accounts.configure(AccountService())
    owner = accounts.register_account(ACCOUNT_NAME, ACCOUNT_PASSWORD)
    subject = accounts.register_account("smoke_subject", ACCOUNT_PASSWORD)
    grand = accounts.register_account("smoke_grand", ACCOUNT_PASSWORD)
    login = accounts.login_account(ACCOUNT_NAME, ACCOUNT_PASSWORD)
    if not (owner.ok and subject.ok and grand.ok and login.ok and login.data and subject.data and grand.data):
        return _result(
            False,
            "账号夹具准备失败, 授权路径无法执行",
            owner=owner.message,
            subject=subject.message,
            grand=grand.message,
            login=login.message,
        )
    owner_id = str(login.data["account_id"])
    subject_id = str(subject.data["account_id"])
    grand_id = str(grand.data["account_id"])
    service = accounts.get_service()
    service.register_device(SMOKE_DEVICE_ID, "冒烟靶机")
    accounts.bind_device(SMOKE_DEVICE_ID, "123456")

    checks: dict[str, bool] = {}
    invalid_scope = accounts.create_grant(SMOKE_DEVICE_ID, subject_id, "admin", 600, False, False)
    checks["scope_invalid"] = invalid_scope.error_code == errors.SCOPE_INVALID
    fixed = accounts.create_grant(SMOKE_DEVICE_ID, subject_id, "control", 600, False, False)
    parent = accounts.create_grant(SMOKE_DEVICE_ID, subject_id, "control", 600, False, True)
    checks["create_transferable"] = bool(parent.ok and parent.data and parent.data.get("transferable") is True)
    fixed_id = str(fixed.data["grant_id"]) if fixed.ok and fixed.data else ""
    parent_id = str(parent.data["grant_id"]) if parent.ok and parent.data else ""
    if not (fixed_id and parent_id):
        return _result(
            False,
            "授权创建失败, 转授路径无法执行",
            checks=checks,
            fixed=fixed.message,
            parent=parent.message,
        )

    not_holder = accounts.transfer_grant(parent_id, grand_id, 300, False, "control")
    checks["holder_only"] = not_holder.error_code == errors.PERMISSION_DENIED

    accounts.logout_account()
    subject_login = accounts.login_account("smoke_subject", ACCOUNT_PASSWORD)
    if not (subject_login.ok and subject_login.data):
        return _result(False, "受让人登录失败, 转授路径无法执行", checks=checks, login=subject_login.message)
    checks["exceeded_scope"] = (
        accounts.transfer_grant(parent_id, grand_id, 300, False, "full").error_code == errors.GRANT_SCOPE_EXCEEDED
    )
    checks["exceeded_expiry"] = (
        accounts.transfer_grant(parent_id, grand_id, 1200, False, "control").error_code == errors.GRANT_SCOPE_EXCEEDED
    )
    checks["not_transferable"] = (
        accounts.transfer_grant(fixed_id, grand_id, 300, False, "control").error_code == errors.GRANT_NOT_TRANSFERABLE
    )
    child = accounts.transfer_grant(parent_id, grand_id, 300, False, "control")
    checks["transfer_ok"] = bool(
        child.ok
        and child.data
        and child.data.get("transferable") is False
        and child.data.get("scope") == "control"
        and child.data.get("subject") == grand_id
    )
    child_id = str(child.data["grant_id"]) if child.ok and child.data else ""
    if child_id:
        accounts.logout_account()
        accounts.login_account("smoke_grand", ACCOUNT_PASSWORD)
        second = accounts.transfer_grant(child_id, owner_id, 60, False, "view")
        checks["second_level_denied"] = second.error_code == errors.GRANT_NOT_TRANSFERABLE
    else:
        checks["second_level_denied"] = False

    failed = _failed_names(checks)
    if failed:
        return _result(False, "授权检查未通过: " + ", ".join(failed), checks=checks)
    return _result(True, f"授权签发/越界/不可转授/二次转授 {len(checks)} 项符合契约", checks=checks)


def check_account_revocation() -> CommandResult:
    """契约 43-44: 登出与吊销设备后会话立即失效."""
    from rdc.core import errors

    accounts.configure(AccountService())
    registered = accounts.register_account(ACCOUNT_NAME, ACCOUNT_PASSWORD)
    login = accounts.login_account(ACCOUNT_NAME, ACCOUNT_PASSWORD)
    if not (registered.ok and login.ok and login.data):
        return _result(False, "账号夹具准备失败", registered=registered.message, login=login.message)
    account_id = str(login.data["account_id"])
    device_token = str(login.data["device_token"])
    service = accounts.get_service()
    service.register_device(SMOKE_DEVICE_ID, "冒烟靶机")
    accounts.bind_device(SMOKE_DEVICE_ID, "123456")

    checks: dict[str, bool] = {}
    logout = accounts.logout_account()
    checks["logout_ok"] = bool(logout.ok and logout.data)
    checks["logout_device_token_dead"] = accounts.refresh_session_token(device_token).error_code == errors.TOKEN_REVOKED
    checks["logout_session_dead"] = accounts.list_devices().error_code == errors.CREDENTIAL_INVALID

    relogin = accounts.login_account(ACCOUNT_NAME, ACCOUNT_PASSWORD)
    checks["relogin_ok"] = bool(relogin.ok and relogin.data)
    bound_token = service.store.issue_token("device", account_id, SMOKE_DEVICE_ID, 3600)[0]
    refreshed = accounts.refresh_session_token(bound_token)
    checks["bound_session_ok"] = bool(refreshed.ok and refreshed.data)
    revoked = accounts.revoke_device_token(SMOKE_DEVICE_ID)
    checks["revoke_ok"] = bool(
        revoked.ok
        and revoked.data
        and int(revoked.data.get("revoked_tokens", 0)) >= 1
        and int(revoked.data.get("kicked_sessions", 0)) >= 1
    )
    checks["revoke_session_dead"] = accounts.list_devices().error_code == errors.TOKEN_REVOKED
    checks["revoke_device_token_dead"] = accounts.refresh_session_token(bound_token).error_code == errors.TOKEN_REVOKED

    failed = _failed_names(checks)
    if failed:
        return _result(False, "登出/吊销检查未通过: " + ", ".join(failed), checks=checks)
    return _result(True, f"登出与吊销即时失效 {len(checks)} 项符合契约", checks=checks)


def check_wall_v15() -> CommandResult:
    """契约 45-47: 聚焦释放 4K60、审计保留、预览令牌跨格与过期."""
    from rdc.core import errors

    # 审计权限由独立项覆盖; 本项只验审计可读、保留与预览令牌, 用守卫隔离账号前置状态
    service = wall.WallService(account_id="smoke-wall-account", audit_guard=lambda wall_id: True)
    wall.configure(service)
    first = wall.start_wall(4)
    second = wall.start_wall(4)
    if not (first.ok and first.data and second.ok and second.data):
        return _result(False, "屏幕墙创建失败", first=first.message, second=second.message)
    first_id = str(first.data["wall_id"])
    second_id = str(second.data["wall_id"])
    for wall_id in (first_id, second_id):
        for cell in range(4):
            service.walls[wall_id].plan["cells"][cell]["device_id"] = SMOKE_DEVICE_ID

    checks: dict[str, bool] = {}
    focus_first = wall.focus_wall_cell(first_id, 0)
    blocked = wall.focus_wall_cell(second_id, 0)
    checks["focus_exclusive"] = bool(focus_first.ok and blocked.error_code == errors.WALL_BUDGET_EXCEEDED)
    stopped = wall.stop_wall_focus(first_id)
    checks["stop_releases_slot"] = bool(
        stopped.ok
        and stopped.data
        and stopped.data.get("focused_cell") is None
        and stopped.data.get("quality_profile") == "preview_480p15"
        and stopped.data.get("units_used") == 4
    )
    checks["focus_after_release"] = wall.focus_wall_cell(second_id, 0).ok

    audit = wall.get_wall_audit_log(first_id)
    events = [str(item.get("event")) for item in (audit.data or {}).get("items", [])] if audit.ok else []
    checks["audit_focus_events"] = all(name in events for name in ("wall.start", "wall.focus", "wall.focus.stop"))
    closed = wall.close_wall(first_id)
    audit_closed = wall.get_wall_audit_log(first_id)
    events_closed = (
        [str(item.get("event")) for item in (audit_closed.data or {}).get("items", [])] if audit_closed.ok else []
    )
    checks["audit_kept_after_close"] = bool(closed.ok and audit_closed.ok and "wall.close" in events_closed)
    checks["close_twice_not_found"] = wall.close_wall(first_id).error_code == errors.WALL_NOT_FOUND

    issued = wall.issue_preview_token(second_id, 0)
    checks["preview_issue_ok"] = bool(
        issued.ok
        and issued.data
        and issued.data.get("scope") == "view"
        and issued.data.get("single_use") is False
        and issued.data.get("expires_at")
    )
    token = str(issued.data["token"]) if issued.ok and issued.data else ""
    if token:
        valid = service.validate_preview_token(token, second_id, 0, "smoke-wall-account")
        cross_cell = service.validate_preview_token(token, second_id, 1, "smoke-wall-account")
        checks["preview_valid"] = valid.ok
        checks["preview_cross_cell_rejected"] = cross_cell.error_code == errors.PREVIEW_TOKEN_INVALID
        reissued = wall.issue_preview_token(second_id, 0)
        old_after_reissue = service.validate_preview_token(token, second_id, 0, "smoke-wall-account")
        checks["preview_reissue_revokes_old"] = bool(
            reissued.ok and old_after_reissue.error_code == errors.PREVIEW_TOKEN_INVALID
        )
        new_token = str(reissued.data["token"]) if reissued.ok and reissued.data else ""
        record = service.preview_tokens.lookup(new_token) if new_token else None
        if record is not None:
            expired_record, expired_reason = service.preview_tokens.validate(
                new_token, second_id, 0, "smoke-wall-account", now=record.expires_at + 1
            )
            checks["preview_expired"] = expired_record is None and "过期" in expired_reason
        else:
            checks["preview_expired"] = False

    close_second = wall.close_wall(second_id)
    token_after_close = service.validate_preview_token(token, second_id, 0, "smoke-wall-account") if token else None
    checks["close_revokes_preview"] = bool(
        close_second.ok
        and token_after_close is not None
        and token_after_close.error_code == errors.PREVIEW_TOKEN_INVALID
    )
    audit_second = wall.get_wall_audit_log(second_id)
    second_events = (
        [str(item.get("event")) for item in (audit_second.data or {}).get("items", [])] if audit_second.ok else []
    )
    checks["preview_audit_readable"] = bool(audit_second.ok and "wall.preview_token.issue" in second_events)

    failed = _failed_names(checks)
    if failed:
        return _result(False, "屏幕墙检查未通过: " + ", ".join(failed), checks=checks)
    return _result(True, f"聚焦释放/审计/预览令牌 {len(checks)} 项符合契约", checks=checks)


def check_wall_audit_permissions() -> CommandResult:
    """契约 v1.7 第 46 条: 未登录、非所有者、未接线一律 PERMISSION_DENIED."""
    from rdc.core import errors

    owner_name = "smoke_wall_owner"
    other_name = "smoke_wall_other"
    device_id = "SMOKE-WALL-DEVICE-01"
    account_service = AccountService()
    accounts.configure(account_service)
    account_service.register_device(device_id, "墙审计靶机")
    account_service.register_account(owner_name, ACCOUNT_PASSWORD)
    account_service.register_account(other_name, ACCOUNT_PASSWORD)
    login = account_service.login_account(owner_name, ACCOUNT_PASSWORD)
    bound = account_service.bind_device(device_id, "123456")
    if not (login.ok and bound.ok):
        return _result(False, "墙审计账号夹具准备失败", login=login.message, bind=bound.message)

    def new_wall(account_id: str | None, device: str) -> str:
        service = wall.WallService(account_id=account_id)
        wall.configure(service)
        created = wall.start_wall(4)
        wall_id = str(created.data["wall_id"])
        for cell in range(4):
            service.walls[wall_id].plan["cells"][cell]["device_id"] = device
        return wall_id

    checks: dict[str, bool] = {}
    account_service.logout_account()
    checks["unauthenticated_denied"] = (
        wall.get_wall_audit_log(new_wall(owner_name, device_id)).error_code == errors.PERMISSION_DENIED
    )

    account_service.login_account(other_name, ACCOUNT_PASSWORD)
    checks["non_owner_denied"] = (
        wall.get_wall_audit_log(new_wall(other_name, device_id)).error_code == errors.PERMISSION_DENIED
    )

    checks["unwired_denied"] = (
        wall.get_wall_audit_log(new_wall(None, "SMOKE-UNREGISTERED-DEVICE")).error_code == errors.PERMISSION_DENIED
    )

    account_service.logout_account()
    account_service.login_account(owner_name, ACCOUNT_PASSWORD)
    checks["owner_allowed"] = wall.get_wall_audit_log(new_wall(owner_name, device_id)).ok

    failed = _failed_names(checks)
    if failed:
        return _result(False, "墙审计权限检查未通过: " + ", ".join(failed), checks=checks)
    return _result(
        True,
        f"未登录/非所有者/未接线拒绝、所有者放行共 {len(checks)} 项符合契约",
        checks=checks,
        note="账号身份取当前登录账号; 未接入真实账号服务时默认拒绝",
    )


def check_presence() -> CommandResult:
    """契约 v1.2: 30 秒心跳、90 秒过期与 online/offline/unknown 三态."""
    accounts.configure(AccountService())
    registered = accounts.register_account(ACCOUNT_NAME, ACCOUNT_PASSWORD)
    login = accounts.login_account(ACCOUNT_NAME, ACCOUNT_PASSWORD)
    if not (registered.ok and login.ok and login.data):
        return _result(False, "账号夹具准备失败", registered=registered.message, login=login.message)
    service = accounts.get_service()
    service.register_device(SMOKE_DEVICE_ID, "冒烟靶机")
    accounts.bind_device(SMOKE_DEVICE_ID, "123456")
    store = service.store

    def device_item() -> dict[str, Any]:
        listing = accounts.list_devices()
        items = (listing.data or {}).get("items", []) if listing.ok else []
        return items[0] if items else {}

    checks: dict[str, bool] = {}
    initial = device_item()
    checks["list_fields_present"] = "presence_state" in initial and "last_seen_at" in initial
    checks["unknown_initially"] = bool(
        initial.get("presence_state") == "unknown"
        and initial.get("last_seen_at") is None
        and initial.get("online") is False
    )
    now = time.time()
    store.record_presence(SMOKE_DEVICE_ID, "online", now=now)
    current = device_item()
    checks["online_after_heartbeat"] = bool(
        current.get("presence_state") == "online"
        and current.get("online") is True
        and current.get("last_seen_at") is not None
    )
    store.record_presence(SMOKE_DEVICE_ID, "online", now=now - 31)
    fresh = device_item()
    checks["heartbeat_30s_keeps_online"] = bool(fresh.get("presence_state") == "online" and fresh.get("online") is True)
    store.record_presence(SMOKE_DEVICE_ID, "online", now=now - 91)
    stale = device_item()
    checks["expired_90s_offline"] = bool(stale.get("presence_state") == "offline" and stale.get("online") is False)
    store.record_presence(SMOKE_DEVICE_ID, "offline", now=now)
    explicit = device_item()
    checks["explicit_offline"] = bool(explicit.get("presence_state") == "offline" and explicit.get("online") is False)
    store.record_presence(SMOKE_DEVICE_ID, "online", now=time.time())
    recovered = device_item()
    checks["online_after_offline"] = bool(
        recovered.get("presence_state") == "online" and recovered.get("online") is True
    )
    store.record_presence(SMOKE_DEVICE_ID, "offline", now=time.time())
    items = (accounts.list_devices().data or {}).get("items", [])
    checks["list_online_consistent"] = all(
        bool(item.get("online")) == (item.get("presence_state") == "online") for item in items
    )

    server = InMemorySignalingServer()
    alice, bob = DeviceIdentity.generate(), DeviceIdentity.generate()
    checks["signal_auth"] = bool(
        _authenticate(server, alice, "alice") is not None and _authenticate(server, bob, "bob") is not None
    )
    server.handle(
        build_envelope(alice, "presence", {"device_id": alice.device_id, "state": "online"}),
        device_id=alice.device_id,
    )
    last_seen = time.time()
    signal_online = server.presence_for(alice.device_id)
    checks["signal_online"] = bool(
        signal_online.get("presence_state") == "online" and signal_online.get("online") is True
    )
    checks["signal_unknown"] = server.presence_for(bob.device_id).get("presence_state") == "unknown"
    checks["signal_heartbeat_30s"] = (
        server.presence_for(alice.device_id, now=last_seen + 31).get("presence_state") == "online"
    )
    checks["signal_expired_90s"] = (
        server.presence_for(alice.device_id, now=last_seen + 91).get("presence_state") == "offline"
    )

    failed = _failed_names(checks)
    if failed:
        return _result(False, "在线状态检查未通过: " + ", ".join(failed), checks=checks)
    return _result(True, f"在线三态与 30/90 秒窗口 {len(checks)} 项符合契约", checks=checks)


# ---------------------------------------------------------------------------- 控制闭环


def check_control_loop(evidence_dir: Path) -> CommandResult:
    """输入注入 → 靶窗口实际生效 → 进程级断言 + 截图; 记录 p50/p95."""
    from PIL import ImageGrab

    if sys.platform != "win32":
        return _result(False, "控制闭环只在 Windows 本机执行", status=STATUS_UNVERIFIED, reason="非 Windows")

    work = evidence_dir / "control"
    work.mkdir(parents=True, exist_ok=True)
    ready_path, state_path = work / "ready.json", work / "state.json"
    for path in (ready_path, state_path):
        if path.exists():
            path.unlink()
    target = subprocess.Popen(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "smoke_target.py"),
            "--ready",
            str(ready_path),
            "--state",
            str(state_path),
        ],
        cwd=str(PROJECT_ROOT),
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not ready_path.exists():
            time.sleep(0.1)
        if not ready_path.exists():
            return _result(False, "靶窗口未在 20 秒内就绪", status=STATUS_FAIL)
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        time.sleep(1.5)

        injector = Injector()
        text_box = ready["text"]
        injector.click(text_box["x"] + text_box["w"] // 2, text_box["y"] + text_box["h"] // 2)
        time.sleep(0.3)

        sent_at = injector.type_text(CONTROL_TEXT)
        state = _wait_state(state_path, lambda item: item.get("text") == CONTROL_TEXT, 8.0)
        if state is None:
            return _result(False, f"键盘注入未生效: 文本框未出现「{CONTROL_TEXT}」", status=STATUS_FAIL)

        chars = [event for event in state["key_events"] if event.get("char")]
        latencies = [
            (float(event["received_ns"]) - float(sent_at[index])) / 1_000_000
            for index, event in enumerate(chars[-len(sent_at) :])
        ]

        injector.select_all()
        injector.type_text(CONTROL_REPLACED)
        replaced = _wait_state(state_path, lambda item: item.get("text") == CONTROL_REPLACED, 8.0)
        if replaced is None:
            return _result(False, "组合键 Ctrl+A 或替换输入未生效", status=STATUS_FAIL)

        canvas = ready["canvas"]
        injector.move_to(canvas["x"] + 5, canvas["y"] + 5)
        time.sleep(0.2)
        injector.move_to(canvas["x"] + canvas["w"] - 5, canvas["y"] + canvas["h"] - 5)
        time.sleep(0.2)
        injector.move_to(canvas["x"] + canvas["w"] // 2, canvas["y"] + canvas["h"] // 2)
        injector.move_relative(40, 20)
        injector.move_relative(-40, -20)
        injector.click(canvas["x"] + canvas["w"] // 2, canvas["y"] + canvas["h"] // 2)
        injector.click(canvas["x"] + canvas["w"] // 2 + 20, canvas["y"] + canvas["h"] // 2, right=True)
        injector.wheel()
        time.sleep(0.6)
        final = json.loads(state_path.read_text(encoding="utf-8"))
        mouse = final["mouse"]

        screenshot_path = work / "control-window.png"
        window = ready["window"]
        bbox = (window["x"], window["y"], window["x"] + window["w"], window["y"] + window["h"])
        screenshot_ok = False
        try:
            image = ImageGrab.grab(bbox=bbox, all_screens=True)
            image.save(screenshot_path)
            screenshot_ok = screenshot_path.stat().st_size > 0 and max(band[1] for band in image.getextrema()) > 0
        except OSError as error:
            screenshot_note = f"截图失败: {error}"
        else:
            screenshot_note = f"截图已存: {screenshot_path}"

        assertions = {
            "text_typed": state["text"] == CONTROL_TEXT,
            "combo_replace": final["text"] == CONTROL_REPLACED,
            "mouse_left": mouse["left"] >= 1,
            "mouse_right": mouse["right"] >= 1,
            "mouse_wheel": mouse["wheel"] >= 1,
            "mouse_motion": mouse["motion"] >= 1,
        }
        if not all(assertions.values()):
            return _result(False, "控制闭环断言未通过", status=STATUS_FAIL, assertions=assertions, mouse=mouse)

        return _result(
            True,
            "键鼠注入在靶窗口实际生效, 文本框内容与进程级断言一致",
            assertions=assertions,
            typed=state["text"],
            replaced=final["text"],
            p50_ms=percentile(latencies, 0.50),
            p95_ms=percentile(latencies, 0.95),
            sample_count=len(latencies),
            mouse=mouse,
            screenshot=screenshot_note,
            screenshot_ok=screenshot_ok,
            evidence=str(work),
            note="本机输入注入闭环; 画面经网络回传未验证(无真实采集/编码/媒体链路), 不得当作远程控制验收",
        )
    finally:
        target.terminate()
        try:
            target.wait(timeout=5)
        except subprocess.TimeoutExpired:
            target.kill()


def _wait_state(path: Path, predicate: Any, timeout: float) -> dict[str, Any] | None:
    """轮询状态文件直到满足条件或超时."""
    deadline = time.time() + timeout
    latest: dict[str, Any] | None = None
    while time.time() < deadline:
        try:
            latest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 靶进程写状态文件非原子, 读到半截或占用时重试, 不当作失败
            time.sleep(0.05)
            continue
        if predicate(latest):
            return latest
        time.sleep(0.05)
    return None


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


WM_CLOSE = 0x0010
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
EDITOR_TEXT = "RDC-EDITOR-2026-OK"


def _read_clipboard_text() -> str | None:
    """读取系统剪贴板文本; 无内容或占用时返回 None."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(pointer)
    finally:
        user32.CloseClipboard()


def _write_clipboard_text(text: str) -> None:
    """写回系统剪贴板文本, 失败时保持原状."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    payload = text.encode("utf-16-le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        return
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        return
    ctypes.memmove(pointer, payload, len(payload))
    kernel32.GlobalUnlock(pointer)
    if not user32.OpenClipboard(None):
        return
    try:
        user32.EmptyClipboard()
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()


def _find_window_by_title(keyword: str) -> int | None:
    """按标题关键字查找可见顶层窗口句柄."""
    user32 = ctypes.windll.user32
    matches: list[int] = []

    def visit(hwnd: int, _param: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if keyword in buffer.value:
            matches.append(int(hwnd))
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(callback(visit), 0)
    return matches[0] if matches else None


def _force_foreground(hwnd: int) -> bool:
    """把窗口切到前台; 拿不到焦点返回 False, 调用方不得继续注入."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.ShowWindow(hwnd, 9)
    user32.AllowSetForegroundWindow(0xFFFFFFFF)
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return True
    foreground = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
    current_thread = kernel32.GetCurrentThreadId()
    if foreground_thread:
        user32.AttachThreadInput(current_thread, foreground_thread, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(hwnd)
    if foreground_thread:
        user32.AttachThreadInput(current_thread, foreground_thread, False)
    time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd


def _wait_clipboard(expected: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _read_clipboard_text() == expected:
            return True
        time.sleep(0.05)
    return False


def _wait_file_text(path: Path, expected: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if path.read_text(encoding="utf-8") == expected:
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def check_control_editor(evidence_dir: Path) -> CommandResult:
    """真实编辑器写入固定字符串: 剪贴板、文件落盘与截图三重断言."""
    from PIL import ImageGrab

    if sys.platform != "win32":
        return _result(False, "编辑器控制取证只在 Windows 本机执行", status=STATUS_UNVERIFIED, reason="非 Windows")
    notepad = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "notepad.exe"
    if not notepad.is_file():
        return _result(False, "未找到记事本, 编辑器取证未执行", status=STATUS_UNVERIFIED, reason=str(notepad))

    work = evidence_dir / "control_editor"
    work.mkdir(parents=True, exist_ok=True)
    target = work / f"rdc-editor-{os.getpid()}.txt"
    target.write_text("", encoding="utf-8")
    original = _read_clipboard_text()
    process = subprocess.Popen([str(notepad), str(target)])
    window: int | None = None
    try:
        deadline = time.time() + 15
        while time.time() < deadline and window is None:
            window = _find_window_by_title(target.name)
            if window is None:
                time.sleep(0.2)
        if window is None:
            return _result(False, "记事本窗口未在 15 秒内出现", status=STATUS_FAIL, target=str(target))
        if not _force_foreground(window):
            return _result(
                False,
                "记事本无法取得前台焦点, 为避免误输入未执行注入",
                status=STATUS_UNVERIFIED,
                window=window,
                target=str(target),
            )
        time.sleep(0.5)
        injector = Injector()
        injector.type_text(EDITOR_TEXT)
        time.sleep(0.3)
        injector.select_all()
        injector.copy()
        clipboard_ok = _wait_clipboard(EDITOR_TEXT, 5.0)
        injector.save()
        file_ok = _wait_file_text(target, EDITOR_TEXT, 5.0)

        rect = _Rect()
        ctypes.windll.user32.GetWindowRect(window, ctypes.byref(rect))
        screenshot = work / "editor-window.png"
        screenshot_ok = False
        try:
            image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
            image.save(screenshot)
            screenshot_ok = screenshot.stat().st_size > 0
        except OSError:
            pass

        text_ok = target.read_text(encoding="utf-8") == EDITOR_TEXT
        assertions = {
            "clipboard_copy": clipboard_ok,
            "file_saved": file_ok,
            "file_content": text_ok,
            "screenshot": screenshot_ok,
        }
        if not (clipboard_ok and file_ok and text_ok):
            return _result(
                False,
                "编辑器写入未在剪贴板或文件落盘生效",
                status=STATUS_FAIL,
                assertions=assertions,
                target=str(target),
            )
        return _result(
            True,
            "记事本实际收到注入文本, Ctrl+C 与 Ctrl+S 均验证一致",
            status=STATUS_PASS,
            assertions=assertions,
            text=EDITOR_TEXT,
            target=str(target),
            screenshot=str(screenshot),
            note="本机输入注入到真实编辑器并落盘; 无远程通道与画面回传, 不替代跨端验收",
        )
    finally:
        if window is not None:
            ctypes.windll.user32.PostMessageW(window, WM_CLOSE, 0, 0)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
        if original is not None:
            _write_clipboard_text(original)


# ---------------------------------------------------------------------------- 剪贴板与传输


def check_clipboard() -> CommandResult:
    """文本双向、防环回、图片与状态不含明文."""
    service = ClipboardSyncService()
    clipboard.configure(service)
    clipboard.set_clipboard_enabled(True)

    text = "剪贴板冒烟：双向同步 2026"
    pushed = clipboard.push_clipboard("desktop", "text", text)
    pulled = clipboard.pull_clipboard("android")
    loop = clipboard.pull_clipboard("desktop")
    image = clipboard.push_clipboard("android", "image", base64.b64encode(b"fake-png-bytes").decode("ascii"))
    status = clipboard.get_clipboard_status()

    ok = (
        pushed.ok
        and pulled.ok
        and pulled.data is not None
        and pulled.data.get("text") == text
        and loop.ok
        and loop.data is not None
        and loop.data.get("applied") is False
        and image.ok
        and status.ok
        and status.data is not None
        and "text" not in status.data
    )
    if not ok:
        return _result(False, "剪贴板双向或防环回未通过", status=STATUS_FAIL)
    return _result(
        True,
        "文本上行→对端拉取一致; 来源端回拉被抑制; 图片通过",
        text=pulled.data["text"],
        loop_suppressed=loop.data["applied"] is False,
        image_digest=image.data["digest"][:16] if image.data else None,
        limits={"text": status.data["text_limit_bytes"], "image": status.data["image_limit_bytes"]},
        note="服务层双向; 未接系统剪贴板与真实跨端通道",
    )


def check_clipboard_dedup_v13() -> CommandResult:
    """契约 v1.3: 同来源同摘要抑制环回, 跨来源同摘要按重复推送抑制."""
    from rdc.core import errors

    service = ClipboardSyncService()
    clipboard.configure(service)
    clipboard.set_clipboard_enabled(True)
    text = "RDC dedup 2026"

    first = clipboard.push_clipboard("desktop", "text", text)
    loop = clipboard.push_clipboard("desktop", "text", text)
    duplicate = clipboard.push_clipboard("android", "text", text)
    pulled = clipboard.pull_clipboard("desktop")
    status = clipboard.get_clipboard_status()

    checks: dict[str, bool] = {
        "first_applied": bool(first.ok and first.data and first.data.get("applied") is True),
        "same_origin_loop": bool(
            loop.ok and loop.data and loop.data.get("applied") is False and loop.data.get("reason") == "loop"
        ),
        "cross_origin_duplicate": bool(
            duplicate.ok
            and duplicate.data
            and duplicate.data.get("applied") is False
            and duplicate.data.get("reason") == "duplicate"
        ),
        "pull_loop_suppressed": bool(
            pulled.ok and pulled.data and pulled.data.get("applied") is False and pulled.data.get("reason") == "loop"
        ),
        "counters": bool(
            status.ok and status.data and status.data.get("pushes") == 1 and status.data.get("suppressed") == 2
        ),
    }

    checks["scope_invalid"] = service.set_scope("admin").error_code == errors.SCOPE_INVALID
    view_scope = service.set_scope("view")
    blocked = clipboard.push_clipboard("desktop", "text", "blocked by view")
    checks["scope_insufficient"] = bool(view_scope.ok and blocked.error_code == errors.SCOPE_INSUFFICIENT)
    service.set_scope("control")

    failed = _failed_names(checks)
    if failed:
        return _result(False, "剪贴板去重/环回/授权检查未通过: " + ", ".join(failed), checks=checks)
    return _result(True, f"去重与环回抑制 {len(checks)} 项符合契约", checks=checks)


def check_transfer(evidence_dir: Path) -> CommandResult:
    """真实文件落盘: 拖拽等价传输、目录递归、暂停续传、哈希一致."""
    service = FileTransferService()
    transfer.configure(service)
    work = evidence_dir / "transfer"
    if work.exists():
        shutil.rmtree(work)
    (work / "src").mkdir(parents=True)
    (work / "dst").mkdir(parents=True)

    payload = hashlib.blake2b(b"rdc-smoke", digest_size=64).digest()
    single = work / "src" / "single.bin"
    single.write_bytes((payload * (512 * 1024 // len(payload) + 1))[: 512 * 1024])

    started = transfer.start_transfer(str(single), str(work / "dst"))
    if not started.ok or not started.data:
        return _result(False, f"传输启动失败: {started.message}", status=STATUS_FAIL)
    first_job = str(started.data["job_id"])
    progress = transfer.get_transfer_progress(first_job)
    copied = work / "dst" / "single.bin"
    if not (progress.ok and copied.is_file() and _sha256(copied) == _sha256(single)):
        return _result(False, "单文件传输哈希不一致", status=STATUS_FAIL)

    tree = work / "src" / "tree"
    (tree / "nested").mkdir(parents=True)
    (tree / "a.txt").write_text("a" * 1000, encoding="utf-8")
    (tree / "nested" / "b.txt").write_text("b" * 2000, encoding="utf-8")
    tree_started = transfer.start_transfer(str(tree), str(work / "dst" / "tree"))
    if not tree_started.ok or not tree_started.data:
        return _result(False, "目录传输启动失败", status=STATUS_FAIL)
    transfer.get_transfer_progress(str(tree_started.data["job_id"]))
    tree_ok = (work / "dst" / "tree" / "nested" / "b.txt").is_file() and (
        (work / "dst" / "tree" / "a.txt").read_text(encoding="utf-8") == "a" * 1000
    )

    big = work / "src" / "resume.bin"
    big.write_bytes((payload * (3 * 1024 * 1024 // len(payload) + 1))[: 3 * 1024 * 1024])
    resume_started = transfer.start_transfer(str(big), str(work / "dst"))
    if not resume_started.ok or not resume_started.data:
        return _result(False, "续传任务启动失败", status=STATUS_FAIL)
    resume_job = str(resume_started.data["job_id"])
    transfer.get_transfer_progress(resume_job)
    paused = transfer.pause_transfer(resume_job)
    paused_bytes = int(paused.data["sent_bytes"]) if paused.data else -1
    resumed = transfer.resume_transfer(resume_job)
    for _ in range(30):
        current = transfer.get_transfer_progress(resume_job)
        if current.data and current.data["state"] in ("completed", "failed"):
            break
    resumed_file = work / "dst" / "resume.bin"
    resume_ok = (
        paused.ok
        and resumed.ok
        and 0 < paused_bytes < 3 * 1024 * 1024
        and resumed_file.is_file()
        and _sha256(resumed_file) == _sha256(big)
    )
    if not (tree_ok and resume_ok):
        return _result(
            False,
            "目录递归或断点续传未通过",
            status=STATUS_FAIL,
            tree_ok=tree_ok,
            resume_ok=resume_ok,
            paused_bytes=paused_bytes,
        )
    return _result(
        True,
        "单文件哈希一致, 目录递归保留结构, 暂停后续传完成且哈希一致",
        status=STATUS_SIMULATED,
        sample_sha256=_sha256(copied),
        tree_ok=tree_ok,
        paused_bytes=paused_bytes,
        total_bytes=3 * 1024 * 1024,
        channel="low_priority",
        evidence=str(work),
        note="本机目标目录等价; 真实远端被控端与跨网限速未验证",
    )


def check_transfer_recovery_v16(evidence_dir: Path) -> CommandResult:
    """契约 48-49: 断点恢复为 paused 后续传, 无待恢复返回 0, scope 两条失败路径."""
    from rdc.core import errors

    work = evidence_dir / "transfer_recovery"
    if work.exists():
        shutil.rmtree(work)
    source_dir = work / "src"
    destination = work / "dst"
    source_dir.mkdir(parents=True)
    destination.mkdir(parents=True)
    payload = (hashlib.blake2b(b"rdc-recover", digest_size=64).digest() * (2 * 1024 * 1024 // 64 + 1))[
        : 2 * 1024 * 1024
    ]
    source_file = source_dir / "recover.bin"
    source_file.write_bytes(payload)
    original = _sha256(source_file)

    service = FileTransferService(poll_budget_bytes=128 * 1024)
    transfer.configure(service)
    started = transfer.start_transfer(str(source_file), str(destination))
    if not started.ok or not started.data:
        return _result(False, f"恢复用例启动失败: {started.message}", status=STATUS_FAIL)
    job_id = str(started.data["job_id"])
    transfer.get_transfer_progress(job_id)
    paused = transfer.pause_transfer(job_id)
    paused_bytes = int(paused.data["sent_bytes"]) if paused.data else -1

    restarted = FileTransferService(poll_budget_bytes=128 * 1024)
    transfer.configure(restarted)
    recovered = transfer.recover_transfers(str(destination))
    recovered_items = (recovered.data or {}).get("items", []) if recovered.ok else []
    recovered_item = next((item for item in recovered_items if item.get("job_id") == job_id), {})
    required_fields = {
        "job_id",
        "source",
        "destination",
        "state",
        "sent_bytes",
        "total_bytes",
        "percent",
        "rate_limit_kbps",
        "channel",
    }

    resumed = transfer.resume_transfer(job_id)
    final_state: dict[str, Any] = {}
    for _ in range(60):
        current = transfer.get_transfer_progress(job_id)
        final_state = dict(current.data or {})
        if final_state.get("state") in ("completed", "failed"):
            break
    target_file = destination / "recover.bin"

    checks: dict[str, bool] = {
        "paused_midway": bool(paused.ok and 0 < paused_bytes < len(payload)),
        "recovered_one": bool(recovered.ok and recovered.data and int(recovered.data.get("recovered", 0)) == 1),
        "recovered_paused": bool(
            recovered_item.get("state") == "paused" and int(recovered_item.get("sent_bytes", -1)) == paused_bytes
        ),
        "recovered_item_fields": required_fields <= set(recovered_item),
        "resumed_completed": bool(resumed.ok and final_state.get("state") == "completed"),
        "hash_matches": bool(target_file.is_file() and _sha256(target_file) == original),
    }
    empty = transfer.recover_transfers(str(destination))
    checks["no_pending_zero"] = bool(empty.ok and empty.data and int(empty.data.get("recovered", -1)) == 0)

    invalid = transfer.set_transfer_scope("admin")
    checks["scope_invalid"] = invalid.error_code == errors.SCOPE_INVALID
    view_scope = transfer.set_transfer_scope("view")
    view_start = transfer.start_transfer(str(source_file), str(destination))
    view_recover = transfer.recover_transfers(str(destination))
    checks["scope_insufficient_start"] = bool(view_scope.ok and view_start.error_code == errors.SCOPE_INSUFFICIENT)
    checks["scope_insufficient_recover"] = view_recover.error_code == errors.SCOPE_INSUFFICIENT
    transfer.set_transfer_scope("control")

    failed = _failed_names(checks)
    if failed:
        return _result(False, "传输恢复或授权范围检查未通过: " + ", ".join(failed), checks=checks)
    return _result(
        True,
        f"断点恢复→续传完成, 无任务返回 0, scope 越权/非法 {len(checks)} 项符合契约",
        status=STATUS_PASS,
        checks=checks,
        job_id=job_id,
        paused_bytes=paused_bytes,
        total_bytes=len(payload),
        note="本机进程内重建服务模拟重启; 未做真实进程重启与跨端恢复",
    )


def check_session_plane_messages_v13() -> CommandResult:
    """protocol-v1 v1.3: 9 类会话数据面消息正/负向与传输顺序异常."""
    from rdc.core import errors
    from rdc.signaling.messages import SESSION_PLANE_TYPES, required_payload_fields
    from rdc.signaling.server import RelayGrantRecord

    server = InMemorySignalingServer()
    alice, bob = DeviceIdentity.generate(), DeviceIdentity.generate()
    if _authenticate(server, alice, "alice") is None or _authenticate(server, bob, "bob") is None:
        return _result(False, "会话数据面用例认证失败", status=STATUS_FAIL)

    session_id = "smoke-plane-session"
    joined = server.handle(
        build_envelope(
            bob,
            "candidate",
            {
                "session_id": session_id,
                "from_device_id": bob.device_id,
                "to_device_id": alice.device_id,
                "kind": "host",
                "address": "127.0.0.1",
                "port": 5000,
                "priority": 1,
            },
            session_id=session_id,
        ),
        device_id=bob.device_id,
    )
    if not joined:
        return _result(False, "会话参与者加入失败", status=STATUS_FAIL)

    content = b"chunk!"
    digest = hashlib.sha256(content).hexdigest()
    text_digest = hashlib.sha256(b"hello").hexdigest()

    def send(msg_type: str, body: dict[str, Any], sid: str | None = session_id) -> list[dict[str, Any]]:
        return server.handle(build_envelope(alice, msg_type, body, session_id=sid), device_id=alice.device_id)

    def code_of(responses: list[dict[str, Any]]) -> str:
        for response in responses:
            body = response.get("payload")
            if isinstance(body, dict) and body.get("code"):
                return str(body["code"])
        return ""

    def routed(responses: list[dict[str, Any]], msg_type: str) -> bool:
        return bool(responses and responses[0].get("type") == msg_type and not code_of(responses))

    def body(msg_type: str, transfer_id: str = "t-main", **overrides: Any) -> dict[str, Any]:
        payloads: dict[str, dict[str, Any]] = {
            "file_offer": {
                "session_id": session_id,
                "transfer_id": transfer_id,
                "items": [{"relative": "a.bin", "size": len(content), "sha256": digest}],
                "total_bytes": len(content),
                "chunk_size": 262144,
                "digest_alg": "sha256",
                "digest": digest,
            },
            "file_accept": {"session_id": session_id, "transfer_id": transfer_id, "accepted_items": ["a.bin"]},
            "file_chunk": {
                "session_id": session_id,
                "transfer_id": transfer_id,
                "file_index": 0,
                "offset": 0,
                "length": len(content),
                "sha256": digest,
                "data": base64.b64encode(content).decode("ascii"),
            },
            "file_ack": {
                "session_id": session_id,
                "transfer_id": transfer_id,
                "file_index": 0,
                "acked_bytes": len(content),
            },
            "file_complete": {"session_id": session_id, "transfer_id": transfer_id, "digest": digest},
            "file_abort": {"session_id": session_id, "transfer_id": transfer_id, "reason": "smoke"},
            "file_result": {
                "session_id": session_id,
                "transfer_id": transfer_id,
                "verified": True,
                "digest": digest,
            },
            "clipboard_update": {
                "session_id": session_id,
                "kind": "text",
                "size_bytes": 5,
                "digest": text_digest,
                "content": "hello",
            },
            "clipboard_request": {"session_id": session_id, "kind": "text"},
            "clipboard_ack": {"session_id": session_id, "kind": "text", "digest": text_digest, "applied": True},
        }
        payloads[msg_type].update(overrides)
        return payloads[msg_type]

    checks: dict[str, bool] = {}
    checks["offer_routed"] = routed(send("file_offer", body("file_offer")), "file_offer")
    checks["accept_routed"] = routed(send("file_accept", body("file_accept")), "file_accept")
    checks["chunk_routed"] = routed(send("file_chunk", body("file_chunk")), "file_chunk")
    checks["ack_routed"] = routed(send("file_ack", body("file_ack")), "file_ack")
    checks["complete_routed"] = routed(send("file_complete", body("file_complete")), "file_complete")
    checks["complete_then_chunk_rejected"] = (
        code_of(send("file_chunk", body("file_chunk"))) == errors.FILE_CHUNK_INVALID
    )

    checks["offer_order_routed"] = routed(send("file_offer", body("file_offer", transfer_id="t-order")), "file_offer")
    checks["chunk_before_accept_rejected"] = (
        code_of(send("file_chunk", body("file_chunk", transfer_id="t-order"))) == errors.FILE_CHUNK_INVALID
    )
    checks["ack_before_accept_rejected"] = (
        code_of(send("file_ack", body("file_ack", transfer_id="t-order"))) == errors.FILE_CHUNK_INVALID
    )
    checks["accept_order_routed"] = routed(
        send("file_accept", body("file_accept", transfer_id="t-order")), "file_accept"
    )
    checks["offset_gap_rejected"] = (
        code_of(send("file_chunk", body("file_chunk", transfer_id="t-order", offset=1))) == errors.FILE_CHUNK_INVALID
    )
    checks["abort_routed"] = routed(send("file_abort", body("file_abort", transfer_id="t-order")), "file_abort")
    checks["chunk_after_abort_rejected"] = (
        code_of(send("file_chunk", body("file_chunk", transfer_id="t-order"))) == errors.FILE_CHUNK_INVALID
    )

    checks["clipboard_update_routed"] = routed(send("clipboard_update", body("clipboard_update")), "clipboard_update")
    checks["clipboard_duplicate_suppressed"] = send("clipboard_update", body("clipboard_update")) == []
    checks["clipboard_digest_mismatch"] = (
        code_of(send("clipboard_update", body("clipboard_update", digest="1" * 64))) == errors.CLIPBOARD_DIGEST_MISMATCH
    )
    checks["clipboard_kind_rejected"] = (
        code_of(send("clipboard_update", body("clipboard_update", kind="file"))) == errors.CLIPBOARD_UNSUPPORTED_KIND
    )
    checks["clipboard_request_routed"] = routed(
        send("clipboard_request", body("clipboard_request")), "clipboard_request"
    )
    checks["clipboard_ack_routed"] = routed(send("clipboard_ack", body("clipboard_ack")), "clipboard_ack")

    view_session = "smoke-view-session"
    server.relay_grants["smoke-view-token"] = RelayGrantRecord(
        session_id=view_session, budget_kbps=1000, expires_at=time.time() + 60, scope="view"
    )
    checks["view_scope_rejected"] = (
        code_of(send("file_offer", body("file_offer", session_id=view_session), sid=view_session))
        == errors.SCOPE_INSUFFICIENT
    )

    missing_ok = True
    for msg_type in sorted(SESSION_PLANE_TYPES):
        incomplete = body(msg_type)
        incomplete.pop(required_payload_fields(msg_type)[-1], None)
        server.errors.clear()
        responses = send(msg_type, incomplete)
        missing_ok = missing_ok and not responses and any(item.startswith("MISSING_FIELD") for item in server.errors)
    checks["missing_field_all_nine"] = missing_ok

    checks["empty_session_id_rejected"] = (
        code_of(send("file_offer", body("file_offer", session_id=""))) == errors.FILE_CHUNK_INVALID
    )

    failed = _failed_names(checks)
    if failed:
        return _result(False, "会话数据面 9 类消息检查未通过: " + ", ".join(failed), checks=checks)
    return _result(
        True,
        f"9 类会话数据面消息正/负向与顺序异常共 {len(checks)} 项符合契约",
        checks=checks,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------- TLS 与未验证项


def check_tls_local() -> CommandResult:
    """TLS 1.3 本地等价: 单向转发、双向校验拒绝/通过、错误 CA 拒绝."""
    cert_dir = _build_root() / "certs" / "relay"
    if not (cert_dir / "ca.crt").is_file():
        return _result(True, "本地证书缺失, TLS 等价测试未执行", status=STATUS_UNVERIFIED, reason=str(cert_dir))
    from poc.tools import run_relay_tls_check

    result = run_relay_tls_check.run()
    scenarios = {
        name: {"ok": bool(item.get("ok")), "tls": item.get("tls_version"), "reason": item.get("reason")}
        for name, item in result.get("scenarios", {}).items()
    }
    if not result.get("all_passed"):
        return _result(
            False,
            "TLS 1.3 本地场景未全部通过",
            status=STATUS_FAIL,
            scenarios=scenarios,
            defect="双令牌同会话场景发布端 relay_token 被对端申请吊销, 握手前连接关闭",
            repro=(
                "两个 DeviceIdentity 对同一 session_id 各取令牌后, 用第一个令牌发起 relay_hello 被 TOKEN_INVALID 拒绝"
            ),
            owner="信令主线 / relay",
        )
    return _result(
        True,
        "TLS 1.3 单向/双向校验与错误 CA 拒绝均符合预期",
        scenarios=scenarios,
        note="本机回环等价; 未跨公网, 不是画质通道验收; 双向校验需分发客户端证书, 取舍见启动说明",
    )


def check_remote_input(evidence_dir: Path) -> CommandResult:
    """真实 TCP 信令与中继数据面的远程输入链路: 独立控制端进程驱动被控端记事本取证."""
    script = PROJECT_ROOT / "scripts" / "e2e_remote_control.py"
    target = PROJECT_ROOT / "evidence" / "smoke" / evidence_dir.name / "remote_input"
    target.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    parts = [str(PROJECT_ROOT / "src"), str(PROJECT_ROOT / "scripts"), str(PROJECT_ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    try:
        process = subprocess.run(
            [sys.executable, str(script), "--evidence", str(target)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return _result(False, "端到端远程输入验证 180 秒未完成", evidence=str(target))
    report_path = target / "e2e.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return _result(
            False,
            f"端到端验证未产出可读证据: {error}",
            evidence=str(target),
            stderr=(process.stderr or "")[-1500:],
        )
    status = str(report.get("status", STATUS_FAIL))
    latency = report.get("latency") or {}
    one_way = latency.get("one_way") or {}
    injection = latency.get("injection") or {}
    detail = {
        "evidence": str(target),
        "assertions": report.get("assertions"),
        "latency": latency,
        "e2e_items": report.get("items"),
        "stdout": (process.stdout or "")[-2000:],
    }
    if process.returncode == 0 and status == STATUS_PASS:
        return _result(
            True,
            "真实 TCP 信令+中继送达并写入记事本, 剪贴板/文件/截图三方一致; "
            f"单向 p50 {one_way.get('p50')}ms/p95 {one_way.get('p95')}ms, "
            f"注入 p50 {injection.get('p50')}ms/p95 {injection.get('p95')}ms",
            **detail,
        )
    reason = str(report.get("reason") or "端到端验证未通过")
    if status == STATUS_UNVERIFIED:
        return _result(
            True,
            f"远程输入链路未验证: {reason}",
            status=STATUS_UNVERIFIED,
            reason=reason,
            **detail,
        )
    return _result(False, f"远程输入链路失败: {reason}", **detail)


def add_unverified(runner: SmokeRunner) -> None:
    """无法在本机完成的项目, 保留阻塞原因, 不计为通过."""
    runner.add(
        "hardware_4k60",
        "4K60 硬件编码端到端",
        STATUS_UNVERIFIED,
        "本机 GT 710 无 NVENC、X99 无核显, 无硬件编码器; 只有软编参考值 26.64fps",
        reason="无硬件编码器; 软编不能承载 4K60, 参考值非实测通过",
    )
    runner.add(
        "ecs_deploy",
        "ECS 真实部署与 systemd 启动",
        STATUS_UNVERIFIED,
        "缺 ECS 登录方式; 未改服务器网络与 22 端口",
        reason="需用户提供登录方式并授权部署窗口",
    )
    runner.add(
        "vm_control_client",
        "虚拟机控制端(Ubuntu 22.04)",
        STATUS_UNVERIFIED,
        "现有虚机无账号口令且 22 端口不通",
        reason="缺 guest 凭据; 未安装控制端",
    )
    runner.add(
        "android_real_device",
        "Android 真机安装与横竖屏",
        STATUS_UNVERIFIED,
        "本机无 arm64 真机; x86_64 主机不支持 arm64 AVD, 当前无可用 AVD",
        reason="保留真机补测项",
    )
    runner.add(
        "remote_media_return",
        "远端画面经网络回传",
        STATUS_UNVERIFIED,
        "无真实采集/编码/媒体传输链路, 输入已走本机 TCP 信令与中继, 画面回传仍缺",
        reason="不得宣称画质通道验收",
    )
    runner.add(
        "clipboard_system_bridge",
        "系统剪贴板与真实跨端通道",
        STATUS_UNVERIFIED,
        "剪贴板服务层双向通过, 未接 OS 剪贴板与真实对端",
        reason="界面已具备开关与状态, 通路未接",
    )


# ---------------------------------------------------------------------------- 汇总与入口


def _print_report(runner: SmokeRunner, evidence: Path, elapsed: float) -> None:
    _emit("")
    _emit("=" * 108)
    _emit(f"V1 冒烟结果  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  用时 {elapsed:.1f}s")
    _emit("=" * 108)
    _emit(f"{'#':<3}{'项目':<34}{'结论':<12}说明")
    _emit("-" * 108)
    for index, item in enumerate(runner.items, start=1):
        message = item.message.replace("\n", " ")
        if len(message) > 62:
            message = message[:59] + "..."
        _emit(f"{index:<3}{item.title:<34}{item.status:<12}{message}")
    _emit("-" * 108)
    counts = {
        status: sum(1 for item in runner.items if item.status == status)
        for status in (STATUS_PASS, STATUS_SIMULATED, STATUS_UNVERIFIED, STATUS_FAIL)
    }
    _emit(
        f"合计 {len(runner.items)} 项: 已实测通过 {counts[STATUS_PASS]}, 模拟口径 {counts[STATUS_SIMULATED]}, "
        f"未验证 {counts[STATUS_UNVERIFIED]}, 失败 {counts[STATUS_FAIL]}"
    )
    _emit(f"证据: {evidence}")
    _emit("口径说明: 「已实测通过」只代表本机实测范围; 模拟口径与非 Windows/模拟器/本机等价不等同验收.")
    _emit("缺口: 4K60 硬编、ECS 部署、虚拟机控制端、Android 真机、远端画面回传、系统剪贴板桥接 均未验证.")
    _emit(
        "控制闭环口径: 本机 SendInput 注入 + 独立进程经真实 TCP 信令/中继数据面送达输入; "
        "无画面回传, 不替代非本机跨端控制验收."
    )


def main(argv: list[str] | None = None) -> int:
    del argv
    started = time.perf_counter()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence = _build_root() / "evidence" / "smoke" / stamp
    evidence.mkdir(parents=True, exist_ok=True)
    os.environ["RDC_BUILD_ROOT"] = str(_build_root())
    os.environ.setdefault("RDC_TRANSFER_DIR", str(evidence / "inbox"))

    runner = SmokeRunner(evidence)
    runner.execute("environment", "依赖与配置自检", check_environment)
    runner.execute("commands", "命令层可调用", check_commands)
    runner.execute("signaling", "信令注册/挑战/认证/路由", check_signaling)
    runner.execute("session", "会话建立与断开重连", check_session_reconnect, simulated=True)
    runner.execute("media", "会话媒体帧收发(密文帧)", check_media_frames, simulated=True)
    runner.execute("ui", "桌面界面壳(离屏七页)", check_ui_shell, simulated=True)
    runner.execute("android", "Android APK(模拟器口径)", check_android_apk, simulated=True)
    runner.execute("logging", "日志字段与脱敏抽检", lambda: check_logging_redaction(evidence))
    runner.execute("errors", "异常路径错误码(12 条)", check_error_paths)
    runner.execute("grants", "授权转授与越界(41-42)", check_grants_v15)
    runner.execute("revocation", "登出与吊销即时失效(43-44)", check_account_revocation)
    runner.execute("wall_v15", "聚焦释放/审计/预览令牌(45-47)", check_wall_v15)
    runner.execute("wall_audit_perms", "墙审计权限(未登录/非所有者/未接线)", check_wall_audit_permissions)
    runner.execute("presence", "在线三态与 30s/90s 心跳", check_presence)
    control = runner.execute("control", "能控制闭环(输入实际生效)", lambda: check_control_loop(evidence))
    runner.execute("control_editor", "编辑器真实写入取证(记事本)", lambda: check_control_editor(evidence))
    runner.execute("remote_input", "远程输入链路(真实TCP信令+中继)", lambda: check_remote_input(evidence))
    runner.execute("clipboard", "剪贴板双向同步", check_clipboard)
    runner.execute("clipboard_dedup", "剪贴板去重与环回抑制", check_clipboard_dedup_v13)
    runner.execute("transfer", "拖拽/续传/哈希一致(本机等价)", lambda: check_transfer(evidence), simulated=True)
    runner.execute("transfer_recovery", "传输恢复与授权范围(48-49)", lambda: check_transfer_recovery_v16(evidence))
    runner.execute("session_plane", "会话数据面 9 类消息(正/负向)", check_session_plane_messages_v13)
    runner.execute("tls", "中继 TLS 1.3 本地等价", check_tls_local)
    add_unverified(runner)

    elapsed = time.perf_counter() - started
    failures = [item for item in runner.items if item.status == STATUS_FAIL]
    control_ok = control.ok
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": round(elapsed, 2),
        "control_loop_passed": control_ok,
        "editor_control_passed": any(
            item.key == "control_editor" and item.status == STATUS_PASS for item in runner.items
        ),
        "counts": {
            status: sum(1 for item in runner.items if item.status == status)
            for status in (STATUS_PASS, STATUS_SIMULATED, STATUS_UNVERIFIED, STATUS_FAIL)
        },
        "items": [
            {
                "key": item.key,
                "title": item.title,
                "status": item.status,
                "message": item.message,
                "detail": item.detail,
            }
            for item in runner.items
        ],
    }
    (evidence / "smoke.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_report(runner, evidence, elapsed)
    if failures or not control_ok:
        _emit(f"结果: 失败({len(failures)} 项; 控制闭环={'通过' if control_ok else '未通过'})")
        return 1
    _emit("结果: 通过(未验证项不冒充通过)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
