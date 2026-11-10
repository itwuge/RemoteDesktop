# ==========================================================================================
# 文件        : deploy.py
# 所属目录    : deploy/stun/
# 文件用途    : 一条命令部署极简 STUN 服务到 Ubuntu 20.04/22.04
# 架构定位    : 部署工具, 不属于产品运行时代码
# 核心功能    : 系统账号、目录、源码、systemd 单元、启用启动、本地 Binding 自检
# 职责边界    : 不改 sshd/22、不改防火墙与安全组、不重启主机、不写任何凭据
# 运行环境    : 目标机 Linux + systemd, Python 3.8+; 需 root
# 依赖关系    : stun_server.py、rdc-stun.service.tmpl
# ==========================================================================================

"""STUN 服务幂等部署脚本.

用法:
    sudo python3 deploy.py --port 3478 --python /opt/rdc-py312/bin/python3.12
    python3 deploy.py --dry-run --port 3478
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001


class DeployError(RuntimeError):
    """部署失败, 携带可执行的修复提示."""


class Deployer:
    """幂等部署器: 先比对现状, 再决定是否写入或重启."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.changed: list[str] = []
        self.unchanged: list[str] = []
        self.needs_restart = False

    def run(self) -> int:
        self._preflight()
        self._ensure_account()
        self._ensure_dirs()
        self._install_app()
        self._install_unit()
        self._start_service()
        self._self_check()
        self._report()
        return 0

    # ------------------------------------------------------------------ 步骤
    def _preflight(self) -> None:
        if self.args.dry_run:
            self._say("预检: dry-run 模式, 不写任何文件")
            for label, path in (
                ("服务源码", Path(__file__).parent / "stun_server.py"),
                ("systemd 模板", Path(__file__).parent / "rdc-stun.service.tmpl"),
            ):
                if not path.exists():
                    raise DeployError(f"{label}不存在: {path}")
                self._say(f"预检: {label}存在 -> {path}")
            return
        if os.geteuid() != 0:
            raise DeployError("需要 root 权限: 请用 sudo 重新执行")
        if not platform.system() == "Linux":
            raise DeployError("目标机必须是 Linux")
        self._require("systemctl", "未找到 systemctl, 目标机必须使用 systemd")
        if not self._python_ok(self.args.python):
            raise DeployError(f"解释器不可用或低于 3.8: {self.args.python}")

    def _ensure_account(self) -> None:
        if self._account_exists(self.args.user):
            self._unchanged(f"账号 {self.args.user} 已存在")
            return
        self._act(
            f"创建系统账号 {self.args.user}",
            [
                "useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin",
                "--home-dir", self.args.prefix, "--user-group", self.args.user,
            ],
        )

    def _ensure_dirs(self) -> None:
        path = Path(self.args.prefix) / "app"
        if path.is_dir():
            self._unchanged(f"目录已存在: {path}")
            return
        self._act(
            f"创建目录 {path}",
            ["install", "-d", "-m", "750", "-o", self.args.user, "-g", self.args.group, str(path)],
        )

    def _install_app(self) -> None:
        source = Path(__file__).parent / "stun_server.py"
        target = Path(self.args.prefix) / "app" / "stun_server.py"
        if target.exists() and self._sha256(source) == self._sha256(target):
            self._unchanged(f"服务源码未变化: {target}")
            return
        self._act(f"安装服务源码到 {target}", None)
        if self.args.dry_run:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o640)
        self._chown(target)
        self.changed.append(f"源码 {target}")
        self.needs_restart = True

    def _install_unit(self) -> None:
        template = (Path(__file__).parent / "rdc-stun.service.tmpl").read_text(encoding="utf-8")
        content = (
            template.replace("@USER@", self.args.user)
            .replace("@GROUP@", self.args.group)
            .replace("@PREFIX@", self.args.prefix)
            .replace("@PORT@", str(self.args.port))
            .replace("@PYTHON@", self.args.python)
        )
        unit = Path(self.args.unit)
        if unit.exists() and unit.read_text(encoding="utf-8") == content:
            self._unchanged(f"systemd 单元内容一致: {unit}")
            return
        self._act(f"写入 systemd 单元 {unit}", None)
        if self.args.dry_run:
            return
        unit.write_text(content, encoding="utf-8")
        unit.chmod(0o644)
        self.changed.append(f"单元 {unit}")
        self.needs_restart = True
        self._act("systemctl daemon-reload", ["systemctl", "daemon-reload"])

    def _start_service(self) -> None:
        enabled = self._query(["systemctl", "is-enabled", self.args.service]) == "enabled"
        active = self._query(["systemctl", "is-active", self.args.service]) == "active"
        if enabled and active and not self.needs_restart:
            self._unchanged("服务已启用并在运行, 无需重启")
            return
        self._act(f"启用并启动 {self.args.service}", ["systemctl", "enable", "--now", self.args.service])
        if not self.args.dry_run and active and self.needs_restart:
            self._act(f"重启 {self.args.service} 以加载新内容", ["systemctl", "restart", self.args.service])

    def _self_check(self) -> None:
        if self.args.dry_run:
            return
        target = ("127.0.0.1", self.args.port)
        request = struct.pack("!HHI", BINDING_REQUEST, 0, MAGIC_COOKIE) + os.urandom(12)
        mapped = None
        # 服务刚启动时端口可能未就绪, 最多等 10 秒
        for _ in range(20):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            try:
                sock.sendto(request, target)
                packet, _ = sock.recvfrom(1500)
                if len(packet) >= 20 and struct.unpack("!H", packet[:2])[0] == 0x0101:
                    mapped = packet
                    break
            except OSError:
                pass
            finally:
                sock.close()
            time.sleep(0.5)
        if mapped is None:
            raise DeployError("本地 Binding 自检失败; 查看日志: journalctl -u rdc-stun -n 50 --no-pager")
        print(f"自检: udp/{target[0]}:{target[1]} Binding Response {len(mapped)}B, XOR-MAPPED-ADDRESS 正常")

    # ------------------------------------------------------------------ 工具
    def _act(self, message: str, command: list[str] | None) -> None:
        prefix = "[dry-run] " if self.args.dry_run else ""
        print(f"{prefix}{message}")
        if command is not None and not self.args.dry_run:
            try:
                result = subprocess.run(command, capture_output=True, text=True, check=False)
            except OSError as error:
                raise DeployError(f"命令不可执行: {' '.join(command)}; {error}") from error
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                raise DeployError(f"命令失败: {' '.join(command)}; {detail}")

    def _say(self, message: str) -> None:
        print("[dry-run] " + message if self.args.dry_run else message)

    def _unchanged(self, message: str) -> None:
        self.unchanged.append(message)
        print(f"[skip] {message}")

    @staticmethod
    def _require(name: str, message: str) -> None:
        if shutil.which(name) is None:
            raise DeployError(message)

    @staticmethod
    def _python_ok(path: str) -> bool:
        try:
            result = subprocess.run([path, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)"],
                                    capture_output=True, text=True, check=False, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    @staticmethod
    def _query(command: list[str]) -> str:
        try:
            return subprocess.run(command, capture_output=True, text=True, check=False, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    @staticmethod
    def _account_exists(name: str) -> bool:
        return Deployer._query(["getent", "passwd", name]) != ""

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _chown(self, path: Path) -> None:
        self._act(f"设置属主 {self.args.user}:{self.args.group} -> {path}",
                  ["chown", f"{self.args.user}:{self.args.group}", str(path)])

    def _report(self) -> None:
        print(f"变更项 {len(self.changed)}: {', '.join(self.changed) if self.changed else '无'}")
        print(f"保持项 {len(self.unchanged)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STUN 服务幂等部署")
    parser.add_argument("--dry-run", action="store_true", help="只打印动作, 不写任何文件")
    parser.add_argument("--port", type=int, default=3478, help="UDP 监听端口, 默认 3478")
    parser.add_argument("--user", default="rdc-stun", help="运行账号, 默认 rdc-stun")
    parser.add_argument("--group", default=None, help="运行组, 默认同账号名")
    parser.add_argument("--prefix", default="/opt/rdc-stun", help="安装根目录, 默认 /opt/rdc-stun")
    parser.add_argument("--unit", default="/etc/systemd/system/rdc-stun.service", help="单元文件路径")
    parser.add_argument("--python", default="/usr/bin/python3", help="单元使用的解释器路径")
    args = parser.parse_args(argv)
    args.group = args.group or args.user
    unit_name = Path(args.unit).name
    args.service = unit_name[: -len(".service")] if unit_name.endswith(".service") else unit_name
    try:
        return Deployer(args).run()
    except DeployError as error:
        print(f"部署失败: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
