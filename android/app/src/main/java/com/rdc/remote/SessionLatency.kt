// ==========================================================================================
// 文件        : SessionLatency.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 会话 ping/pong 往返采样与 p50/p95 统计
// 架构定位    : 纯 JVM 统计组件, 供 LanSession 采集、ControlContract 出指标
// 核心功能    : 1 s 探针记账; 30 样本窗口; 少于 3 样本不出分位; 5 s 未回丢弃
// 职责边界    : 不建连、不发包、不判断会话状态; 探测失败只丢样本
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35); JVM 单测
// 依赖关系    : 被 LanSession 调用; 口径对齐 src/rdc/session/remote.py
// ==========================================================================================

package com.rdc.remote

/** 延迟快照; [samples] 为窗口内实际样本数, 不足 [LatencyTracker.LATENCY_MIN_SAMPLES] 时分为 null. */
internal data class LatencySnapshot(
    val samples: Int,
    val p50Ms: Double?,
    val p95Ms: Double?,
)

/**
 * 会话延迟采样.
 *
 * 与桌面 `src/rdc/session/remote.py` 同口径: 周期 1 s, 窗口 30, 最少 3 个样本才出 p50/p95,
 * 5 s 未收到 pong 的探针丢弃; 分位取最近秩, 恰好 .5 时按 Python `round` 取偶数, 不做插值.
 */
internal class LatencyTracker(
    private val window: Int = LATENCY_WINDOW,
    private val minSamples: Int = LATENCY_MIN_SAMPLES,
    private val timeoutNanos: Long = PING_TIMEOUT_NANOS,
) {
    private val samples = ArrayDeque<Long>()
    private val pending = HashMap<Long, Long>()
    private var nextSeq = 1L

    /** 登记一次待回探针并返回序号; 发送失败时由 [expire] 自然丢弃. */
    @Synchronized
    fun nextProbe(nowNanos: Long): Long {
        val seq = nextSeq
        nextSeq += 1
        pending[seq] = nowNanos
        return seq
    }

    /** 收到 pong 记一个 RTT 样本; 未知序号(过期或已重置)返回 false 且不计数. */
    @Synchronized
    fun onPong(seq: Long, nowNanos: Long): Boolean {
        val sentAt = pending.remove(seq) ?: return false
        samples.addLast((nowNanos - sentAt).coerceAtLeast(0L))
        while (samples.size > window) {
            samples.removeFirst()
        }
        return true
    }

    /** 丢弃超过超时仍未回的探针. */
    @Synchronized
    fun expire(nowNanos: Long) {
        pending.entries.removeAll { nowNanos - it.value > timeoutNanos }
    }

    /** 通道切换/会话重连时清空, 旧通道的 pong 不再计入. */
    @Synchronized
    fun reset() {
        samples.clear()
        pending.clear()
    }

    @Synchronized
    fun snapshot(): LatencySnapshot {
        val ordered = samples.map { it / NANOS_PER_MILLI }.sorted()
        if (ordered.isEmpty()) {
            return LatencySnapshot(0, null, null)
        }
        if (ordered.size < minSamples) {
            return LatencySnapshot(ordered.size, null, null)
        }
        return LatencySnapshot(
            ordered.size,
            percentile(ordered, 0.5),
            percentile(ordered, 0.95),
        )
    }

    private fun percentile(ordered: List<Double>, ratio: Double): Double =
        ordered[nearestRankIndex(ordered.size, ratio)]

    private fun nearestRankIndex(size: Int, ratio: Double): Int {
        val raw = (size - 1) * ratio
        val floor = kotlin.math.floor(raw)
        val index = when {
            raw - floor < 0.5 -> floor.toInt()
            raw - floor > 0.5 -> floor.toInt() + 1
            floor.toLong() % 2L == 0L -> floor.toInt()
            else -> floor.toInt() + 1
        }
        return index.coerceIn(0, size - 1)
    }

    companion object {
        /** 探针周期, 与桌面 PING_INTERVAL_S 一致. */
        const val PING_INTERVAL_MS = 1_000L

        /** 滑动窗口样本数, 与桌面 LATENCY_WINDOW 一致. */
        const val LATENCY_WINDOW = 30

        /** 出分位所需最少样本, 与桌面 LATENCY_MIN_SAMPLES 一致. */
        const val LATENCY_MIN_SAMPLES = 3

        /** 探针超时, 与桌面 PING_TIMEOUT_S 一致. */
        const val PING_TIMEOUT_NANOS = 5_000_000_000L

        private const val NANOS_PER_MILLI = 1_000_000.0
    }
}
