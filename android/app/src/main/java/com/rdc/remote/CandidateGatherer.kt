// ==========================================================================================
// 文件        : CandidateGatherer.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 安卓候选采集: 绑定当前蜂窝/Wi-Fi 网络, 枚举 host 候选并做 STUN 反射探测
// 架构定位    : 连接策略的采集层 (spec S8), 只采集与判定映射行为, 不做选路与打洞
// 核心功能    : 单个 IPv4 DatagramSocket 贯穿采集/打洞/数据面; 1500 ms 预算; 网络切换中止
// 职责边界    : 不做 UPnP/NAT-PMP/PCP/TURN; 不改协议字段; 不写 LanSession
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34); 需要 ACCESS_NETWORK_STATE
// 依赖关系    : 依赖 StunClient、Candidate、ConnectLog; 产出 GatherResult 交后续 S9/S10
// ==========================================================================================

package com.rdc.remote

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import java.io.IOException
import java.net.DatagramSocket
import java.net.Inet6Address
import java.net.NetworkInterface
import java.net.SocketException

/** 采集结果; [socket] 非空时必须由调用方持有并贯穿打洞与数据面, 会话结束时关闭. */
internal data class GatherResult(
    val candidates: List<Candidate> = emptyList(),
    val mapping: String = StunClient.MAPPING_UNKNOWN,
    val socket: DatagramSocket? = null,
    val localPort: Int = 0,
    val elapsedMs: Long = 0,
    val networkChanged: Boolean = false,
    val tried: Int = 0,
    val rttMs: List<Double> = emptyList(),
    val error: String = "",
)

/**
 * 候选采集器.
 *
 * 采集发生在 signaling offer 之前, 预算 1500 ms; 超时则只保留 relay 候选.
 * 网络切换由 [ConnectivityManager.NetworkCallback] 捕获: 一旦切换立即关闭当前 socket 并返回
 * `networkChanged=true`, 由选路层重新采集并从 L1 重连, 不静默沿用旧 socket.
 */
internal class CandidateGatherer(private val context: Context) {

    fun gather(
        relayCandidates: List<Candidate> = defaultRelayCandidates(),
        stunServers: List<StunServer> = StunClient.DEFAULT_SERVERS,
        budgetMs: Int = StunClient.TOTAL_BUDGET_MS,
    ): GatherResult {
        val started = System.nanoTime()
        val relay = dedupeCandidates(relayCandidates)
        val manager = context.getSystemService(ConnectivityManager::class.java)
        val network = manager?.activeNetwork
        if (manager == null || network == null) {
            return report(
                GatherResult(
                    candidates = sortCandidates(relay),
                    elapsedMs = elapsedMs(started),
                    error = "no_network",
                ),
            )
        }
        val link = runCatching { manager.getLinkProperties(network) }.getOrNull()

        var socket: DatagramSocket? = null
        var switched = false
        fun abortOnSwitch() {
            if (!switched) {
                switched = true
                runCatching { socket?.close() }
            }
        }

        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(available: Network) {
                if (available != network) {
                    abortOnSwitch()
                }
            }

            override fun onLost(lost: Network) {
                if (lost == network) {
                    abortOnSwitch()
                }
            }
        }
        var watched = false
        try {
            manager.registerDefaultNetworkCallback(callback)
            watched = true
        } catch (_: Exception) {
            // 无 ACCESS_NETWORK_STATE 或系统拒绝时退化为不监听切换, 采集照常进行
        }

