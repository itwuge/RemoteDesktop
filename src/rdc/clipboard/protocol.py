# ==========================================================================================
# 文件        : protocol.py
# 所属目录    : src/rdc/clipboard/
# 文件用途    : 剪贴板同步在线协议侧的消息需求与契约覆盖比对
# 架构定位    : 业务层与协议层的边界说明, 只读引用 signaling 的消息类型
# 核心功能    : 声明剪贴板所需消息、比对协议契约已有消息、输出是否可跨端收发
# 职责边界    : 不构造包络; 不发送网络数据; 不修改契约正文
# ==========================================================================================

"""剪贴板协议覆盖.

契约依据: docs/contracts/protocol-v1.md 第三节, v1.3 起定义 clipboard_update、
clipboard_request、clipboard_ack 三类消息与大小上限。
"""

from __future__ import annotations

from rdc.signaling.messages import MESSAGE_TYPES

CLIPBOARD_MESSAGES: tuple[str, ...] = ("clipboard_update", "clipboard_request", "clipboard_ack")


def missing_protocol_messages() -> dict[str, str]:
    """返回契约中缺失的剪贴板消息及原因; 契约补齐后返回空字典."""
    missing: dict[str, str] = {}
    for name in CLIPBOARD_MESSAGES:
        if name not in MESSAGE_TYPES:
            missing[name] = "protocol-v1 第三节未定义该剪贴板消息, 需走契约变更流程"
    return missing


def protocol_ready() -> bool:
    """契约是否已覆盖剪贴板收发消息."""
    return not missing_protocol_messages()
