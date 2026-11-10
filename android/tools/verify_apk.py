#!/usr/bin/env python3
# ==========================================================================================
# 文件        : verify_apk.py
# 所属目录    : android/tools/
# 文件用途    : 侧载 APK 的静态校验: 包名、版本、SDK 区间、启动器、签名、对齐、ABI、SHA-256
# 架构定位    : 构建产物校验脚本; 不做安装, 不依赖设备或模拟器
# 核心功能    : aapt badging + apksigner verify + zipalign -c + zip 条目检查
# 职责边界    : 只读 APK; 不修改产物、不重签、不写仓库内凭据
# 运行环境    : Python 3.12 + JDK 17 + Android SDK build-tools 34
# 依赖关系    : android/app/build/outputs/apk/release/app-release.apk
# ==========================================================================================

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

EXPECTED_PACKAGE = "com.rdc.remote"
EXPECTED_VERSION_CODE = "10"
EXPECTED_VERSION_NAME = "1.0.0"
EXPECTED_MIN_SDK = "29"
EXPECTED_TARGET_SDK = "34"
EXPECTED_LAUNCHER = "com.rdc.remote.MainActivity"
EXPECTED_SIGNER_CN = "CN=RDC Remote Desktop"
EXPECTED_ABI = "arm64-v8a"

DEFAULT_SDK = Path("D:/RDC-Build/tools/android/sdk")
DEFAULT_JDK = Path("D:/RDC-Build/tools/jdk17/jdk-17.0.21+9")


def find_sdk(explicit: str | None) -> Path:
    for candidate in (explicit, os.environ.get("ANDROID_SDK_ROOT"), os.environ.get("ANDROID_HOME")):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    local_properties = Path(__file__).resolve().parents[1] / "local.properties"
    if local_properties.is_file():
        for line in local_properties.read_text(encoding="utf-8").splitlines():
            if line.startswith("sdk.dir="):
                raw = line.split("=", 1)[1].strip().replace("\\\\", "\\").replace("\\:", ":")
                if Path(raw).is_dir():
                    return Path(raw)
    if DEFAULT_SDK.is_dir():
        return DEFAULT_SDK
    raise SystemExit("未找到 Android SDK, 请用 --sdk 指定")


def find_build_tools(sdk: Path) -> Path:
    root = sdk / "build-tools"
    candidates = sorted(
        (item for item in root.iterdir() if (item / "aapt.exe").is_file() and (item / "apksigner.bat").is_file()),
        key=lambda item: item.name,
        reverse=True,
    )
    if not candidates:
        raise SystemExit(f"未找到 build-tools: {root}")
    return candidates[0]


def find_java() -> Path | None:
    java_home = os.environ.get("JAVA_HOME")
    if java_home and (Path(java_home) / "bin" / "java.exe").is_file():
        return Path(java_home)
    if (DEFAULT_JDK / "bin" / "java.exe").is_file():
        return DEFAULT_JDK
    parent = Path("D:/RDC-Build/tools/jdk17")
    if parent.is_dir():
        for item in parent.iterdir():
            if (item / "bin" / "java.exe").is_file():
                return item
    return None


def run(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)


