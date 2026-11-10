// ==========================================================================================
// 文件        : AndroidAccountTransport.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 账号中心 HTTP/1.1 传输实现, 走内置 CA 校验的 TLS 1.3
// 架构定位    : 传输层; 与桌面端 src/rdc/accounts/remote.py 的 _https_request 对齐
// 核心功能    : 解析 endpoint; 拼 HTTP 请求; 按 Content-Length 读包络; 强制 Connection: close
// 职责边界    : 不解析业务字段、不重试业务请求、不落盘任何内容
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35)
// 依赖关系    : 实现 AccountTransport, 由 RdcAccountService 注入 RemoteAccountClient
// ==========================================================================================

package com.rdc.remote

import android.content.Context
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.URI

internal class AndroidAccountTransport(
    context: Context,
    endpoint: String,
    private val timeoutMs: Int = DEFAULT_TIMEOUT_MS,
) : AccountTransport {

    private val appContext = context.applicationContext
    private val uri = URI(endpoint.trim())
    private val host: String = uri.host ?: ""
    private val port: Int = if (uri.port > 0) uri.port else 443
    private val hostHeader: String = if (uri.port > 0) "${uri.host}:${uri.port}" else (uri.host ?: "")

    init {
        require(uri.scheme.equals("https", ignoreCase = true)) { "账号中心地址必须使用 https" }
        require(host.isNotBlank()) { "账号中心地址缺少主机" }
    }

    override fun request(
        method: String,
        path: String,
        headers: Map<String, String>,
        body: ByteArray,
    ): AccountHttpResponse {
        RelayTls.connect(appContext, host, port, timeoutMs, timeoutMs).use { socket ->
            val output: OutputStream = socket.outputStream
            val head = buildString {
                append("$method $path HTTP/1.1\r\n")
                append("Host: $hostHeader\r\n")
                append("User-Agent: rdc-account-client/1\r\n")
                for ((name, value) in headers) {
                    append("$name: $value\r\n")
                }
                append("Content-Length: ${body.size}\r\n")
                append("Connection: close\r\n\r\n")
            }
            output.write(head.toByteArray(Charsets.US_ASCII))
            if (body.isNotEmpty()) {
                output.write(body)
            }
            output.flush()
            val input = BufferedInputStream(socket.inputStream)
            val (status, responseBody) = readResponse(input)
            return AccountHttpResponse(status, responseBody)
        }
    }

    /** 读状态行与头部, 按 Content-Length 或连接关闭读取正文. */
    private fun readResponse(input: InputStream): Pair<Int, String> {
        val statusLine = readLine(input) ?: throw RemoteBadResponse("账号中心未返回状态行")
        val parts = statusLine.split(' ', limit = 3)
        if (parts.size < 2) {
            throw RemoteBadResponse("账号中心状态行无法解析")
        }
        val status = parts[1].toIntOrNull() ?: throw RemoteBadResponse("账号中心状态码无法解析")
        var contentLength = -1
        while (true) {
            val line = readLine(input) ?: break
            if (line.isEmpty()) {
                break
            }
            val colon = line.indexOf(':')
            if (colon <= 0) {
                continue
            }
            val name = line.substring(0, colon).trim().lowercase()
            val value = line.substring(colon + 1).trim()
            if (name == "content-length") {
                contentLength = value.toIntOrNull() ?: -1
            }
        }
        val buffer = ByteArrayOutputStream()
        if (contentLength >= 0) {
            val chunk = ByteArray(DEFAULT_CHUNK)
            var remaining = contentLength
            while (remaining > 0) {
                val read = input.read(chunk, 0, minOf(chunk.size, remaining))
                if (read < 0) {
                    break
                }
                buffer.write(chunk, 0, read)
                remaining -= read
            }
        } else {
            buffer.write(input.readBytes())
        }
        return status to String(buffer.toByteArray(), Charsets.UTF_8)
    }

    private fun readLine(input: InputStream): String? {
        val line = ByteArrayOutputStream()
        while (true) {
            val value = input.read()
            if (value < 0) {
                return if (line.size() == 0) null else String(line.toByteArray(), Charsets.US_ASCII)
            }
            if (value == '\n'.code) {
                break
            }
            if (value != '\r'.code) {
                line.write(value)
            }
            if (line.size() > MAX_HEADER_LINE) {
                throw RemoteBadResponse("账号中心响应头过长")
            }
        }
        return String(line.toByteArray(), Charsets.US_ASCII)
    }

    companion object {
        const val DEFAULT_TIMEOUT_MS = 8000
        private const val DEFAULT_CHUNK = 4096
        private const val MAX_HEADER_LINE = 16 * 1024
    }
}
