// ==========================================================================================
// 文件        : SessionLatencyTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : LatencyTracker 单测: 样本不足、窗口滑动、通道切换、探针超时
// 架构定位    : 纯 JVM 单测, 不依赖 Android 框架
// 核心功能    : 校验 p50/p95 最近秩口径与桌面 remote.py 一致
// 职责边界    : 不测网络、不测界面
// 运行环境    : JVM (JUnit 4)
// 依赖关系    : LatencyTracker、LatencySnapshot
// ==========================================================================================

package com.rdc.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SessionLatencyTest {

    private fun ms(value: Long): Long = value * 1_000_000L

    @Test
    fun `样本不足时不输出分位只报实际数量`() {
        val tracker = LatencyTracker()
        repeat(2) { index ->
            val seq = tracker.nextProbe(ms(index * 100L))
            assertTrue(tracker.onPong(seq, ms(index * 100L) + ms(20L)))
        }
        val snapshot = tracker.snapshot()
        assertEquals(2, snapshot.samples)
        assertNull(snapshot.p50Ms)
        assertNull(snapshot.p95Ms)
    }

    @Test
    fun `窗口滑动只保留最近样本`() {
        val tracker = LatencyTracker(window = 3, minSamples = 3)
        val rtts = listOf(10L, 20L, 30L, 40L, 50L)
        rtts.forEach { rtt ->
            val seq = tracker.nextProbe(0L)
            assertTrue(tracker.onPong(seq, ms(rtt)))
        }
        val snapshot = tracker.snapshot()
        assertEquals(3, snapshot.samples)
        // 窗口保留 30/40/50, p50 索引 round(1.0)=1
        assertEquals(40.0, snapshot.p50Ms!!, 0.001)
        assertEquals(50.0, snapshot.p95Ms!!, 0.001)
    }

    @Test
    fun `通道切换重置样本并忽略旧探针`() {
        val tracker = LatencyTracker()
        repeat(3) {
            val seq = tracker.nextProbe(0L)
            assertTrue(tracker.onPong(seq, ms(20L)))
        }
        assertEquals(3, tracker.snapshot().samples)

        val staleSeq = tracker.nextProbe(0L)
        tracker.reset()
        assertEquals(0, tracker.snapshot().samples)
        // 旧通道的 pong 在重置后到达, 不得计入新通道
        assertFalse(tracker.onPong(staleSeq, ms(20L)))
        assertEquals(0, tracker.snapshot().samples)

        val freshSeq = tracker.nextProbe(0L)
        assertTrue(tracker.onPong(freshSeq, ms(15L)))
        assertEquals(1, tracker.snapshot().samples)
    }

    @Test
    fun `超时未回的探针不计入样本`() {
        val tracker = LatencyTracker()
        val timeoutSeq = tracker.nextProbe(0L)
        tracker.expire(ms(6_000L))
        assertFalse(tracker.onPong(timeoutSeq, ms(6_001L)))
        assertEquals(0, tracker.snapshot().samples)
    }

    @Test
    fun `分位取最近秩且半数按偶数取舍`() {
        val tracker = LatencyTracker()
        listOf(10L, 20L, 30L, 40L).forEach { rtt ->
            val seq = tracker.nextProbe(0L)
            assertTrue(tracker.onPong(seq, ms(rtt)))
        }
        val snapshot = tracker.snapshot()
        // n=4: p50 索引 round(1.5)=2 -> 30; p95 索引 round(2.85)=3 -> 40
        assertEquals(30.0, snapshot.p50Ms!!, 0.001)
        assertEquals(40.0, snapshot.p95Ms!!, 0.001)
    }
}
