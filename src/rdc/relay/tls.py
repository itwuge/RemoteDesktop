# ==========================================================================================
# 文件        : tls.py
# 所属目录    : src/rdc/relay/
# 文件用途    : 构建 TLS 1.3 服务端与客户端上下文, 供中继与本地验证复用
# 架构定位    : 中继服务的传输安全层, 只做上下文与证书配置, 不解析业务帧
# 核心功能    : 最低版本锁定 TLS 1.3; 单向/双向证书校验; 证书加载失败转明确错误
# 职责边界    : 不内置固定密钥与证书, 不提供明文回退; 不修改数据面分帧逻辑
# 运行环境    : Python 3.8 及以上(服务端要求 3.10+ 才支持 TLSv1_3 常量)
# 依赖关系    : Python 标准库 ssl
# ==========================================================================================

"""TLS 1.3 上下文构建.

证书与私钥只从配置给出的路径读取, 不在代码或仓库中内置任何密钥。
"""

from __future__ import annotations

import ssl
import time

TLS_MIN_VERSION_NAME = "TLSv1.3"


def build_server_context(
    *,
    cert_file: str,
    key_file: str,
    ca_file: str = "",
    require_client_cert: bool = False,
) -> ssl.SSLContext:
    """构建服务端 TLS 1.3 上下文.

    :param cert_file: 服务端证书链路径
    :param key_file: 服务端私钥路径
    :param ca_file: 客户端证书校验用 CA 路径, 仅双向校验时使用
    :param require_client_cert: True 时要求并校验客户端证书
    :return: 已配置的 SSLContext
    :raises ValueError: 证书缺失、不可加载或双向校验缺少 CA
    """
    if not cert_file or not key_file:
        raise ValueError("启用 TLS 时必须配置 cert_file 与 key_file")
    if require_client_cert and not ca_file:
        raise ValueError("require_client_cert=true 时必须配置 ca_file")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    except (OSError, ssl.SSLError) as error:
        raise ValueError(f"加载服务端证书失败: {error}") from error

    if require_client_cert:
        try:
            context.load_verify_locations(cafile=ca_file)
        except (OSError, ssl.SSLError) as error:
            raise ValueError(f"加载客户端 CA 失败: {error}") from error
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        # 单向 TLS: 只出示服务端证书, 不索取客户端证书
        context.verify_mode = ssl.CERT_NONE
    return context


def build_client_context(
    *,
    ca_file: str,
    cert_file: str = "",
    key_file: str = "",
    check_hostname: bool = True,
) -> ssl.SSLContext:
    """构建客户端 TLS 1.3 上下文, 用于本地等价验证与后续客户端接入.

    :param ca_file: 信任的服务端 CA 路径
    :param cert_file: 客户端证书路径, 双向校验时提供
    :param key_file: 客户端私钥路径
    :param check_hostname: 是否校验服务端证书主机名/SAN
    :return: 已配置的 SSLContext
    :raises ValueError: CA 或客户端证书加载失败
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        context.load_verify_locations(cafile=ca_file)
        if cert_file:
            context.load_cert_chain(certfile=cert_file, keyfile=key_file or None)
    except (OSError, ssl.SSLError) as error:
        raise ValueError(f"加载客户端证书失败: {error}") from error
    context.check_hostname = check_hostname
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def certificate_not_after_epoch(cert_file: str) -> float | None:
    """读取证书到期时刻, 单位 epoch 秒; 无法解析返回 None.

    只依赖标准库, 证书可以是 PEM 或 DER. 读取失败不抛异常, 供健康检查降级使用.
    """
    decoder = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decoder is None or not cert_file:
        return None
    try:
        info = decoder(cert_file)
        return float(ssl.cert_time_to_seconds(str(info["notAfter"])))
    except (OSError, ssl.SSLError, KeyError, TypeError, ValueError):
        return None


def certificate_days_left(cert_file: str, *, now: float | None = None) -> int | None:
    """证书剩余有效天数, 向下取整; 到期或不可解析返回 None 或负数.

    :param cert_file: 证书路径
    :param now: 注入当前时刻, 便于测试
    """
    expires_at = certificate_not_after_epoch(cert_file)
    if expires_at is None:
        return None
    current = time.time() if now is None else now
    return int((expires_at - current) // 86400)
