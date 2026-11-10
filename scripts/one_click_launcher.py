# ==========================================================================================
# 文件        : one_click_launcher.py
# 所属目录    : scripts/
# 文件用途    : Windows 一键演示入口: 通过 SSH 启动 Ubuntu 被控端, 读取本次设备 ID 与连接码,
#               自动填入控制端并立即连接, 窗口留在桌面供直接操作
# 架构定位    : 演示外壳, 复用 rdc_control_app 的控制端窗口与既有信令/中继/加密实现
# 核心功能    : 读取本机演示配置 → 启动被控端 → 解析启动横幅 → 自动连接 → 窗口关闭时回收被控端
# 职责边界    : 不把连接码写入仓库文件/日志/证据; 不做免鉴权; 仅用于本机内网演示
# ==========================================================================================

"""Windows 一键演示入口.

用法(双击根目录 启动演示.bat, 无需任何参数):
    python scripts/one_click_launcher.py

本机演示配置保存在仓库之外: %USERPROFILE%\\.rdc-demo\\host.txt
    target=user@192.168.37.132
    key=D:\\path\\to\\id_ed25519
    ssh_port=22022

连接码只在启动横幅里读取一次, 直接交给控制端窗口, 不落盘.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from rdc_control_app import ControllerWindow

SSH_TIMEOUT_S = 35.0
ADDRESS_RE = re.compile(r"监听地址\s*:\s*([0-9.]+):(\d+)")
DEVICE_RE = re.compile(r"设备\s*ID\s*:\s*([A-Za-z0-9]+)")
CODE_RE = re.compile(r"连接码\s*:\s*(\d{4,8})")
REMOTE_COMMAND = "pkill -f '[r]dc_host_linux.py'; sleep 0.5; cd $HOME/rdc/app && exec bash scripts/start-host.sh"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def config_path() -> Path:
    """演示配置路径; 可用 RDC_DEMO_CONFIG 覆盖."""
    override = os.environ.get("RDC_DEMO_CONFIG", "")
    if override:
        return Path(override)
    return Path.home() / ".rdc-demo" / "host.txt"


def load_config() -> dict[str, str]:
    """读取 key=value 形式的演示配置; 缺失时给出可读错误."""
    path = config_path()
    if not path.is_file():
        raise RuntimeError(f"缺少本机演示配置: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    if not values.get("target"):
        raise RuntimeError(f"演示配置缺少 target: {path}")
    return values


def build_ssh_command(config: dict[str, str]) -> list[str]:
    """拼出非交互 SSH 命令: 先清理旧被控端, 再前台启动新的."""
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=NUL",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "LogLevel=ERROR",
        "-p",
        config.get("ssh_port", "22"),
    ]
    if config.get("key"):
        command += ["-i", config["key"]]
    command += [config["target"], REMOTE_COMMAND]
    return command


def _drain(stream: Any) -> None:
    """持续读空管道, 防止被控端写日志时阻塞; 内容直接丢弃."""
    if stream is None:
        return
    try:
        for _ in stream:
            pass
    except (OSError, ValueError):
        return


def _tail(stream: Any) -> str:
    """取错误输出的最后一行, 用于可读提示."""
    if stream is None:
        return ""
    try:
        text = stream.read()
    except (OSError, ValueError):
        return ""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return lines[-1] if lines else ""


class HostLauncher(QThread):
    """启动 Ubuntu 被控端并解析本次连接参数."""

    ready = Signal(str, int, str, str)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.process: subprocess.Popen[str] | None = None

    def run(self) -> None:
        try:
            self._launch()
        except (OSError, RuntimeError, ValueError) as error:
            self.failed.emit(str(error))

    def _launch(self) -> None:
        config = load_config()
        process = subprocess.Popen(
            build_ssh_command(config),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        self.process = process
        watchdog = threading.Timer(SSH_TIMEOUT_S, _terminate, args=(process,))
        watchdog.start()
        try:
            banner = self._read_banner(process)
        finally:
            watchdog.cancel()
        if banner is None:
            _terminate(process)
            raise RuntimeError(_explain_tail(_tail(process.stderr)))
        address, port, device_id, code = banner
        threading.Thread(target=_drain, args=(process.stdout,), daemon=True).start()
        threading.Thread(target=_drain, args=(process.stderr,), daemon=True).start()
        self.ready.emit(address, port, device_id, code)

    def _read_banner(self, process: subprocess.Popen[str]) -> tuple[str, int, str, str] | None:
        """从启动横幅里读出地址、设备 ID 与连接码; 读到失败提示立即返回 None."""
        address = ""
        port = 0
        device_id = ""
        code = ""
        if process.stdout is None:
            return None
        for line in process.stdout:
            found = ADDRESS_RE.search(line)
            if found:
                address = found.group(1)
                port = int(found.group(2))
            found = DEVICE_RE.search(line)
            if found:
                device_id = found.group(1)
            found = CODE_RE.search(line)
            if found:
                code = found.group(1)
            if "启动失败" in line:
                return None
            if address and port and device_id and code:
                return address, port, device_id, code
        return None


def _explain_tail(text: str) -> str:
    """把 SSH 的常见错误换成可读提示."""
    if not text:
        return "被控端没有在预期时间内就绪"
    if "Connection refused" in text or "Connection timed out" in text or "No route" in text:
        return "连不上 Ubuntu 虚机: 请确认虚机已开机, 且 SSH 服务可达"
    if "Permission denied" in text:
        return "SSH 登录被拒绝: 请检查本机演示配置里的账号与密钥"
    return text


def _terminate(process: subprocess.Popen[str]) -> None:
    """结束 SSH 会话, 连带停掉远端被控端."""
    if process.poll() is not None:
        return
    with contextlib.suppress(OSError):
        process.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(3)


def main(argv: list[str] | None = None) -> int:
    """打开控制端窗口, 后台启动被控端, 就绪后自动连接."""
    parser = argparse.ArgumentParser(description="Windows 一键演示入口")
    parser.add_argument("--config", default="", help="演示配置文件路径")
    arguments = parser.parse_args(argv)
    if arguments.config:
        os.environ["RDC_DEMO_CONFIG"] = arguments.config

    application = QApplication([sys.argv[0]])
    window = ControllerWindow()
    window.setWindowTitle("RDC 一键演示 · Windows 控制 Ubuntu")
    window.set_status_text("connecting", "正在启动 Ubuntu 被控端, 请稍候 …")
    window.connect_button.setEnabled(False)

    launcher = HostLauncher()

    def on_ready(address: str, port: int, device_id: str, code: str) -> None:
        window.setWindowTitle("RDC 一键演示 · Windows 控制 Ubuntu")
        window.start_session(address, port, device_id, code)

    def on_failed(message: str) -> None:
        window.set_status_text("failed", f"被控端启动失败: {message}")
        QMessageBox.critical(window, "一键演示", f"Ubuntu 被控端没有启动成功。\n\n{message}")
        application.quit()

    launcher.ready.connect(on_ready)
    launcher.failed.connect(on_failed)

    def on_quit() -> None:
        if launcher.process is not None:
            _terminate(launcher.process)

    application.aboutToQuit.connect(on_quit)
    window.show()
    launcher.start()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
