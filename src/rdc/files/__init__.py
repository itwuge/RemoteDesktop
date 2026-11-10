"""文件传输包; 契约 v1.4 第 32 至 36 条.

对外只暴露服务与带宽策略; 命令层与界面层按契约签名调用。
"""

from rdc.files.dnd import handle_drop
from rdc.files.journal import TransferJournal
from rdc.files.model import CHUNK_SIZE, TransferItem, TransferJob
from rdc.files.protocol import missing_protocol_messages, wire_plan
from rdc.files.ratelimit import (
    CHANNEL_LOW_PRIORITY,
    DEFAULT_VIDEO_FLOOR_KBPS,
    BandwidthPlan,
    TokenBucket,
    plan_bandwidth,
)
from rdc.files.scope import (
    REQUIRED_SCOPE,
    SCOPE_INSUFFICIENT,
    SCOPE_INVALID,
    SCOPE_LEVELS,
    scope_covers,
    scope_rejection,
)
from rdc.files.service import FileTransferService, default_destination_root, human_size

__all__ = [
    "CHANNEL_LOW_PRIORITY",
    "CHUNK_SIZE",
    "DEFAULT_VIDEO_FLOOR_KBPS",
    "REQUIRED_SCOPE",
    "SCOPE_INSUFFICIENT",
    "SCOPE_INVALID",
    "SCOPE_LEVELS",
    "BandwidthPlan",
    "FileTransferService",
    "TokenBucket",
    "TransferItem",
    "TransferJob",
    "TransferJournal",
    "default_destination_root",
    "handle_drop",
    "human_size",
    "missing_protocol_messages",
    "plan_bandwidth",
    "scope_covers",
    "scope_rejection",
    "wire_plan",
]
