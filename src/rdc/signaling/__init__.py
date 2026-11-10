"""协议层: 七字段包络、14 类信令与内存服务端骨架."""

from rdc.signaling.envelope import ENVELOPE_FIELDS, PROTOCOL_VERSION, build_envelope, validate_envelope
from rdc.signaling.messages import MESSAGE_TYPES, REQUIRED_PAYLOAD
from rdc.signaling.server import InMemorySignalingServer

__all__ = [
    "ENVELOPE_FIELDS",
    "MESSAGE_TYPES",
    "PROTOCOL_VERSION",
    "REQUIRED_PAYLOAD",
    "InMemorySignalingServer",
    "build_envelope",
    "validate_envelope",
]
