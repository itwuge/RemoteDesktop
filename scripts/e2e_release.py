# ==========================================================================================
# 文件        : e2e_release.py
# 所属目录    : scripts/
# 文件用途    : 本地模拟 GitHub Release 的端到端验证: tag -> 产物 -> 清单 -> 检查 -> 下载 -> 校验
# 架构定位    : 本地闭环验证脚本; 不访问外部服务, 不推送任何远端
# 核心功能    : 演练 GitHub/镜像/本地三种源; 正常更新、降级拒绝、篡改拒绝、更新面板截图
# 职责边界    : 不修改业务代码; 不执行下载的包; 不重启机器; 不触碰 ECS
# ==========================================================================================

"""本地模拟 Release 端到端验证.

用法:
    python scripts/e2e_release.py
    python scripts/e2e_release.py --current-version 0.1.0

前置条件: 已执行 scripts/build_windows.py 生成安装包与便携包.
证据目录: <构建根>\\evidence\\release\\<时间戳>\\ 与 项目 evidence/update/<时间戳>-release/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUILD_ROOT = Path(r"D:\RDC-Build")
DEFAULT_REPO = "itwuge/RemoteDesktop"
CHUNK_SIZE = 64 * 1024


class QuietHandler(SimpleHTTPRequestHandler):
    """静默 HTTP 处理器, 仅供本地闭环验证."""

    def log_message(self, fmt: str, *args: object) -> None:
        return


class ThrottledHandler(QuietHandler):
    """限速处理器, 让下载进度在截图时可见."""

    def copyfile(self, source: object, outputfile: object) -> None:
        while True:
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            outputfile.write(chunk)
            time.sleep(0.004)


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _start_server(root: Path, handler: type[SimpleHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, int(server.server_address[1])


def _publish(args: list[str]) -> bool:
    script = Path(__file__).with_name("publish_release.py")
    result = subprocess.run([sys.executable, str(script), *args], cwd=ROOT, check=False)
    return result.returncode == 0


def _download(service: object, evidence: Path, timeout: float = 120.0) -> tuple[bool, str, Path]:
    start = service.start_update_download()
    task_id = str((start.data or {}).get("task_id", ""))
    progress = start
    deadline = time.monotonic() + timeout
    while task_id and time.monotonic() < deadline:
        progress = service.get_update_progress(task_id)
        if str((progress.data or {}).get("status", "")) in {"done", "failed", "canceled"}:
            break
        time.sleep(0.05)
    data = progress.data or {}
    package = Path(str(data.get("package_path", "")))
    return bool(progress.ok and data.get("verified") and package.is_file()), str(data.get("status", "")), package


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地模拟 Release 端到端验证")
    parser.add_argument("--build-root", default=os.environ.get("RDC_BUILD_ROOT", str(DEFAULT_BUILD_ROOT)))
    parser.add_argument("--current-version", default="0.1.0", help="模拟的旧客户端版本")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args(argv)

    build_root = Path(args.build_root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence = build_root / "evidence" / "release" / stamp
    ui_dir = evidence / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    repo_evidence = ROOT / "evidence" / "update" / f"{stamp}-release"
    repo_evidence.mkdir(parents=True, exist_ok=True)

    from rdc.update.service import UpdateService
    from rdc.update.source import parse_source

    steps: list[dict[str, object]] = []
    version = _code_version()
    tag = f"v{version}"
    started = time.perf_counter()

    release_root = build_root / "release-sim"
    offline_root = build_root / "release-sim-offline"
    release_root.mkdir(parents=True, exist_ok=True)
    server, port = _start_server(release_root, ThrottledHandler)
    base = f"http://127.0.0.1:{port}"
    served = f"{base}/github/{args.repo}/releases"
    asset_base = f"{served}/download/{tag}"

    published = _publish(
        [
            "--artifacts",
            str(build_root / "artifacts" / "desktop"),
            "--out",
            str(release_root),
            "--repo",
            args.repo,
            "--asset-url-base",
            asset_base,
        ]
    )
    offline = _publish(
        [
            "--artifacts",
            str(build_root / "artifacts" / "desktop"),
            "--out",
            str(offline_root),
            "--repo",
            args.repo,
            "--relative-asset-urls",
        ]
    )
    steps.append(
        {
            "step": "发布演练: tag/产物/清单/回读校验",
            "passed": published and offline,
            "detail": f"online={published} offline={offline} out={release_root}",
        }
    )
    if not (published and offline):
        server.shutdown()
        server.server_close()
        _write_result(evidence, repo_evidence, stamp, args, steps, time.perf_counter() - started)
        return 1

    latest = release_root / "github" / args.repo / "releases" / "latest" / "download"
    manifest = json.loads((latest / "update-manifest.json").read_text(encoding="utf-8"))
    steps.append(
        {
            "step": "清单资产与命名一致",
            "passed": manifest.get("schema") == 2 and len(manifest.get("assets", [])) >= 1,
            "detail": f"schema={manifest.get('schema')} assets={[item['name'] for item in manifest.get('assets', [])]}",
        }
    )

    github_source = parse_source(f"github:{args.repo}")
    expected_manifest = f"https://github.com/{args.repo}/releases/latest/download/update-manifest.json"
    steps.append(
        {
            "step": "默认 GitHub 源解析",
            "passed": github_source.kind == "github" and github_source.manifest_url == expected_manifest,
            "detail": f"{github_source.label} -> {github_source.manifest_url}",
        }
    )

    offline_latest = offline_root / "github" / args.repo / "releases" / "latest" / "download"
    local_service = UpdateService(
        current_version=args.current_version,
        source=f"local:{offline_latest}",
        work_dir=evidence / "downloads-local",
    )
    local_status = local_service.refresh_update_status()
    local_ok, local_state, local_package = _download(local_service, evidence)
    local_digest = _sha256(local_package) if local_package.is_file() else ""
    local_manifest_sha = _asset_sha(offline_latest / "update-manifest.json", local_package.name)
    steps.append(
        {
            "step": "本地目录源: 检查->下载->校验",
            "passed": bool(local_status.ok and (local_status.data or {}).get("available"))
            and local_ok
            and local_digest == local_manifest_sha,
            "detail": f"status={local_state} verified={local_ok} sha256_match={local_digest == local_manifest_sha}",
        }
    )

    mirror_service = UpdateService(
        current_version=args.current_version,
        source=f"mirror:{served}/latest/download",
        work_dir=evidence / "downloads-mirror",
    )
    mirror_status = mirror_service.refresh_update_status()
    mirror_ok, mirror_state, mirror_package = _download(mirror_service, evidence)
    mirror_digest = _sha256(mirror_package) if mirror_package.is_file() else ""
    expected_sha = _asset_sha(latest / "update-manifest.json", mirror_package.name)
    steps.append(
        {
            "step": "镜像源(模拟 GitHub 资产直链): 检查->下载->校验",
            "passed": bool(mirror_status.ok and (mirror_status.data or {}).get("available"))
            and mirror_ok
            and mirror_digest == expected_sha,
            "detail": f"status={mirror_state} verified={mirror_ok} sha256_match={mirror_digest == expected_sha}",
        }
    )

    downgrade = UpdateService(
        current_version="9.9.9",
        source=f"mirror:{served}/latest/download",
        work_dir=evidence / "downloads-downgrade",
    )
    downgrade_result = downgrade.refresh_update_status()
    steps.append(
        {
            "step": "拒绝降级",
            "passed": (not downgrade_result.ok) and downgrade_result.error_code == "UPDATE_DOWNGRADE_REJECTED",
            "detail": f"error_code={downgrade_result.error_code}",
        }
    )

    tampered_root = evidence / "tampered-release"
    shutil.copytree(latest, tampered_root, dirs_exist_ok=True)
    tampered_manifest_path = tampered_root / "update-manifest.json"
    tampered_manifest = json.loads(tampered_manifest_path.read_text(encoding="utf-8"))
    tampered_asset = next(
        (item for item in tampered_manifest["assets"] if item["name"] == local_package.name),
        tampered_manifest["assets"][0],
    )
    tampered_asset["url"] = tampered_asset["name"]
    tampered_manifest_path.write_text(
        json.dumps(tampered_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (tampered_root / tampered_asset["name"]).write_bytes(os.urandom(128 * 1024))
    tampered_service = UpdateService(
        current_version=args.current_version,
        source=f"local:{tampered_root}",
        work_dir=evidence / "downloads-tampered",
    )
    tampered_service.refresh_update_status()
    tampered_ok, tampered_state, _ = _download(tampered_service, evidence, timeout=30)
    leftover = list((evidence / "downloads-tampered").glob("*"))
    steps.append(
        {
            "step": "篡改包校验失败并清理",
            "passed": not tampered_ok and not leftover,
            "detail": f"status={tampered_state or 'rejected'} leftover={len(leftover)}",
        }
    )

    no_check = UpdateService(
        current_version=args.current_version,
        source=f"mirror:{served}/latest/download",
        work_dir=evidence / "downloads-no-check",
    )
    no_check_result = no_check.start_update_download()
    steps.append({"step": "未检查先下载被拒绝", "passed": not no_check_result.ok, "detail": no_check_result.message})

    steps.extend(
        _capture_ui(
            ui_dir=ui_dir,
            source=f"mirror:{served}/latest/download",
            current_version=args.current_version,
            work_dir=evidence / "downloads-ui",
        )
    )

    server.shutdown()
    server.server_close()
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    for item in steps:
        _emit(f"[{'通过' if item['passed'] else '失败'}] {item['step']}: {item['detail']}")
    _emit(f"演练输出: {release_root}")
    _emit(f"证据目录: {evidence}")
    _emit(f"总耗时: {duration_ms} ms")
    return _write_result(evidence, repo_evidence, stamp, args, steps, time.perf_counter() - started)


def _code_version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from rdc.version import __version__

    return __version__


def _asset_sha(manifest_path: Path, name: str) -> str:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for asset in payload.get("assets", []):
        if asset.get("name") == name:
            return str(asset.get("sha256", ""))
    return ""


def _write_result(
    evidence: Path,
    repo_evidence: Path,
    stamp: str,
    args: argparse.Namespace,
    steps: list[dict[str, object]],
    elapsed: float,
) -> int:
    passed = all(bool(item["passed"]) for item in steps)
    result = {
        "timestamp": stamp,
        "channel": "local-release-simulation",
        "repo": args.repo,
        "current_version": args.current_version,
        "duration_ms": round(elapsed * 1000, 2),
        "passed": passed,
        "steps": steps,
        "screenshots": sorted(path.name for path in (evidence / "ui").glob("*.png")),
        "remote_actions_executed": False,
    }
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    (evidence / "result.json").write_text(payload, encoding="utf-8", newline="\n")
    (repo_evidence / "result.json").write_text(payload, encoding="utf-8", newline="\n")
    for path in (evidence / "ui").glob("*.png"):
        shutil.copy2(path, repo_evidence / path.name)
    return 0 if passed else 1


def _capture_ui(*, ui_dir: Path, source: str, current_version: str, work_dir: Path) -> list[dict[str, object]]:
    """驱动更新面板并截图; Windows 使用 windows 插件以加载系统中文字体."""
    os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen")
    from PySide6.QtWidgets import QApplication

    from rdc.commands import update as update_commands
    from rdc.ui.main_window import MainWindow
    from rdc.ui.theme import apply_theme
    from rdc.update.service import UpdateService

    steps: list[dict[str, object]] = []
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    app.setStyle("Fusion")
    apply_theme(app)
    update_commands.configure(UpdateService(current_version=current_version, source=source, work_dir=work_dir))
    window = MainWindow()
    window.resize(1296, 879)
    window.show()
    window.switch_page("settings")
    for _ in range(6):
        app.processEvents()
        time.sleep(0.05)
    window.grab().save(str(ui_dir / "settings-entry.png"))

    window.settings._on_check_update()
    panel = window.settings._update_panel
    if panel is None:
        window.close()
        return [{"step": "更新面板打开", "passed": False, "detail": "面板未创建"}]
    panel.show()
    _wait_until(app, lambda: panel.kind_chip.text() != "未检查", 15)
    panel.grab().save(str(ui_dir / "panel-available.png"))
    latest_text = panel.latest_label.text()
    steps.append(
        {
            "step": "更新面板显示新版本与来源",
            "passed": latest_text == _code_version() and panel.download_button.isEnabled(),
            "detail": f"current={panel.current_label.text()} latest={latest_text} source={panel.source_label.text()}",
        }
    )

    panel._on_download()
    _wait_until(app, lambda: 5 <= panel.progress_bar.value() <= 95, 30)
    panel.grab().save(str(ui_dir / "panel-downloading.png"))
    steps.append(
        {
            "step": "下载进度可见",
            "passed": 0 < panel.progress_bar.value() < 100,
            "detail": f"progress={panel.progress_bar.value()}%",
        }
    )

    _wait_until(app, lambda: panel.verify_chip.text() in {"校验通过", "校验失败"}, 120)
    panel.grab().save(str(ui_dir / "panel-verified.png"))
    steps.append(
        {
            "step": "面板显示校验通过与安装提示",
            "passed": panel.verify_chip.text() == "校验通过" and "可以安装" in panel.hint_label.text(),
            "detail": f"verify={panel.verify_chip.text()} hint={panel.hint_label.text()}",
        }
    )
    panel.close()
    window.close()
    return steps


def _wait_until(app: object, condition: object, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        app.processEvents()
        time.sleep(0.03)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
