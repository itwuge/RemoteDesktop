// ==========================================================================================
// 文件        : StunClient.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : RFC 5389 Binding 客户端: 请求构造、XOR-MAPPED-ADDRESS 解析、映射行为判定
// 架构定位    : 连接策略的纯客户端实现, 只做 RFC 5389 最小子集; 不判路、不开新 socket
// 核心功能    : 500 ms 单次超时、最多 3 次发送、双服务器并行、1500 ms 总预算、EIM/SYMMETRIC
// 职责边界    : 不建 TCP、不做 UPnP/TURN; socket 由候选采集层创建并贯穿打洞与数据面
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34); 单测跑 JVM
// 依赖关系    : 被 CandidateGatherer 调用; 返回 StunResult 供候选生成与选路判定
// ==========================================================================================

package com.rdc.remote

import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.security.SecureRandom
import kotlin.math.round

/** 一个 STUN 服务端点. */
internal data class StunServer(val host: String, val port: Int)

/** STUN 反射出的公网映射端点. */
internal data class StunEndpoint(val address: String, val port: Int) {
    val family: Int
        get() = if (address.contains(':')) CandidateSpec.FAMILY_V6 else CandidateSpec.FAMILY_V4
}

/** 一次探测的结果; [mapping] 取 [StunClient.MAPPING_EIM] / [StunClient.MAPPING_SYMMETRIC] / unknown. */
internal data class StunResult(
    val endpoints: List<StunEndpoint> = emptyList(),
    val mapping: String = StunClient.MAPPING_UNKNOWN,
    val rttMs: List<Double> = emptyList(),
    val attempts: Int = 0,
    val elapsedMs: Long = 0,
    val error: String = "",
)

/**
 * STUN 客户端.
 *
 * 两个服务器共用同一 socket 并行发送: 各自独立事务 ID, 先发后收, 按事务 ID 归位,
 * 从而保证两次映射查询看到的是同一个本地端口. 单服务器 500 ms 未响应即重发, 最多 3 次,
 * 总预算 1500 ms.
 */
internal object StunClient {

    const val MAPPING_EIM = "endpoint-independent"
    const val MAPPING_SYMMETRIC = "address-or-port-dependent"
    const val MAPPING_UNKNOWN = "unknown"

    /** 单次等待 500 ms; 3 次发送正好覆盖 1500 ms 总预算. */
    const val PER_REQUEST_TIMEOUT_MS = 500
    const val MAX_SENDS = 3
    const val TOTAL_BUDGET_MS = 1500

    /** 默认与桌面一致: 两个不同 IP 的 STUN 服务. */
    val DEFAULT_SERVERS: List<StunServer> = listOf(
        StunServer("47.109.32.249", 3478),
        StunServer("106.12.251.193", 3478),
    )

    private const val MAGIC_COOKIE = 0x2112A442
    private const val BINDING_REQUEST = 0x0001
    private const val BINDING_SUCCESS = 0x0101
    private const val ATTR_XOR_MAPPED_ADDRESS = 0x0020
    private const val TRANSACTION_ID_BYTES = 12
    private const val NANOS_PER_MS = 1_000_000L

    private val random = SecureRandom()

    /** 随机 96 位事务 ID. */
    fun newTransactionId(): ByteArray = ByteArray(TRANSACTION_ID_BYTES).also { random.nextBytes(it) }

    /** 构造 Binding 请求 (20 字节, 无属性). */
    fun buildBindingRequest(transactionId: ByteArray): ByteArray {
        require(transactionId.size == TRANSACTION_ID_BYTES) { "事务 ID 必须为 96 位" }
        val packet = ByteArray(20)
        packet[0] = ((BINDING_REQUEST ushr 8) and 0xff).toByte()
        packet[1] = (BINDING_REQUEST and 0xff).toByte()
        packet[4] = ((MAGIC_COOKIE ushr 24) and 0xff).toByte()
        packet[5] = ((MAGIC_COOKIE ushr 16) and 0xff).toByte()
        packet[6] = ((MAGIC_COOKIE ushr 8) and 0xff).toByte()
        packet[7] = (MAGIC_COOKIE and 0xff).toByte()
        System.arraycopy(transactionId, 0, packet, 8, TRANSACTION_ID_BYTES)
        return packet
    }

