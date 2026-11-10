# ==========================================================================================
# 文件        : rdc_host_linux.py
# 所属目录    : scripts/
# 文件用途    : Ubuntu 被控端启动薄壳; 采集、编码、输入注入与协议全部来自 src/rdc
# 架构定位    : 薄壳。只解析参数、调命令函数 start_hosting、打印启动横幅、等退出
# 核心功能    : 启动真实被控端并输出 "监听地址 / 设备 ID / 连接码" 三行横幅,
#               本机有多个可连地址时追加 "可用地址" 行(含全局 IPv6)
# 职责边界    : 不自带采集/编码/握手实现; 不写证据; 不面向最终用户
# 依赖关系    : rdc.commands.api.start_hosting; rdc.session.host.HostService
# ==========================================================================================

"""Ubuntu 被控端启动薄壳.

用法:
    python scripts/rdc_host_linux.py [--port 27888]

端口由产品配置决定(默认 27888). --fps 与 --bitrate 仅做兼容占位: 命令层
start_hosting 的签名由 desktop-v1 契约冻结, 不接受媒体参数.
"""

from __future__ import annotations

import argparse
import importlib
import signal
import sys
import time
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

# 用 importlib 而非顶层 import: 需要先注入 src 路径, 又不想引入 E402 豁免
api = importlib.import_module("rdc.commands.api")
HostSession = importlib.import_module("rdc.session.host").HostSession
FileTransferReceiver = importlib.import_module("rdc.files.receiver").FileTransferReceiver


def _emit(message: str) -> None:
    """把启动横幅写到标准输出; 一键演示脚本按行解析这三行."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    """解析启动参数; 媒体参数保留只为兼容旧命令."""
    parser = argparse.ArgumentParser(description="远程桌面 Ubuntu 被控端")
    parser.add_argument("--port", type=int, default=27888)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--bitrate", type=int, default=4_000_000)
    parser.add_argument("--display-name", default="Ubuntu 被控端")
    return parser.parse_args()


def main() -> int:
    """启动被控端并阻塞等待; Ctrl+C 退出时回收会话与中继."""
    arguments = parse_args()
    service = api.get_service()
    service.hosting_port = arguments.port
    service.live_endpoint = f"127.0.0.1:{arguments.port}"
    if arguments.fps != 30 or arguments.bitrate != 4_000_000:
        _emit("提示: 帧率与码率由产品配置决定, 本次 --fps/--bitrate 不再生效")
    result = api.start_hosting(arguments.display_name)
    if not result.ok:
        _emit(f"启动失败: {result.message}")
        return 1
    data = result.data or {}
    address = str(data.get("address") or "127.0.0.1")
    port = int(data.get("port") or arguments.port)
    _emit(f"监听地址 : {address}:{port}")
    info = api.get_hosting_info()
    for extra in info.get("addresses") or []:
        text = str(extra or "")
        if text and text != address:
            _emit(f"可用地址 : {text}:{port}")
    _emit(f"设备 ID : {data.get('device_id', '')}")
    _emit(f"连接码 : {data.get('access_code', '')}")
    _emit("被控端已就绪, 按 Ctrl+C 停止")

    def handle_stop(_signum: int, _frame: object) -> None:
        api.stop_hosting()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    try:
        while True:
            time.sleep(0.5)
    finally:
        api.stop_hosting()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
