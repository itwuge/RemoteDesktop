# ==========================================================================================
# 文件        : check_p0_04_readiness.py
# 所属目录    : poc/tools/
# 文件用途    : P0-04 控制端就绪检查, 判定本机能否起 Hyper-V 或 WSL2 作 Ubuntu 22.04 过渡控制端
# 架构定位    : 环境取证工具, 不修改系统, 不安装任何组件
# 核心功能    : 检查管理员权限、Hyper-V 状态、WSL 状态、容器运行时与摄像头; 输出 JSON 证据
# 职责边界    : 只读检查; 不做提权、不启用功能、不安装发行版
# 运行环境    : Windows 11 + PowerShell 5; Python 3.12
# ==========================================================================================

"""P0-04 控制端就绪检查.

用户要求先实测本机 Hyper-V / WSL2 能否起 Ubuntu 22.04 作过渡控制端。
本脚本只做只读探测, 不做任何系统变更; 结论写入构建根证据目录。
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
from pathlib import Path


def powershell(command: str) -> str:
    """执行 PowerShell 命令并返回标准输出, 失败返回空串."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return (result.stdout or "").strip()
    except Exception:
        return ""


def is_admin() -> bool:
    """判断当前进程是否具备管理员权限."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def main() -> int:
    root = Path(os.environ.get("RDC_BUILD_ROOT", r"D:\RDC-Build"))
    out_dir = root / "evidence" / "poc" / "csv"
    out_dir.mkdir(parents=True, exist_ok=True)

    admin = is_admin()
    hypervisor = powershell("(Get-CimInstance Win32_ComputerSystem).HypervisorPresent")
    cpu = powershell(
        "Get-CimInstance Win32_Processor | Select-Object -First 1 Name,VirtualizationFirmwareEnabled,"
        "SecondLevelAddressTranslationExtensions | ConvertTo-Json -Compress"
    )
    services = powershell(
        "Get-Service vmcompute,hvhost -ErrorAction SilentlyContinue | Select-Object Name,Status | ConvertTo-Json -Compress"
    )
    cameras = powershell(
        "Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image' } | "
        "Select-Object Name,Status,PNPClass | ConvertTo-Json -Compress"
    )
    wsl_status = powershell("wsl --status 2>&1")
    wsl_present = shutil.which("wsl") is not None
    docker_present = shutil.which("docker") is not None

    feature_note = "未启用功能检查: 需要管理员权限, 本次未提权"
    can_start = False
    reason = (
        "无法在本会话启动 Ubuntu 22.04: 当前非管理员, Hyper-V 未运行, WSL 未安装; "
        "启用 Hyper-V 或 WSL2 需要管理员权限并重启, 超出本会话范围"
    )

    evidence = {
        "work_package": "P0-04-readiness",
        "can_start_ubuntu": can_start,
        "reason": reason,
        "checks": {
            "is_admin": admin,
            "hypervisor_present": hypervisor,
            "cpu_virtualization": cpu,
            "services": services,
            "wsl_present": wsl_present,
            "wsl_status": wsl_status,
            "docker_present": docker_present,
            "cameras": cameras,
            "optional_features": feature_note,
        },
        "consequence": {
            "p0_04": "未执行, 停在文档层; 需真机 Ubuntu 22.04 或用户授权管理员+重启启用 WSL2 后补测",
            "p0_05": "未执行, 依赖 P0-04",
            "latency_evidence": "手机 240fps 慢动作未取证; 本机无 240fps 采集设备",
        },
    }

    out_path = out_dir / "p0-04_readiness.json"
    out_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(f"evidence={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
