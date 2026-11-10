# ==========================================================================================
# 文件        : protocol.py
# 所属目录    : src/rdc/files/
# 文件用途    : 文件传输在线协议侧的消息需求与契约覆盖比对
# 架构定位    : 业务层与协议层的边界说明, 只读引用 signaling 的消息类型
# 核心功能    : 声明传输所需消息、比对协议契约已有消息、输出线上形态
# 职责边界    : 不构造包络; 不发送网络数据; 不修改契约正文
# ==========================================================================================

"""文件传输协议覆盖.

契约依据: docs/contracts/protocol-v1.md 第三节, v1.3 起定义 file_offer、file_accept、
file_chunk、file_ack、file_complete、file_abort 六类消息与顺序语义。
本模块按 ``MESSAGE_TYPES`` 实际取值比对, 不硬编码消息总数。
"""

from __future__ import annotations

from typing import Any

from rdc.signaling.messages import MESSAGE_TYPES

# 文件传输所需的消息, 与 protocol-v1 v1.3 第三节一致
TRANSFER_MESSAGES: tuple[str, ...] = (
    "file_offer",
    "file_accept",
    "file_chunk",
    "file_ack",
    "file_complete",
    "file_abort",
)


def missing_protocol_messages() -> dict[str, str]:
    """返回契约中缺失的传输消息及原因; 契约补齐后返回空字典."""
    missing: dict[str, str] = {}
    for name in TRANSFER_MESSAGES:
        if name not in MESSAGE_TYPES:
            missing[name] = "protocol-v1 第三节未定义该文件传输消息, 需走契约变更流程"
    return missing


def wire_plan(item_count: int, total_bytes: int) -> dict[str, Any]:
    """描述线上形态; 契约与消息集合一致时走会话数据面."""
    missing = missing_protocol_messages()
    return {
        "protocol_ready": not missing,
        "transport": "local_staging" if missing else "session_data_plane",
        "required_messages": list(TRANSFER_MESSAGES),
        "missing_messages": sorted(missing),
        "item_count": item_count,
        "total_bytes": total_bytes,
    }