def badging(aapt: Path, apk: Path) -> dict[str, str]:
    result = run([str(aapt), "dump", "badging", str(apk)])
    if result.returncode != 0:
        raise SystemExit(f"aapt 读取失败: {result.stderr.strip()}")
    text = result.stdout
    found: dict[str, str] = {}
    package = re.search(r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", text)
    if package:
        found["package"] = package.group(1)
        found["version_code"] = package.group(2)
        found["version_name"] = package.group(3)
    min_sdk = re.search(r"sdkVersion:'([^']+)'", text)
    if min_sdk:
        found["min_sdk"] = min_sdk.group(1)
    target_sdk = re.search(r"targetSdkVersion:'([^']+)'", text)
    if target_sdk:
        found["target_sdk"] = target_sdk.group(1)
    launcher = re.search(r"launchable-activity: name='([^']+)'", text)
    if launcher:
        found["launcher"] = launcher.group(1)
    return found


def verify_signature(build_tools: Path, apk: Path, java_home: Path | None) -> tuple[bool, str]:
    env = dict(os.environ)
    if java_home:
        env["JAVA_HOME"] = str(java_home)
        env["PATH"] = f"{java_home / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    result = run([str(build_tools / "apksigner.bat"), "verify", "--verbose", "--print-certs", str(apk)], env)
    output = f"{result.stdout}\n{result.stderr}".strip()
    ok = result.returncode == 0 and "Verifies" in output and EXPECTED_SIGNER_CN in output
    schemes = [line for line in output.splitlines() if line.startswith("Verified using")]
    summary = "; ".join(schemes) if schemes else output.splitlines()[0] if output else "无输出"
    return ok, summary


def verify_alignment(build_tools: Path, apk: Path) -> tuple[bool, str]:
    result = run([str(build_tools / "zipalign.exe"), "-c", "-v", "4", str(apk)])
    output = f"{result.stdout}\n{result.stderr}".strip()
    return result.returncode == 0, output.splitlines()[-1] if output else "无输出"


def native_abis(apk: Path) -> list[str]:
    abis: set[str] = set()
    with zipfile.ZipFile(apk) as archive:
        for name in archive.namelist():
            if name.startswith("lib/") and name.endswith(".so"):
                parts = name.split("/")
                if len(parts) > 2:
                    abis.add(parts[1])
    return sorted(abis)


def main() -> int:
    parser = argparse.ArgumentParser(description="校验侧载 APK 的包名、版本、签名、对齐与 ABI")
    parser.add_argument("--apk", default=None, help="APK 路径")
    parser.add_argument("--sdk", default=None, help="Android SDK 根目录")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    apk = Path(args.apk) if args.apk else (
        Path(__file__).resolve().parents[1] / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
    )
    if not apk.is_file():
        raise SystemExit(f"APK 不存在: {apk}")

    sdk = find_sdk(args.sdk)
    build_tools = find_build_tools(sdk)
    info = badging(build_tools / "aapt.exe", apk)
    signature_ok, signature = verify_signature(build_tools, apk, find_java())
    aligned_ok, alignment = verify_alignment(build_tools, apk)
    abis = native_abis(apk)
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()

    checks: list[tuple[str, bool, str]] = [
        ("包名", info.get("package") == EXPECTED_PACKAGE, info.get("package", "缺失")),
        ("versionCode", info.get("version_code") == EXPECTED_VERSION_CODE, info.get("version_code", "缺失")),
        ("versionName", info.get("version_name") == EXPECTED_VERSION_NAME, info.get("version_name", "缺失")),
        ("minSdk", info.get("min_sdk") == EXPECTED_MIN_SDK, info.get("min_sdk", "缺失")),
        ("targetSdk", info.get("target_sdk") == EXPECTED_TARGET_SDK, info.get("target_sdk", "缺失")),
        ("启动 Activity", info.get("launcher") == EXPECTED_LAUNCHER, info.get("launcher", "缺失")),
        ("签名", signature_ok, signature),
        ("4 字节对齐", aligned_ok, alignment),
        (
            "ABI",
            not abis or set(abis) == {EXPECTED_ABI},
            ",".join(abis) if abis else f"无 native 库, 构建配置 abiFilters={EXPECTED_ABI}",
        ),
    ]

    print(f"APK: {apk}")
    print(f"大小: {apk.stat().st_size} 字节")
    print(f"SHA-256: {digest}")
    failed = 0
    for name, ok, detail in checks:
        print(f"[{'通过' if ok else '失败'}] {name}: {detail}")
        if not ok:
            failed += 1
    print(f"结论: {'全部通过' if failed == 0 else f'{failed} 项失败'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
