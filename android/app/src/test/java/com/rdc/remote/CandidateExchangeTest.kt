// ==========================================================================================
// 文件        : CandidateExchangeTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : offer/answer 候选交换容错单测 (spec ipv6-punch §3.4、§7.6)
// 架构定位    : 只测纯数据转换; 不建 socket、不依赖 Android 运行时
// 核心功能    : candidates 数组解析、chosen_candidate 兜底、全缺失兜底、脏数据不崩
// 职责边界    : 不测采集与选路
// 运行环境    : JDK 17, JUnit 4
// 依赖关系    : CandidateExchange、Candidate
// ==========================================================================================

package com.rdc.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CandidateExchangeTest {

    private val fallback = relayCandidate("47.109.32.249", 8750)

    @Test
    fun `parse candidates array keeps every usable item`() {
        val payload = mapOf(
            "candidates" to listOf(
                mapOf("kind" to "srflx", "address" to "1.2.3.4", "port" to 5000, "family" to 4),
                mapOf("kind" to "host", "address" to "240e::1", "port" to 27888, "family" to 6),
            ),
        )
        val result = parseAnswerCandidates(payload, fallback)
        assertEquals(2, result.size)
        assertTrue(result.any { it.kind == CandidateSpec.KIND_SRFLX })
        assertTrue(result.any { it.family == CandidateSpec.FAMILY_V6 })
    }

    @Test
    fun `missing candidates falls back to chosen candidate`() {
        val payload = mapOf(
            "chosen_candidate" to mapOf("kind" to "relay", "address" to "7.7.7.7", "port" to 8750),
        )
        val result = parseAnswerCandidates(payload, fallback)
        assertEquals(1, result.size)
        assertEquals("7.7.7.7", result[0].address)
    }

    @Test
    fun `missing everything falls back to single relay candidate`() {
        val result = parseAnswerCandidates(emptyMap<String, Any?>(), fallback)
        assertEquals(1, result.size)
        assertEquals(fallback.address, result[0].address)
        assertEquals(fallback.port, result[0].port)
    }

    @Test
    fun `null payload falls back to single relay candidate`() {
        val result = parseAnswerCandidates(null, fallback)
        assertEquals(1, result.size)
        assertEquals(fallback.port, result[0].port)
    }

    @Test
    fun `malformed items are skipped without crash`() {
        val payload = mapOf(
            "candidates" to listOf(
                "not-a-map",
                42,
                mapOf("kind" to "bogus", "address" to "", "port" to 0),
            ),
        )
        val result = parseAnswerCandidates(payload, fallback)
        assertEquals(1, result.size)
        assertEquals(fallback.port, result[0].port)
    }

    @Test
    fun `offer filters loopback and link local`() {
        val candidates = listOf(
            hostCandidate("127.0.0.1", 1234),
            hostCandidate("10.0.0.2", 1234),
            srflxCandidate("1.2.3.4", 5000, "10.0.0.2"),
        )
        val offer = offerCandidates(candidates)
        assertTrue(offer.none { it.address == "127.0.0.1" })
        assertTrue(offer.any { it.address == "10.0.0.2" })
        assertTrue(offer.any { it.kind == CandidateSpec.KIND_SRFLX })
    }

    @Test
    fun `wire map contains every schema field`() {
        val item = srflxCandidate("1.2.3.4", 5000, "10.0.0.2")
        val wire = candidateWireMap(item)
        assertEquals(
            setOf("kind", "address", "port", "priority", "family", "scope", "origin", "related", "proto"),
            wire.keys,
        )
        assertEquals("srflx", wire["kind"])
    }
}