    /**
     * 解析 Binding 成功响应; cookie、事务 ID、消息类型任一不符或缺少 XOR-MAPPED-ADDRESS 返回 null.
     */
    fun parseBindingResponse(data: ByteArray, transactionId: ByteArray): StunEndpoint? {
        if (data.size < 20 || transactionId.size != TRANSACTION_ID_BYTES) {
            return null
        }
        val messageType = readU16(data, 0)
        val messageLength = readU16(data, 2)
        if (readU32(data, 4) != MAGIC_COOKIE) {
            return null
        }
        for (index in 0 until TRANSACTION_ID_BYTES) {
            if (data[8 + index] != transactionId[index]) {
                return null
            }
        }
        if (messageType != BINDING_SUCCESS) {
            return null
        }
        val end = minOf(data.size, 20 + messageLength)
        var offset = 20
        while (offset + 4 <= end) {
            val attrType = readU16(data, offset)
            val attrLength = readU16(data, offset + 2)
            val valueStart = offset + 4
            if (valueStart + attrLength > data.size) {
                return null
            }
            if (attrType == ATTR_XOR_MAPPED_ADDRESS && attrLength >= 8) {
                return decodeXorMappedAddress(data, valueStart, attrLength)
            }
            offset = valueStart + ((attrLength + 3) / 4) * 4
        }
        return null
    }

    /**
     * 按「映射端点是否唯一」判定映射行为.
     *
     * 两个服务器返回同一端点 → [MAPPING_EIM]; 端点不一致 → [MAPPING_SYMMETRIC];
     * 只有一个成功或全部失败 → [MAPPING_UNKNOWN]: 单次观测无法区分 EIM 与端口受限, 不据此宣称可打洞.
     */
    fun classifyMapping(endpoints: List<StunEndpoint>): String {
        if (endpoints.size < 2) {
            return MAPPING_UNKNOWN
        }
        val unique = endpoints.map { "${it.address}:${it.port}" }.toSet()
        return if (unique.size == 1) MAPPING_EIM else MAPPING_SYMMETRIC
    }

    /**
     * 在同一 socket 上并行查询 [servers], 返回映射端点与判定结果.
     *
     * 只收发 UDP, 不改变 socket 的对端; 采集结束后该 socket 继续用于打洞与数据面.
     */
    fun probe(
        socket: DatagramSocket,
        servers: List<StunServer> = DEFAULT_SERVERS,
        budgetMs: Int = TOTAL_BUDGET_MS,
    ): StunResult {
        val started = System.nanoTime()
        val deadline = started + budgetMs.coerceAtLeast(1).toLong() * NANOS_PER_MS
        val pending = servers
            .filter { it.host.isNotBlank() && it.port in 1..65535 }
            .distinctBy { "${it.host}|${it.port}" }
            .map { Pending(it, newTransactionId()) }
        if (pending.isEmpty()) {
            return StunResult(error = "no_server")
        }
        val buffer = ByteArray(2048)
        var error = ""
        while (System.nanoTime() < deadline && pending.any { it.endpoint == null }) {
            val now = System.nanoTime()
            for (item in pending) {
                if (item.endpoint != null || item.sends >= MAX_SENDS) {
                    continue
                }
                val due = item.lastSentNanos + PER_REQUEST_TIMEOUT_MS * NANOS_PER_MS
                if (item.sends > 0 && now < due) {
                    continue
                }
                try {
                    val payload = buildBindingRequest(item.transactionId)
                    val target = InetAddress.getByName(item.server.host)
                    socket.send(DatagramPacket(payload, payload.size, target, item.server.port))
                    item.sends += 1
                    item.lastSentNanos = System.nanoTime()
                } catch (exc: IOException) {
                    error = "${exc.javaClass.simpleName}: ${exc.message}"
                    item.sends = MAX_SENDS
                }
            }
            val nextDue = nextSendDue(pending, deadline)
            val waitNanos = minOf(nextDue - System.nanoTime(), deadline - System.nanoTime())
            socket.soTimeout = (waitNanos / NANOS_PER_MS).coerceIn(1, Int.MAX_VALUE.toLong()).toInt()
            try {
                val packet = DatagramPacket(buffer, buffer.size)
                socket.receive(packet)
                val data = packet.data.copyOfRange(packet.offset, packet.offset + packet.length)
                for (item in pending) {
                    if (item.endpoint != null) {
                        continue
                    }
                    val endpoint = parseBindingResponse(data, item.transactionId) ?: continue
                    item.endpoint = endpoint
                    item.rttMs = (System.nanoTime() - item.lastSentNanos) / 1_000_000.0
                    break
                }
            } catch (_: SocketTimeoutException) {
                continue
            } catch (exc: SocketException) {
                error = "${exc.javaClass.simpleName}: ${exc.message}"
                break
            } catch (exc: IOException) {
                error = "${exc.javaClass.simpleName}: ${exc.message}"
                break
            }
        }
        val endpoints = pending.mapNotNull { it.endpoint }
        val rtts = pending.filter { it.endpoint != null }.map { round(it.rttMs * 10) / 10.0 }
        val elapsed = (System.nanoTime() - started) / NANOS_PER_MS
        if (endpoints.isEmpty() && error.isEmpty()) {
            error = "stun_timeout"
        }
        // 采集结束还原为阻塞读, 打洞与数据面的超时由后续阶段自行设置
        runCatching { socket.soTimeout = 0 }
        return StunResult(
            endpoints = endpoints,
            mapping = classifyMapping(endpoints),
            rttMs = rtts,
            attempts = pending.sumOf { it.sends },
            elapsedMs = elapsed,
            error = error,
        )
    }

