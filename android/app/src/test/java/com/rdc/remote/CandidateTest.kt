// ==========================================================================================
// 文件        : CandidateTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : 候选模型、优先级表与序列化容错的纯 JVM 单测 (spec ipv6-punch §3、§7.4)
// 架构定位    : 协议数据结构单测; 不建连、不依赖 Android 运行时
// 核心功能    : 全字段默认值、opt* 容错、未知 kind 按 relay、family/scope 推断、去重排序
// 职责边界    : 不测采集与选路; 不测网络
// 运行环境    : JDK 17, JUnit 4
// 依赖关系    : Candidate、CandidateSpec
// ==========================================================================================

package com.rdc.remote

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CandidateTest {

    @Test
    fun `candidate has defaults for every field`() {
        val candidate = Candidate()
        assertEquals(CandidateSpec.KIND_RELAY, candidate.kind)
        assertEquals("", candidate.address)
        assertEquals(0, candidate.port)
        assertEquals(0, candidate.priority)
        assertEquals(CandidateSpec.FAMILY_UNKNOWN, candidate.family)
        assertEquals("", candidate.scope)
        assertEquals("", candidate.origin)
        assertEquals("", candidate.related)
        assertEquals("", candidate.proto)
    }

    @Test
    fun `json round trip keeps every field`() {
        val original = Candidate(
            kind = CandidateSpec.KIND_SRFLX,
            address = "106.12.251.193",
            port = 49188,
            priority = 90,
            family = 4,
            scope = "",
            origin = "stun",
            related = "192.168.1.23",
            proto = "udp",
        )
        val restored = Candidate.fromJson(original.toJson())
        assertEquals(original, restored)
    }

    @Test
    fun `unknown keys are ignored and missing optional keys take defaults`() {
        val payload = JSONObject()
            .put("kind", "host")
            .put("address", "192.168.1.10")
            .put("port", 27888)
            .put("priority", 96)
            .put("future_field", "ignored")
            .put("extra_object", JSONObject().put("x", 1))
        val candidate = Candidate.fromJson(payload)
        assertEquals(CandidateSpec.KIND_HOST, candidate.kind)
        assertEquals(4, candidate.family)
        assertEquals(CandidateSpec.SCOPE_PRIVATE, candidate.scope)
        assertEquals(CandidateSpec.ORIGIN_HOST, candidate.origin)
        assertEquals(CandidateSpec.PROTO_UDP, candidate.proto)
        assertEquals(96, candidate.priority)
    }

    @Test
    fun `unknown kind including mapped is treated as relay`() {
        val mapped = Candidate.fromJson(JSONObject().put("kind", "mapped").put("address", "1.2.3.4").put("port", 1))
        assertEquals(CandidateSpec.KIND_RELAY, mapped.kind)
        val bogus = Candidate.fromJson(JSONObject().put("kind", "quantum").put("address", "1.2.3.4").put("port", 1))
        assertEquals(CandidateSpec.KIND_RELAY, bogus.kind)
    }

    @Test
    fun `family is inferred from literal when missing and zero for hostnames`() {
        assertEquals(6, Candidate.fromJson(JSONObject().put("address", "240e:370::1")).family)
        assertEquals(4, Candidate.fromJson(JSONObject().put("address", "10.0.0.2")).family)
        assertEquals(0, Candidate.fromJson(JSONObject().put("address", "relay.example.test")).family)
        assertEquals(0, Candidate.fromJson(null).family)
    }

    @Test
    fun `null object and malformed array elements do not throw`() {
        assertNotNull(Candidate.fromJson(null))
        val array = JSONArray().put(JSONObject().put("kind", "host").put("address", "192.168.0.2")).put(42)
        val parsed = parseCandidates(array)
        assertEquals(1, parsed.size)
        assertTrue(parseCandidates(null).isEmpty())
    }

    @Test
    fun `host priority table matches spec`() {
        val globalV6 = hostCandidate("240e:370:6303:cb0::1", 27888)
        assertEquals(100, globalV6.priority)
        assertEquals(6, globalV6.family)
        assertEquals(CandidateSpec.SCOPE_GLOBAL, globalV6.scope)
        assertEquals(CandidateSpec.PROTO_TCP, globalV6.proto)

        val ulaV6 = hostCandidate("fd00::1", 27888)
        assertEquals(70, ulaV6.priority)
        assertEquals(CandidateSpec.SCOPE_ULA, ulaV6.scope)

        val privateV4 = hostCandidate("192.168.1.10", 49152)
        assertEquals(96, privateV4.priority)
        assertEquals(CandidateSpec.SCOPE_PRIVATE, privateV4.scope)
        assertEquals(CandidateSpec.PROTO_UDP, privateV4.proto)

        val publicV4 = hostCandidate("8.8.8.8", 49152)
        assertEquals(85, publicV4.priority)
        assertEquals("", publicV4.scope)
    }

    @Test
    fun `loopback and link local are never publishable`() {
        val loopback = hostCandidate("127.0.0.1", 27888)
        assertEquals(0, loopback.priority)
        assertFalse(isPublishable(loopback))
        val linkLocal = hostCandidate("fe80::1", 27888)
        assertEquals(0, linkLocal.priority)
        assertFalse(isPublishable(linkLocal))
        assertTrue(isPublishable(hostCandidate("240e:370::1", 27888)))
    }

    @Test
    fun `srflx and relay candidates keep fixed priority and origin`() {
        val srflx = srflxCandidate("106.12.251.193", 49188, "192.168.1.23")
        assertEquals(90, srflx.priority)
        assertEquals(CandidateSpec.ORIGIN_STUN, srflx.origin)
        assertEquals("192.168.1.23", srflx.related)
        assertEquals(CandidateSpec.PROTO_UDP, srflx.proto)

        val relay = relayCandidate("47.109.32.249", 8750)
        assertEquals(10, relay.priority)
        assertEquals(4, relay.family)
        assertEquals(CandidateSpec.ORIGIN_RELAY, relay.origin)
        assertEquals(CandidateSpec.PROTO_TCP, relay.proto)

        val hostnameRelay = relayCandidate("relay.example.test", 8750)
        assertEquals(0, hostnameRelay.family)
        assertTrue(isPublishable(hostnameRelay))
    }

    @Test
    fun `dedupe keeps higher priority and sort follows spec key`() {
        val low = srflxCandidate("203.0.113.7", 40000, "")
        val high = low.copy(priority = 96)
        val deduped = dedupeCandidates(listOf(low, high))
        assertEquals(1, deduped.size)
        assertEquals(96, deduped.first().priority)

        val sorted = sortCandidates(
            listOf(
                relayCandidate("47.109.32.249", 8750),
                hostCandidate("240e:370::1", 27888),
                hostCandidate("192.168.1.10", 49152),
            ),
        )
        assertEquals(listOf(100, 96, 10), sorted.map { it.priority })
    }
}
