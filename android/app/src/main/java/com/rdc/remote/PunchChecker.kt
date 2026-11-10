// ==========================================================================================
// 文件        : PunchChecker.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : RDCP1 双向 UDP 打洞探测: 100 ms 节奏、2000 ms/候选对、5000 ms 层预算
// 架构定位    : 连接策略的真实探测器, 只测量连通性, 不判定是否成功
// 核心功能    : Q/R 探测包编解码; 双向响应计数; RTT 采样; 观测对端来源端点
// 职责边界    : 不选候选、不做数据面; 成功判据由 PathSelector 负责
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34); 单测跑 JVM
// 依赖关系    : Candidate、ConnectBudget、PathReason
// ==========================================================================================

package com.rdc.remote

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.net.SocketTimeoutException
import java.security.SecureRandom

/** 一次打洞探测的结果. */
internal data class PunchResult(
    val success: Boolean = false,
    val forward: Int = 0,
    val backward: Int = 0,
    val sent: Int = 0,
    val rttSamples: List<Double> = emptyList(),
    val peerAddress: String = "",
    val peerPort: Int = 0,
    val elapsedMs: Long = 0,
    val reason: String = PathReason.TIMEOUT,
    /** 实际参与打洞的端点列表, 便于对照日志判断探测范围. */
    val targets: List<String> = emptyList(),
)

/** RDCP1 探测包编解码, 与桌面 punch.py 逐字节一致. */
internal object PunchProbe {
    private val MAGIC = "RDCP1".toByteArray(Charsets.US_ASCII)
    private const val KIND_REQUEST = 'Q'.code.toByte()
    private const val KIND_REPLY = 'R'.code.toByte()
    private const val TOKEN_HEX_CHARS = 16
    private val random = SecureRandom()

    fun newToken(): String {
        val bytes = ByteArray(TOKEN_HEX_CHARS / 2).also { random.nextBytes(it) }
        val builder = StringBuilder(TOKEN_HEX_CHARS)
        for (byte in bytes) {
            builder.append("%02x".format(byte.toInt() and 0xff))
        }
        return builder.toString()
    }

    fun encodeRequest(token: String): ByteArray = encode(token, KIND_REQUEST)

    fun encodeReply(token: String): ByteArray = encode(token, KIND_REPLY)

    /** 返回 (是否请求, 令牌); 非本协议包返回 null. */
    fun decode(data: ByteArray): Pair<Boolean, String>? {
        if (data.size != MAGIC.size + 1 + TOKEN_HEX_CHARS) {
            return null
        }
        for (index in MAGIC.indices) {
            if (data[index] != MAGIC[index]) {
                return null
            }
        }
        val kind = data[MAGIC.size]
        if (kind != KIND_REQUEST && kind != KIND_REPLY) {
            return null
        }
        val token = String(data, MAGIC.size + 1, TOKEN_HEX_CHARS, Charsets.US_ASCII)
        return (kind == KIND_REQUEST) to token
    }

    private fun encode(token: String, kind: Byte): ByteArray {
        val tokenBytes = token.toByteArray(Charsets.US_ASCII)
        val packet = ByteArray(MAGIC.size + 1 + tokenBytes.size)
        System.arraycopy(MAGIC, 0, packet, 0, MAGIC.size)
        packet[MAGIC.size] = kind
        System.arraycopy(tokenBytes, 0, packet, MAGIC.size + 1, tokenBytes.size)
        return packet
    }
}

/**
 * 在共享 UDP socket 上做双向打洞探测.
 *
 * 每个候选对最多 [perCandidateMs], 整体不超过 [budgetMs]; 100 ms 发一次 Q, 收到 Q 立即回 R,
 * 收到匹配 R 记一次 RTT. 成功必须双向各 ≥2 响应且 RTT 样本 ≥3.
 */
internal class PunchChecker(private val socket: DatagramSocket) {

