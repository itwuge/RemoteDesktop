# ==========================================================================================
# 文件        : deploy.py
# 所属目录    : deploy/relay/
# 文件用途    : 一条命令从零部署信令与中继服务到干净 Ubuntu 20.04/22.04 x86_64
# 架构定位    : 部署工具, 不属于产品运行时代码
# 核心功能    : 依赖自举、最小权限账号、目录与配置、systemd 单元、健康检查、幂等重跑
# 职责边界    : 不修改 sshd 与 22 端口, 不改防火墙, 不重启主机, 不写入任何口令
# 运行环境    : 目标机 Ubuntu 20.04/22.04 x86_64, Python 3.8+; 需 root 或 sudo
# 依赖关系    : deploy/relay/rdc-relay.service.tmpl、relay.conf.example、healthcheck.py
# ==========================================================================================

"""中继服务部署脚本.

用法:
    sudo python3 deploy/relay/deploy.py --config deploy/relay/relay.conf.example
    python3 deploy/relay/deploy.py --dry-run --config deploy/relay/relay.conf.example

安全约束:
    - 全程不写 sshd 配置, 不触碰 22 端口, 不重启主机.
    - 配置与单元文件不含口令; 脚本输出不回显任何凭据.
    - 可重复执行; 内容未变化的文件不重写, 服务不重启.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:  # 部署目标为 Linux; 本地 Windows 只跑 --dry-run 校验, 故按需导入
    import grp
    import pwd
except ImportError:  # pragma: no cover - Windows 本地校验路径
    grp = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]

DEFAULTS = {
    "prefix": "/opt/rdc-relay",
    "user": "rdc",
    "group": "rdc",
    "config": "/etc/rdc-relay/relay.conf",
    "unit": "/etc/systemd/system/rdc-relay.service",
    "logdir": "/var/log/rdc-relay",
}
SUPPORTED_UBUNTU = ("20.04", "22.04")


class DeployError(RuntimeError):
    """部署失败, 携带可直接执行的修复提示."""


def config_tls_enabled(content: str) -> bool:
    """读取配置文本里的 [tls] tls_enabled; 缺省按关闭处理, 与运行时默认一致."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(content)
        return parser.getboolean("tls", "tls_enabled", fallback=False)
    except (configparser.Error, ValueError) as error:
        raise DeployError(f"配置内容无法解析: {error}") from error


