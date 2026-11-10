# ==========================================================================================
# 文件        : tls_cert.py
# 所属目录    : poc/signaling/
# 文件用途    : 为 PoC 信令服务生成自签 TLS 证书, 供 WSS 使用
# 架构定位    : PoC 信令层辅助模块, 不进入发布路径
# 核心功能    : 生成 RSA 私钥与自签证书, 写入构建根临时目录
# 职责边界    : 只生成与落盘证书; 不启动服务, 不做证书分发
# 运行环境    : Python 3.12; 依赖 cryptography
# 依赖关系    : 被 signaling.py 与 PoC 启动脚本引用
# ==========================================================================================

"""PoC 自签 TLS 证书生成模块."""

from __future__ import annotations

import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def ensure_self_signed_cert(cert_path: Path, key_path: Path) -> tuple[Path, Path]:
    """确保自签证书存在, 不存在则生成.

    :param cert_path: 证书输出路径
    :type cert_path: Path
    :param key_path: 私钥输出路径
    :type key_path: Path
    :return: 证书与私钥路径
    :rtype: tuple
    """
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "RDC PoC"),
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ]
    )
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=7))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path
