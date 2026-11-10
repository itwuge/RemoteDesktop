# ==========================================================================================
# 文件        : identity_store.py
# 所属目录    : src/rdc/crypto/
# 文件用途    : 持久化设备身份, 保证跨端账号下的设备标识稳定
# 架构定位    : 能力层存储适配, 只负责读写 Ed25519 私钥
# 核心功能    : 载入已有身份; 不存在或损坏时生成并落盘
# 职责边界    : 不做网络; 不参与会话密钥派生; 不向界面暴露私钥
# ==========================================================================================

"""设备身份持久化.

账号中心按 Ed25519 设备身份绑定设备, 每次启动随机生成会导致同一台机器在账号下出现多条记录,
因此首次启动生成后写入数据目录, 后续复用同一身份.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rdc.core.logging import log_event
from rdc.core.runtime import data_dir
from rdc.crypto.identity import DeviceIdentity, b64decode, b64encode

IDENTITY_FILE_NAME = "device_identity.json"
IDENTITY_VERSION = 1
# 区分「首次启动没有文件」与「文件损坏」: 前者是正常路径, 后者必须留痕
IDENTITY_MISSING = "missing"


def load_or_create_identity(path: str | Path | None = None) -> DeviceIdentity:
    """载入持久设备身份; 文件缺失时生成新身份并落盘.

    文件存在但已不可用(损坏、版本不符、私钥非法)时会重建, 并记 WARNING:
    换 ID 会让已配对设备的设备码全部失效, 必须能在日志里定位到原因.
    """
    target = Path(path) if path is not None else data_dir() / IDENTITY_FILE_NAME
    identity, problem = _load(target)
    if identity is not None:
        return identity
    replacement = DeviceIdentity.generate()
    if problem != IDENTITY_MISSING:
        log_event(
            "crypto",
            "device_identity_rebuilt",
            level="WARNING",
            path=str(target),
            reason=problem,
            device_id=replacement.device_id,
        )
    _save(target, replacement)
    return replacement


def _load(path: Path) -> tuple[DeviceIdentity | None, str]:
    """读取身份文件; 失败时返回 (None, 原因), 原因用于区分首次启动与损坏重建."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, IDENTITY_MISSING
    except (OSError, UnicodeDecodeError) as error:
        return None, f"unreadable:{type(error).__name__}"
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(loaded, dict):
        return None, "invalid_payload"
    try:
        version = int(loaded.get("version", 0))
    except (TypeError, ValueError):
        return None, "unsupported_version"
    if version != IDENTITY_VERSION:
        return None, "unsupported_version"
    private_b64 = loaded.get("private_key")
    if not isinstance(private_b64, str) or not private_b64:
        return None, "invalid_key"
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(b64decode(private_b64))
    except (ValueError, TypeError):
        return None, "invalid_key"
    return DeviceIdentity(private_key=private_key), ""


def _save(path: Path, identity: DeviceIdentity) -> None:
    """原子写入私钥文件; 失败只告警, 本次运行仍使用内存身份."""
    raw = identity.private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    payload = {"version": IDENTITY_VERSION, "private_key": b64encode(raw)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(temp, 0o600)
        temp.replace(path)
    except OSError:
        log_event("crypto", "device_identity_save_failed", level="WARNING")
