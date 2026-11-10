# ==========================================================================================
# 文件        : session_store.py
# 所属目录    : src/rdc/accounts/
# 文件用途    : 客户端登录态密文仓, 杀进程重开后恢复登录, 退出登录清空
# 架构定位    : 账号线本地存储层, 只存密文, 不存明文口令
# 核心功能    : Windows DPAPI 用户绑定加密; 其他平台 AES-GCM(机器标识+用户标识派生密钥)
# 职责边界    : 不发起网络; 不校验口令; 不向界面暴露明文; 日志不写令牌与密钥
# ==========================================================================================

"""登录态密文仓.

令牌落盘前必须加密: Windows 使用 DPAPI(当前用户绑定), 其他平台使用 AES-GCM,
密钥由机器标识与用户标识经 PBKDF2-HMAC-SHA256 派生且不落盘.
文件内容为 account_id、account_name、device_id、device_token、session_token、expires_at.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from rdc.core.logging import log_event
from rdc.core.runtime import data_dir

VAULT_MAGIC = b"RDCVAULT1"
MODE_DPAPI = b"DPAPI001"
MODE_AES = b"AESGCM01"
SALT_BYTES = 16
NONCE_BYTES = 12
PBKDF2_ITERATIONS = 200_000
VAULT_FILE_NAME = "account_session.bin"

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SessionVault:
    """登录态密文仓; 读写失败一律按「无登录态」处理, 不影响启动."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else data_dir() / VAULT_FILE_NAME

    def save(self, payload: dict[str, Any]) -> bool:
        """加密并原子写入; 失败返回 False, 调用方保持内存登录态可用."""
        try:
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            blob = self._encrypt(raw)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            temp.write_bytes(blob)
            with contextlib.suppress(OSError):
                os.chmod(temp, 0o600)
            temp.replace(self.path)
            return True
        except (OSError, ValueError, RuntimeError):
            log_event("accounts", "session_vault_save_failed", level="WARNING")
            return False

    def load(self) -> dict[str, Any] | None:
        """读取并解密; 文件不存在、损坏或解密失败返回 None."""
        try:
            blob = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            log_event("accounts", "session_vault_read_failed", level="WARNING")
            return None
        try:
            raw = self._decrypt(blob)
            loaded = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, InvalidTag, RuntimeError):
            log_event("accounts", "session_vault_unreadable", level="WARNING")
            return None
        if not isinstance(loaded, dict):
            return None
        return loaded

    def clear(self) -> None:
        """删除密文仓; 文件不存在视为已清空."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            log_event("accounts", "session_vault_clear_failed", level="WARNING")

    # ------------------------------------------------------------------ 加密
    def _encrypt(self, raw: bytes) -> bytes:
        if sys.platform == "win32":
            return VAULT_MAGIC + MODE_DPAPI + _dpapi_protect(raw)
        salt = os.urandom(SALT_BYTES)
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(_derive_key(salt)).encrypt(nonce, raw, VAULT_MAGIC)
        return VAULT_MAGIC + MODE_AES + salt + nonce + ciphertext

    def _decrypt(self, blob: bytes) -> bytes:
        if not blob.startswith(VAULT_MAGIC):
            raise ValueError("密文仓格式不正确")
        body = blob[len(VAULT_MAGIC) :]
        if body.startswith(MODE_DPAPI):
            return _dpapi_unprotect(body[len(MODE_DPAPI) :])
        if body.startswith(MODE_AES):
            body = body[len(MODE_AES) :]
            if len(body) <= SALT_BYTES + NONCE_BYTES:
                raise ValueError("密文仓长度不正确")
            salt = body[:SALT_BYTES]
            nonce = body[SALT_BYTES : SALT_BYTES + NONCE_BYTES]
            ciphertext = body[SALT_BYTES + NONCE_BYTES :]
            return AESGCM(_derive_key(salt)).decrypt(nonce, ciphertext, VAULT_MAGIC)
        raise ValueError("密文仓加密方式未知")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(raw: bytes) -> bytes:
    """调用 Windows DPAPI 加密; 仅当前用户可解密."""
    if sys.platform != "win32":
        raise RuntimeError("DPAPI 仅在 Windows 可用")
    buffer = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        ctypes.c_wchar_p("rdc-account-session"),
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("DPAPI 加密失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    """调用 Windows DPAPI 解密; 跨用户或损坏时抛 OSError."""
    if sys.platform != "win32":
        raise RuntimeError("DPAPI 仅在 Windows 可用")
    buffer = ctypes.create_string_buffer(blob, len(blob))
    blob_in = _DataBlob(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("DPAPI 解密失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _machine_identifier() -> str:
    """机器标识; Linux 优先 /etc/machine-id, 其他平台退回主机名."""
    if sys.platform.startswith("linux"):
        try:
            value = Path("/etc/machine-id").read_text(encoding="ascii").strip()
            if value:
                return value
        except OSError:
            pass
    return platform.node() or "unknown-host"


def _derive_key(salt: bytes) -> bytes:
    """由机器标识与用户标识派生 AES-256 密钥; 密钥不落盘."""
    uid = os.getuid() if hasattr(os, "getuid") else 0
    material = f"{_machine_identifier()}:{uid}".encode()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS)
    return kdf.derive(material)
