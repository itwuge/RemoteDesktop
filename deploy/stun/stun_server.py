# ==========================================================================================
# 文件        : stun_server.py
# 所属目录    : deploy/stun/
# 文件用途    : 极简 STUN Binding 服务, 只回应 Binding Request, 报告 srflx 映射
# 架构定位    : 部署工具, 不属于产品运行时代码
# 核心功能    : RFC 5389 头部校验; XOR-MAPPED-ADDRESS 与 MAPPED-ADDRESS 回填; SIGTERM 优雅退出
# 职责边界    : 只监听一个 UDP 端口; 不落盘、不鉴权、不转发数据、不输出凭据
# 运行环境    : Python 3.8+ 标准库, Ubuntu 20.04/22.04 x86_64
# 依赖关系    : 无第三方依赖
# ==========================================================================================

"""最小 STUN Binding 服务.

只处理 Binding Request (0x0001): 校验 magic cookie 与长度后, 回填客户端在服务端看到的
源地址, 即 srflx 候选所需映射. 不实现 TURN/CHANGE-REQUEST 等扩展, 不打日志刷屏.
"""

from __future__ import annotations

import argparse
import signal
import socket
import struct
import sys

MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_SUCCESS = 0x0101
ATTR_MAPPED_ADDRESS = 0x0001
ATTR_XOR_MAPPED_ADDRESS = 0x0020
HEADER_BYTES = 20
IPV4_FAMILY = 0x01


def _xor_mapped(addr: tuple[str, int]) -> bytes:
    """构造 XOR-MAPPED-ADDRESS 属性, IPv4."""
    packed_ip = socket.inet_aton(addr[0])
    mask = struct.pack("!I", MAGIC_COOKIE)
    xored_ip = bytes(left ^ right for left, right in zip(packed_ip, mask))
    xored_port = addr[1] ^ (MAGIC_COOKIE >> 16)
    body = struct.pack("!BBH", 0, IPV4_FAMILY, xored_port) + xored_ip
    return struct.pack("!HH", ATTR_XOR_MAPPED_ADDRESS, len(body)) + body


def _mapped(addr: tuple[str, int]) -> bytes:
    """构造兼容旧客户端的 MAPPED-ADDRESS 属性, IPv4."""
    body = struct.pack("!BBH", 0, IPV4_FAMILY, addr[1]) + socket.inet_aton(addr[0])
    return struct.pack("!HH", ATTR_MAPPED_ADDRESS, len(body)) + body


def build_success(transaction_id: bytes, addr: tuple[str, int]) -> bytes:
    """组装 Binding Success Response."""
    attributes = _xor_mapped(addr) + _mapped(addr)
    header = struct.pack("!HHI", BINDING_SUCCESS, len(attributes), MAGIC_COOKIE)
    return header + transaction_id + attributes


def is_binding_request(packet: bytes) -> bool:
    """只接受长度合法、带 magic cookie 的 Binding Request."""
    if len(packet) < HEADER_BYTES:
        return False
    message_type, length = struct.unpack("!HH", packet[:4])
    cookie = struct.unpack("!I", packet[4:8])[0]
    return (
        message_type == BINDING_REQUEST
        and cookie == MAGIC_COOKIE
        and length + HEADER_BYTES <= len(packet)
        and length % 4 == 0
    )


def serve(host: str, port: int) -> int:
    """单线程收包回包, 直到收到 SIGTERM/SIGINT."""
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.settimeout(1.0)
    print(f"stun listening on udp/{host}:{port}", flush=True)
    try:
        while running:
            try:
                packet, peer = sock.recvfrom(1500)
            except socket.timeout:
                continue
            except OSError as error:
                if not running:
                    break
                print(f"stun recv error: {error}", file=sys.stderr, flush=True)
                continue
            if not is_binding_request(packet):
                continue
            try:
                sock.sendto(build_success(packet[8:20], peer), peer)
            except OSError as error:
                print(f"stun send error: {error}", file=sys.stderr, flush=True)
    finally:
        sock.close()
    print("stun stopped", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="最小 STUN Binding 服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址, 默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=3478, help="监听 UDP 端口, 默认 3478")
    args = parser.parse_args(argv)
    return serve(args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