        try {
            val created = try {
                DatagramSocket()
            } catch (exc: SocketException) {
                return report(
                    GatherResult(
                        candidates = sortCandidates(relay),
                        elapsedMs = elapsedMs(started),
                        error = "socket_failed: ${exc.javaClass.simpleName}",
                    ),
                )
            }
            socket = created
            try {
                network.bindSocket(created)
            } catch (exc: IOException) {
                created.close()
                return report(
                    GatherResult(
                        candidates = sortCandidates(relay),
                        elapsedMs = elapsedMs(started),
                        error = "bind_failed: ${exc.javaClass.simpleName}",
                    ),
                )
            }
            runCatching {
                created.receiveBufferSize = SOCKET_BUFFER_BYTES
                created.sendBufferSize = SOCKET_BUFFER_BYTES
            }
            if (switched) {
                created.close()
                return networkChangedResult(relay, started)
            }

            val localPort = created.localPort
            val hosts = collectHostCandidates(link, localPort).filter { isPublishable(it) }
            val localV4 = hosts.firstOrNull { it.family == CandidateSpec.FAMILY_V4 }?.address.orEmpty()
            val remainingMs = ((started + budgetMs.coerceAtLeast(1).toLong() * 1_000_000L - System.nanoTime()) / 1_000_000L).toInt()
            val stun = if (remainingMs > 0) {
                StunClient.probe(created, stunServers, minOf(remainingMs, StunClient.TOTAL_BUDGET_MS))
            } else {
                StunResult(error = "budget_exhausted")
            }
            if (switched) {
                created.close()
                return networkChangedResult(relay, started)
            }

            val srflx = stun.endpoints.map { srflxCandidate(it.address, it.port, localV4) }
            val all = sortCandidates(dedupeCandidates(hosts + srflx + relay))
            val reason = when {
                stun.endpoints.isEmpty() -> stun.error.ifEmpty { "stun_timeout" }
                else -> "ok"
            }
            return report(
                GatherResult(
                    candidates = all,
                    mapping = stun.mapping,
                    socket = created,
                    localPort = localPort,
                    elapsedMs = elapsedMs(started),
                    tried = stun.attempts,
                    rttMs = stun.rttMs,
                    error = reason,
                ),
            )
        } finally {
            if (watched) {
                runCatching { manager.unregisterNetworkCallback(callback) }
            }
        }
    }

    private fun networkChangedResult(relay: List<Candidate>, started: Long): GatherResult = report(
        GatherResult(
            candidates = sortCandidates(relay),
            elapsedMs = elapsedMs(started),
            networkChanged = true,
            error = "network_changed",
        ),
    )

    /** 采集结束统一留痕; socket 已关闭时 local_port 记 0. */
    private fun report(result: GatherResult): GatherResult {
        ConnectLog.emit(
            event = "connect.candidate_gather",
            transport = "direct_udp",
            reason = result.error.ifEmpty { "ok" },
            elapsedMs = result.elapsedMs,
            extra = linkedMapOf(
                "candidates" to result.candidates.size,
                "tried" to result.tried,
                "rtt_ms" to result.rttMs,
                "mapping" to result.mapping,
                "local_port" to result.localPort,
                "network_changed" to result.networkChanged,
            ),
            warn = result.error.isNotEmpty() && result.error != "ok",
        )
        return result
    }

    /**
     * 枚举在用网卡地址生成 host 候选; 排除回环、链路本地、组播与不属于当前网络的地址.
     *
     * v6 候选用于 TCP 直连, 端口取被控端同端口 27888; v4 候选用于打洞, 端口取共享 UDP 端口.
     */
    private fun collectHostCandidates(link: LinkProperties?, udpPort: Int): List<Candidate> {
        val bound = link?.linkAddresses.orEmpty()
            .mapNotNull { it.address.hostAddress }
            .toSet()
        val result = ArrayList<Candidate>()
        val interfaces = try {
            NetworkInterface.getNetworkInterfaces()?.toList().orEmpty()
        } catch (_: SocketException) {
            return result
        }
        for (nif in interfaces) {
            val up = runCatching { nif.isUp }.getOrDefault(false)
            if (!up || nif.isLoopback) {
                continue
            }
            for (address in nif.inetAddresses.toList()) {
                if (address.isLoopbackAddress || address.isLinkLocalAddress ||
                    address.isMulticastAddress || address.isAnyLocalAddress
                ) {
                    continue
                }
                val text = address.hostAddress ?: continue
                if (bound.isNotEmpty() && text !in bound) {
                    continue
                }
                val port = if (address is Inet6Address) HOST_TCP_PORT else udpPort
                val candidate = hostCandidate(text, port)
                if (candidate.priority > 0) {
                    result.add(candidate)
                }
            }
        }
        return result
    }

    companion object {

        /** 被控端 TCP 直连端口, 与主机监听同号. */
        const val HOST_TCP_PORT = 27888

        /** UDP 收发缓冲下限 4 MiB, 避免 1080p 突发被内核丢弃. */
        const val SOCKET_BUFFER_BYTES = 4 * 1024 * 1024

        fun defaultRelayCandidates(): List<Candidate> = listOf(
            relayCandidate(LanSession.RELAY_HOST, LanSession.DEFAULT_PORT),
        )
    }
}

private fun elapsedMs(startedNanos: Long): Long = (System.nanoTime() - startedNanos) / 1_000_000L
