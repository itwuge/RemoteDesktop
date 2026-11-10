// ==========================================================================================
// 文件        : PathSelectorTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : 选路判据纯 JVM 单测 (spec ipv6-punch §4.2、§7.8)
// 架构定位    : 只测纯函数; 不建 socket、不依赖 Android 运行时
// 核心功能    : 对称短路、EIM 进 L2、hairpin 剔除、reason 口径与预算常量
// 职责边界    : 不测真实打洞; 打洞由 PunchCheckerTest 覆盖
// 运行环境    : JDK 17, JUnit 4
// 依赖关系    : PathSelector、Candidate、StunClient
// ==========================================================================================

package com.rdc.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PathSelectorTest {

    @Test
    fun `symmetric local mapping skips L2`() {
        val peer = listOf(srflxCandidate("1.2.3.4", 5000, "10.0.0.2"))
        val gate = gatePunch(true, StunClient.MAPPING_SYMMETRIC, false, peer)
        assertFalse(gate.attempt)
        assertEquals(PathReason.SYMMETRIC_NAT, gate.reason)
    }

    @Test
    fun `mapped candidate allows L2 under symmetric`() {
        val peer = listOf(srflxCandidate("1.2.3.4", 5000, "10.0.0.2"))
        val gate = gatePunch(true, StunClient.MAPPING_SYMMETRIC, true, peer)
        assertTrue(gate.attempt)
        assertEquals(PathReason.OK, gate.reason)
    }

    @Test
    fun `eim with srflx candidate enters L2`() {
        val peer = listOf(srflxCandidate("1.2.3.4", 5000, "10.0.0.2"))
        val gate = gatePunch(true, StunClient.MAPPING_EIM, false, peer)
        assertTrue(gate.attempt)
    }

    @Test
    fun `punch disabled short circuits`() {
        val peer = listOf(srflxCandidate("1.2.3.4", 5000, "10.0.0.2"))
        val gate = gatePunch(false, StunClient.MAPPING_EIM, false, peer)
        assertFalse(gate.attempt)
        assertEquals(PathReason.PUNCH_DISABLED, gate.reason)
    }

    @Test
    fun `relay only peer has no punch candidate`() {
        val peer = listOf(relayCandidate("47.109.32.249", 8750))
        val gate = gatePunch(true, StunClient.MAPPING_EIM, false, peer)
        assertFalse(gate.attempt)
        assertEquals(PathReason.NO_CANDIDATE, gate.reason)
    }

    @Test
    fun `peer symmetric forces symmetric`() {
        val peer = listOf(
            srflxCandidate("1.1.1.1", 100, "10.0.0.2"),
            srflxCandidate("1.1.1.1", 200, "10.0.0.2"),
        )
        assertEquals(StunClient.MAPPING_SYMMETRIC, classifyPeerMapping(peer))
        assertEquals(StunClient.MAPPING_SYMMETRIC, effectiveMapping(StunClient.MAPPING_EIM, peer))
    }

    @Test
    fun `same egress hairpin candidates removed`() {
        val local = listOf(srflxCandidate("9.9.9.9", 4000, "10.0.0.2"))
        val peer = listOf(
            srflxCandidate("9.9.9.9", 5000, "10.0.0.3"),
            srflxCandidate("8.8.8.8", 5000, "10.0.0.3"),
        )
        val (kept, removed) = removeHairpin(peer, local)
        assertEquals(1, removed)
        assertEquals(1, kept.size)
        assertEquals("8.8.8.8", kept[0].address)
    }

    @Test
    fun `hairpin removal is a no-op without local srflx`() {
        val peer = listOf(srflxCandidate("8.8.8.8", 5000, "10.0.0.3"))
        val (kept, removed) = removeHairpin(peer, emptyList())
        assertEquals(0, removed)
        assertEquals(1, kept.size)
    }

    @Test
    fun `punch reason timeout when both directions weak`() {
        val result = PunchResult(success = false, forward = 1, backward = 0, rttSamples = listOf(12.0))
        assertEquals(PathReason.TIMEOUT, punchReason(result))
    }

    @Test
    fun `punch reason single direction when only forward works`() {
        val result = PunchResult(
            success = false,
            forward = 5,
            backward = 0,
            rttSamples = listOf(12.0, 13.0, 14.0),
        )
        assertEquals(PathReason.SINGLE_DIRECTION, punchReason(result))
    }

    @Test
    fun `punch reason single direction when rtt samples insufficient`() {
        val result = PunchResult(
            success = false,
            forward = 2,
            backward = 2,
            rttSamples = listOf(12.0),
        )
        assertEquals(PathReason.SINGLE_DIRECTION, punchReason(result))
    }

    @Test
    fun `budgets match desktop`() {
        assertEquals(1500, ConnectBudget.L1_BUDGET_MS)
        assertEquals(5000, ConnectBudget.L2_BUDGET_MS)
        assertEquals(8000, ConnectBudget.L3_BUDGET_MS)
        assertEquals(2000, ConnectBudget.L2_PER_PAIR_MS)
        assertEquals(100, ConnectBudget.CHECK_INTERVAL_MS)
        assertEquals(2, ConnectBudget.SUCCESS_MIN_RESPONSES)
        assertEquals(3, ConnectBudget.SUCCESS_MIN_RTT_SAMPLES)
    }
}