    private fun nextSendDue(pending: List<Pending>, deadline: Long): Long {
        var due = deadline
        for (item in pending) {
            if (item.endpoint != null || item.sends >= MAX_SENDS) {
                continue
            }
            val itemDue = if (item.sends == 0) {
                System.nanoTime()
            } else {
                item.lastSentNanos + PER_REQUEST_TIMEOUT_MS * NANOS_PER_MS
            }
            due = minOf(due, itemDue)
        }
        return due
    }

    private fun decodeXorMappedAddress(data: ByteArray, valueStart: Int, attrLength: Int): StunEndpoint? {
        val family = data[valueStart + 1].toInt() and 0xff
        val xport = readU16(data, valueStart + 2)
        val port = xport xor (MAGIC_COOKIE ushr 16)
        val cookie = ByteArray(4) {
            ((MAGIC_COOKIE ushr (24 - it * 8)) and 0xff).toByte()
        }
        val rawLength = when (family) {
            0x01 -> 4
            0x02 -> 16
            else -> return null
        }
        if (attrLength < 4 + rawLength) {
            return null
        }
        val mask = if (rawLength == 4) cookie else cookie + ByteArray(12)
        val addressBytes = ByteArray(rawLength) { index ->
            (data[valueStart + 4 + index].toInt() xor mask[index].toInt()).toByte()
        }
        val address = try {
            InetAddress.getByAddress(addressBytes)
        } catch (_: UnknownHostException) {
            return null
        }
        return StunEndpoint(address.hostAddress ?: return null, port)
    }

    private fun readU16(data: ByteArray, offset: Int): Int =
        ((data[offset].toInt() and 0xff) shl 8) or (data[offset + 1].toInt() and 0xff)

    private fun readU32(data: ByteArray, offset: Int): Int =
        ((data[offset].toInt() and 0xff) shl 24) or
            ((data[offset + 1].toInt() and 0xff) shl 16) or
            ((data[offset + 2].toInt() and 0xff) shl 8) or
            (data[offset + 3].toInt() and 0xff)

    private class Pending(val server: StunServer, val transactionId: ByteArray) {
        var sends: Int = 0
        var lastSentNanos: Long = 0
        var endpoint: StunEndpoint? = null
        var rttMs: Double = 0.0
    }
}