class Deployer:
    """幂等部署器: 每一步先检查现状, 再决定是否动作."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = Path(__file__).resolve().parents[2]
        self.changed: list[str] = []
        self.unchanged: list[str] = []
        # 只有应用代码/配置/单元真正变化时才需要重启; 建目录等动作不算
        self.needs_restart = False

    # ------------------------------------------------------------------ 入口
    def run(self) -> int:
        """按顺序执行部署步骤, 返回进程退出码."""
        self._preflight()
        self._ensure_runtime()
        self._ensure_account()
        self._ensure_dirs()
        self._install_app()
        self._install_config()
        self._install_unit()
        self._start_service()
        self._install_cert_timer()
        self._install_journal_limit()
        self._report()
        return 0

    # ------------------------------------------------------------------ 步骤
    def _preflight(self) -> None:
        if self.args.dry_run:
            # 本地离线校验: 只验证输入物与解释器, 不检查目标机环境
            self._say("预检: dry-run 模式, 不做任何写操作")
            for label, path in (
                ("配置模板", Path(self.args.config_source)),
                ("systemd 模板", Path(__file__).parent / "rdc-relay.service.tmpl"),
                ("健康检查", Path(__file__).parent / "healthcheck.py"),
                ("证书巡检脚本", Path(__file__).parent / "cert_check.sh"),
                ("证书巡检单元", Path(__file__).parent / "rdc-cert-check.service.tmpl"),
                ("证书巡检定时器", Path(__file__).parent / "rdc-cert-check.timer.tmpl"),
                ("journald 上限", Path(__file__).parent / "journald-rdc.conf"),
                ("源码目录", self.root / "src" / "rdc"),
            ):
                if not path.exists():
                    raise DeployError(f"{label}不存在: {path}")
                self._say(f"预检: {label}存在 -> {path}")
            if sys.version_info < (3, 8):
                raise DeployError(f"部署脚本需要 Python 3.8+, 当前 {platform.python_version()}")
            return
        if os.geteuid() != 0:
            raise DeployError("需要 root 权限: 请用 sudo 重新执行")
        os_release = Path("/etc/os-release")
        if not os_release.exists():
            raise DeployError("未找到 /etc/os-release, 只支持 Ubuntu 20.04/22.04")
        values = {}
        for line in os_release.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"')
        if values.get("ID") != "ubuntu" or values.get("VERSION_ID") not in SUPPORTED_UBUNTU:
            message = f"目标系统为 {values.get('ID')} {values.get('VERSION_ID')}, 只支持 Ubuntu 20.04/22.04"
            if not self.args.allow_unsupported:
                raise DeployError(message + "; 如确认继续, 加 --allow-unsupported")
            self._say("警告: " + message)
        machine = platform.machine()
        if machine not in ("x86_64", "amd64"):
            message = f"目标架构为 {machine}, 只验证过 x86_64"
            if not self.args.allow_unsupported:
                raise DeployError(message + "; 如确认继续, 加 --allow-unsupported")
            self._say("警告: " + message)
        self._require("systemctl", "未找到 systemctl, 目标机必须使用 systemd")
        if not self._python_ok(self.args.python):
            raise DeployError(f"指定的解释器不可用或低于 3.8: {self.args.python}")

    def _ensure_runtime(self) -> None:
        if self._python_ok("python3"):
            self._unchanged("python3 已满足 3.8+")
            return
        self._act("安装 python3 与 python3-venv", ["apt-get", "install", "-y", "python3", "python3-venv"])
        if not self.args.dry_run and not self._python_ok("python3"):
            raise DeployError("python3 安装后仍不可用, 请检查 apt 源与网络")

    def _ensure_account(self) -> None:
        if self._account_exists(self.args.user):
            self._unchanged(f"账号 {self.args.user} 已存在")
            return
        self._act(
            f"创建系统账号 {self.args.user}",
            [
                "useradd",
                "--system",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                "--home-dir",
                self.args.prefix,
                "--user-group",
                self.args.user,
            ],
        )

    def _ensure_dirs(self) -> None:
        for path, mode in (
            (Path(self.args.prefix) / "app", 0o750),
            (Path(self.args.logdir), 0o750),
            (Path(self.args.config).parent, 0o750),
        ):
            if path.is_dir():
                self._unchanged(f"目录已存在: {path}")
                continue
            self._act(
                f"创建目录 {path}",
                ["install", "-d", "-m", f"{mode:o}", "-o", self.args.user, "-g", self.args.group, str(path)],
            )

    def _install_app(self) -> None:
        source = self.root / "src" / "rdc"
        target = Path(self.args.prefix) / "app" / "rdc"
        if not source.exists():
            raise DeployError(f"源码目录不存在: {source}")
        if self._tree_hash(source) == self._tree_hash(target):
            self._unchanged(f"应用代码未变化: {target}")
            return
        self._act(f"同步应用代码到 {target}", None)
        if self.args.dry_run:
            return
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self._chown(target)
        # 权限: 目录 0750, 文件 0640, 不允许其他用户读
        for path in target.rglob("*"):
            path.chmod(0o750 if path.is_dir() else 0o640)
        self.changed.append(f"应用代码 {target}")
        self.needs_restart = True

    def _install_config(self) -> None:
        target = Path(self.args.config)
        source = Path(self.args.config_source)
        if not source.exists():
            raise DeployError(f"配置模板不存在: {source}")
        if target.exists() and not self.args.force_config:
            self._unchanged(f"配置已存在, 保持不动: {target}")
            return
        content = source.read_text(encoding="utf-8")
        if not config_tls_enabled(content) and not self.args.allow_disable_tls:
            raise DeployError(
                "拒绝写入: 配置中 tls_enabled=false, 会把 8750 降为明文; "
                "安全默认必须开启 TLS, 如确需明文调试请显式加 --allow-disable-tls"
            )
        if self._write(target, content, 0o640):
            self.changed.append(f"配置 {target}")
            self.needs_restart = True
        else:
            self._unchanged(f"配置内容一致: {target}")

    def _install_unit(self) -> None:
        template = (Path(__file__).parent / "rdc-relay.service.tmpl").read_text(encoding="utf-8")
        content = (
            template.replace("@USER@", self.args.user)
            .replace("@GROUP@", self.args.group)
            .replace("@PREFIX@", self.args.prefix)
            .replace("@CONFIG@", self.args.config)
            .replace("@LOGDIR@", self.args.logdir)
            .replace("@PYTHON@", self.args.python)
        )
        if self._write(Path(self.args.unit), content, 0o644):
            self.changed.append(f"systemd 单元 {self.args.unit}")
            self.needs_restart = True
            self._act("systemctl daemon-reload", ["systemctl", "daemon-reload"])
        else:
            self._unchanged(f"systemd 单元内容一致: {self.args.unit}")

    def _start_service(self) -> None:
        enabled = self._query(["systemctl", "is-enabled", "rdc-relay"]) == "enabled"
        active = self._query(["systemctl", "is-active", "rdc-relay"]) == "active"
        if enabled and active and not self.needs_restart:
            self._unchanged("服务已启用并在运行, 无需重启")
        else:
            self._act("启用并启动 rdc-relay", ["systemctl", "enable", "--now", "rdc-relay"])
            if not self.args.dry_run and not self.args.no_restart and self.needs_restart:
                self._act("重启 rdc-relay 以加载新内容", ["systemctl", "restart", "rdc-relay"])
        if self.args.dry_run:
            return
        check = None
        # 服务刚启动时端口可能尚未就绪, 最多等待 10 秒再判定失败
        for _ in range(20):
            check = subprocess.run(
                [sys.executable, str(Path(__file__).parent / "healthcheck.py"), self.args.config],
                capture_output=True,
                text=True,
                check=False,
            )
            if check.returncode == 0:
                break
            time.sleep(0.5)
        print(check.stdout.strip() or check.stderr.strip())
        if check.returncode != 0:
            raise DeployError("健康检查未通过; 查看日志: journalctl -u rdc-relay -n 100 --no-pager")

    def _install_cert_timer(self) -> None:
        """安装证书巡检 oneshot 与每日 timer; 只新增单元, 不触碰 relay 服务."""
        base = Path(__file__).parent
        script_target = Path(self.args.prefix) / "bin" / "cert_check.sh"
        files = {
            Path(self.args.prefix) / "bin" / "cert_check.py": (base / "cert_check.py").read_text(encoding="utf-8"),
            script_target: (base / "cert_check.sh")
            .read_text(encoding="utf-8")
            .replace("@PYTHON@", self.args.python)
            .replace("@CONFIG@", self.args.config),
        }
        for target, content in files.items():
            if self._write(target, content, 0o750):
                self.changed.append(f"证书巡检文件 {target}")
                if not self.args.dry_run:
                    shutil.chown(target, self.args.user, self.args.group)
        context = {"@USER@": self.args.user, "@GROUP@": self.args.group, "@SCRIPT@": str(script_target)}
        unit_changed = False
        for name in ("rdc-cert-check.service", "rdc-cert-check.timer"):
            content = (base / f"{name}.tmpl").read_text(encoding="utf-8")
            for key, value in context.items():
                content = content.replace(key, value)
            if self._write(Path("/etc/systemd/system") / name, content, 0o644):
                unit_changed = True
                self.changed.append(f"systemd 单元 {name}")
        if unit_changed:
            self._act("systemctl daemon-reload", ["systemctl", "daemon-reload"])
        enabled = self._query(["systemctl", "is-enabled", "rdc-cert-check.timer"]) == "enabled"
        active = self._query(["systemctl", "is-active", "rdc-cert-check.timer"]) == "active"
        if enabled and active and not unit_changed:
            self._unchanged("证书巡检 timer 已启用并在运行")
        else:
            self._act("启用并启动 rdc-cert-check.timer", ["systemctl", "enable", "--now", "rdc-cert-check.timer"])

    def _install_journal_limit(self) -> None:
        """限制 journald 总量, 防长跑写满磁盘; 只影响 journald, 不重启业务服务."""
        target = Path("/etc/systemd/journald.conf.d/rdc.conf")
        content = (Path(__file__).parent / "journald-rdc.conf").read_text(encoding="utf-8")
        if not self._write(target, content, 0o644):
            self._unchanged(f"journald 容量上限一致: {target}")
            return
        self.changed.append(f"journald 容量上限 {target}")
        self._act("重启 systemd-journald 加载容量上限", ["systemctl", "restart", "systemd-journald"])

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
            self.changed.append(message)

    def _write(self, target: Path, content: str, mode: int) -> bool:
        if target.exists() and target.read_text(encoding="utf-8") == content:
            return False
        prefix = "[dry-run] " if self.args.dry_run else ""
        print(f"{prefix}写入 {target} (mode {mode:o})")
        if self.args.dry_run:
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        os.chmod(target, mode)
        if target.parent == Path("/etc/rdc-relay"):
            os.chown(target, 0, self._gid())
        return True

    def _say(self, message: str) -> None:
        print(message)

    def _unchanged(self, message: str) -> None:
        print(f"[skip] {message}")
        self.unchanged.append(message)

    def _require(self, command: str, message: str) -> None:
        if shutil.which(command) is None:
            raise DeployError(message)

    def _python_ok(self, command: str) -> bool:
        if shutil.which(command) is None:
            return False
        result = subprocess.run(
            [command, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)"],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def _account_exists(self, name: str) -> bool:
        if pwd is None:
            return False
        try:
            pwd.getpwnam(name)
            return True
        except KeyError:
            return False

    def _gid(self) -> int:
        if grp is None:
            raise DeployError("当前环境无 grp 模块, 无法解析运行组")
        try:
            return grp.getgrnam(self.args.group).gr_gid
        except KeyError:
            return pwd.getpwnam(self.args.user).pw_gid

    def _query(self, command: list[str]) -> str:
        if shutil.which(command[0]) is None:
            return ""
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        return result.stdout.strip()

    def _tree_hash(self, root: Path) -> str:
        if not root.exists():
            return ""
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                digest.update(str(path.relative_to(root)).encode("utf-8"))
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def _chown(self, target: Path) -> None:
        if pwd is None:
            raise DeployError("当前环境无 pwd 模块, 无法设置属主")
        uid = pwd.getpwnam(self.args.user).pw_uid
        gid = self._gid()
        for path in [target, *target.rglob("*")]:
            os.chown(path, uid, gid)

    def _report(self) -> None:
        print("---- 部署结果 ----")
        print(f"变更项: {len(self.changed)}")
        print(f"保持不变: {len(self.unchanged)}")
        if self.args.dry_run:
            print("dry-run 结束, 未修改目标机")


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数."""
    parser = argparse.ArgumentParser(description="RDC 信令与中继服务部署")
    parser.add_argument(
        "--config",
        dest="config_source",
        default=str(Path(__file__).parent / "relay.conf.example"),
        help="配置模板路径, 部署时写入目标机",
    )
    parser.add_argument(
        "--config-target", default=DEFAULTS["config"], help=f"目标机配置文件路径, 默认 {DEFAULTS['config']}"
    )
    parser.add_argument("--prefix", default=DEFAULTS["prefix"])
    parser.add_argument("--user", default=DEFAULTS["user"])
    parser.add_argument("--group", default=DEFAULTS["group"])
    parser.add_argument("--logdir", default=DEFAULTS["logdir"])
    parser.add_argument(
        "--python", default="/usr/bin/python3", help="systemd 单元使用的解释器路径, 默认 /usr/bin/python3"
    )
    parser.add_argument("--unit", default=DEFAULTS["unit"], help=f"systemd 单元路径, 默认 {DEFAULTS['unit']}")
    parser.add_argument("--dry-run", action="store_true", help="只打印动作, 不修改目标机")
    parser.add_argument("--force-config", action="store_true", help="覆盖已有配置")
    parser.add_argument(
        "--allow-disable-tls",
        action="store_true",
        help="显式允许写入 tls_enabled=false 的配置(明文, 仅限调试)",
    )
    parser.add_argument("--no-restart", action="store_true", help="不重启已在运行的服务")
    parser.add_argument("--allow-unsupported", action="store_true", help="允许非 Ubuntu 20.04/22.04 x86_64")
    return parser


def main(argv: list[str] | None = None) -> int:
    """脚本入口."""
    args = build_parser().parse_args(argv)
    args.config = args.config_target
    try:
        return Deployer(args).run()
    except DeployError as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