    fun run(
        peer: List<Candidate>,
        budgetMs: Int = ConnectBudget.L2_BUDGET_MS,
        perCandidateMs: Int = ConnectBudget.L2_PER_PAIR_MS,
        shouldAbort: () -> Boolean = { false },
    ): PunchResult {
        val started = System.nanoTime()
        val active = peer.filter {
            it.family == CandidateSpec.FAMILY_V4 &&
                it.port in 1..65535 &&
                it.address.isNotEmpty() &&
                (it.kind == CandidateSpec.KIND_HOST || it.kind == CandidateSpec.KIND_SRFLX)
        }
        if (active.isEmpty()) {
            return PunchResult(reason = PathReason.NO_CANDIDATE)
        }
        val overallDeadline = started + budgetMs.coerceAtLeast(1).toLong() * NANOS_PER_MS
        val pending = HashMap<String, Long>()
        val samples = ArrayList<Double>()
        var forward = 0
        var backward = 0
        var sent = 0
        var peerEndpoint: InetSocketAddress? = null
        val buffer = ByteArray(2048)

        for (candidate in active) {
            if (shouldAbort() || System.nanoTime() >= overallDeadline) {
                break
            }
            val target = try {
                InetSocketAddress(candidate.address, candidate.port)
            } catch (_: Exception) {
                continue
            }
            val pairDeadline = minOf(
                System.nanoTime() + perCandidateMs.coerceAtLeast(1).toLong() * NANOS_PER_MS,
                overallDeadline,
            )
            var nextSend = System.nanoTime()
            while (System.nanoTime() < pairDeadline) {
                if (shouldAbort()) {
                    break
                }
                val now = System.nanoTime()
                if (now >= nextSend) {
                    val token = PunchProbe.newToken()
                    try {
                        val payload = PunchProbe.encodeRequest(token)
                        socket.send(DatagramPacket(payload, payload.size, target))
                        pending[token] = System.nanoTime()
                        sent += 1
                    } catch (_: Exception) {
                        // 单次发送失败不终止探测, 等下一轮
                    }
                    nextSend = now + ConnectBudget.CHECK_INTERVAL_MS * NANOS_PER_MS
                }
                val waitMs = minOf(
                    (nextSend - System.nanoTime()) / NANOS_PER_MS,
                    (pairDeadline - System.nanoTime()) / NANOS_PER_MS,
                ).coerceIn(1L, 50L)
                try {
                    socket.soTimeout = waitMs.toInt()
                    val packet = DatagramPacket(buffer, buffer.size)
                    socket.receive(packet)
                    val data = packet.data.copyOfRange(packet.offset, packet.offset + packet.length)
                    val decoded = PunchProbe.decode(data) ?: continue
                    peerEndpoint = InetSocketAddress(packet.address, packet.port)
                    if (decoded.first) {
                        // 对端主动探测到达本端: 立即回 R, 记一次反向可达
                        try {
                            val reply = PunchProbe.encodeReply(decoded.second)
                            socket.send(DatagramPacket(reply, reply.size, packet.address, packet.port))
                        } catch (_: Exception) {
                        }
                        backward += 1
                    } else {
                        val sentAt = pending.remove(decoded.second) ?: continue
                        samples.add((System.nanoTime() - sentAt) / 1_000_000.0)
                        forward += 1
                    }
                } catch (_: SocketTimeoutException) {
                    continue
                } catch (_: Exception) {
                    if (shouldAbort()) {
                        break
                    }
                }
            }
            if (isSuccess(forward, backward, samples.size)) {
                break
            }
        }
        runCatching { socket.soTimeout = 0 }
        val elapsed = (System.nanoTime() - started) / NANOS_PER_MS
        val success = isSuccess(forward, backward, samples.size)
        return PunchResult(
            success = success,
            forward = forward,
            backward = backward,
            sent = sent,
            rttSamples = samples,
            peerAddress = peerEndpoint?.address?.hostAddress.orEmpty(),
            peerPort = peerEndpoint?.port ?: 0,
            elapsedMs = elapsed,
            targets = active.map { "${it.address}:${it.port}" },
            reason = punchReason(
                PunchResult(
                    success = success,
                    forward = forward,
                    backward = backward,
                    rttSamples = samples,
                ),
            ),
        )
    }

    private fun isSuccess(forward: Int, backward: Int, samples: Int): Boolean =
        forward >= ConnectBudget.SUCCESS_MIN_RESPONSES &&
            backward >= ConnectBudget.SUCCESS_MIN_RESPONSES &&
            samples >= ConnectBudget.SUCCESS_MIN_RTT_SAMPLES

    private companion object {
        const val NANOS_PER_MS = 1_000_000L
    }
}
