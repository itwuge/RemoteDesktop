// ==========================================================================================
// 文件        : RelayTls.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 公网中继与账号中心共用的 TLS 1.3 连接与证书链校验
// 架构定位    : 传输层安全工具; 唯一信任锚为 res/raw/relay_ca.crt
// 核心功能    : 只用内置 CA 校验服务端证书; 默认按 relay.wuge.fun 发送 SNI, 可按连接覆盖; 强制 TLS 1.3
// 职责边界    : 不建业务连接、不读令牌、不做明文降级
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35)
// 依赖关系    : 被 LanSession 与 AndroidAccountTransport 调用
// ==========================================================================================

package com.rdc.remote

import android.content.Context
import java.net.InetSocketAddress
import java.net.Socket
import java.security.KeyStore
import java.security.cert.CertificateFactory
import javax.net.ssl.SNIHostName
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.TrustManagerFactory

internal object RelayTls {

    /** 证书 CN 与部署域名; 按 IP 直连时只作 SNI, 链校验交给内置 CA. */
    const val SNI = "relay.wuge.fun"

    /** 建 TCP 并按 TLS 1.3 包装; 任一步失败抛异常, 由调用方翻译成中文原因. */
    fun connect(
        context: Context,
        host: String,
        port: Int,
        connectTimeoutMs: Int,
        readTimeoutMs: Int,
        serverName: String? = null,
    ): SSLSocket {
        val raw = Socket().apply {
            tcpNoDelay = true
            connect(InetSocketAddress(host, port), connectTimeoutMs)
            soTimeout = readTimeoutMs
        }
        return try {
            wrap(context, raw, host, port, serverName)
        } catch (error: Throwable) {
            try {
                raw.close()
            } catch (_: Exception) {
            }
            throw error
        }
    }

    /** 把已建立的 TCP 连接升级为 TLS 1.3; 未指定证书域名时沿用中继 SNI. */
    fun wrap(context: Context, raw: Socket, host: String, port: Int, serverName: String? = null): SSLSocket {
        val ssl = factory(context).createSocket(raw, host, port, true) as SSLSocket
        ssl.sslParameters = ssl.sslParameters.apply {
            protocols = arrayOf("TLSv1.3")
            serverNames = listOf(SNIHostName(serverName?.takeIf { it.isNotBlank() } ?: SNI))
        }
        ssl.startHandshake()
        return ssl
    }

    /** 只用 res/raw/relay_ca.crt 作为信任锚, 不信任系统根证书, 防止公网中间人. */
    fun factory(context: Context): SSLSocketFactory {
        val certificateFactory = CertificateFactory.getInstance("X.509")
        val ca = context.applicationContext.resources.openRawResource(R.raw.relay_ca).use {
            certificateFactory.generateCertificate(it)
        }
        val store = KeyStore.getInstance(KeyStore.getDefaultType()).apply {
            load(null)
            setCertificateEntry("rdc-relay-ca", ca)
        }
        val managers = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply {
            init(store)
        }
        val sslContext = SSLContext.getInstance("TLSv1.3").apply {
            init(null, managers.trustManagers, java.security.SecureRandom())
        }
        return sslContext.socketFactory
    }
}
