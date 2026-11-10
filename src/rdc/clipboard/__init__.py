"""剪贴板同步包; 契约 v1.4 第 37 至 40 条."""

from rdc.clipboard.protocol import missing_protocol_messages, protocol_ready
from rdc.clipboard.sync import (
    IMAGE_LIMIT_BYTES,
    TEXT_LIMIT_BYTES,
    ClipboardSyncService,
)

__all__ = [
    "IMAGE_LIMIT_BYTES",
    "TEXT_LIMIT_BYTES",
    "ClipboardSyncService",
    "missing_protocol_messages",
    "protocol_ready",
]
