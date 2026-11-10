// ==========================================================================================
// 文件        : PunchCheckerTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : RDCP1 打洞探测真 socket 单测 (spec ipv6-punch §4.2、§7.6)
// 架构定位    : 用 127.0.0.1 上的真实 UDP socket 覆盖双向/单向/超时三种判据
// 核心功能    : 编解码往返; 双向成功; 仅正向判 single_direction; 无对端判 timeout
// 职责边界    : 不测 NAT 与真实公网; 不测数据面
// 运行环境    : JDK 17, JUnit 4
// 依赖关系    : PunchChecker、PunchProbe、Candidate
// ==========================================================================================

package com.rdc.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketTimeoutException
import java.util.concurrent.atomic.AtomicBoolean

class PunchCheckerTest {

    @Test
    fun `probe encode decode round trip`() {
        val token = PunchProbe.newToken()
        assertEquals(16, token.length)
        val request = PunchProbe.decode(PunchProbe.encodeRequest(token))
        assertNotNull(request)
        assertTrue(request!!.first)
        assertEquals(token, request.second)

        val reply = PunchProbe.decode(PunchProbe.encodeReply(token))
        assertNotNull(reply)
        assertFalse(reply!!.first)
        assertEquals(token, reply.second)
    }

    @Test
    fun `bidirectional responder reaches direct udp`() {
        val loopback = InetAddress.getByName("127.0.0.1")
        val checker = DatagramSocket(0, loopback)
        val responder = DatagramSocket(0, loopback)
        val running = AtomicBoolean(true)
        val responderThread = Thread { respond(checker.localPort, responder, loopback, running, sendQueries = true) }
        responderThread.isDaemon = true
        responderThread.start()
        try {
            val candidate = punchCandidate(responder.localPort)
            val result = PunchChecker(checker).run(
                peer = listOf(candidate),
                budgetMs = 3000,
                perCandidateMs = 3000,
            )
            assertTrue(result.success)
            assertTrue(result.forward >= ConnectBudget.SUCCESS_MIN_RESPONSES)
            assertTrue(result.backward >= ConnectBudget.SUCCESS_MIN_RESPONSES)
            assertTrue(result.rttSamples.size >= ConnectBudget.SUCCESS_MIN_RTT_SAMPLES)
            assertEquals(PathReason.OK, result.reason)
            assertEquals("127.0.0.1", result.peerAddress)
        } finally {
            running.set(false)
            responderThread.join(500)
            checker.close()
            responder.close()
        }
    }

    @Test
    fun `only reply responder is single direction`() {
        val loopback = InetAddress.getByName("127.0.0.1")
        val checker = DatagramSocket(0, loopback)
        val responder = DatagramSocket(0, loopback)
        val running = AtomicBoolean(true)
        val responderThread = Thread { respond(checker.localPort, responder, loopback, running, sendQueries = false) }
        responderThread.isDaemon = true
        responderThread.start()
        try {
            val candidate = punchCandidate(responder.localPort)
            val result = PunchChecker(checker).run(
                peer = listOf(candidate),
                budgetMs = 1200,
                perCandidateMs = 1200,
            )
            assertFalse(result.success)
            assertTrue(result.forward >= ConnectBudget.SUCCESS_MIN_RESPONSES)
            assertEquals(0, result.backward)
            assertEquals(PathReason.SINGLE_DIRECTION, result.reason)
        } finally {
            running.set(false)
            responderThread.join(500)
            checker.close()
            responder.close()
        }
    }

    @Test
    fun `no responder ends with timeout`() {
        val loopback = InetAddress.getByName("127.0.0.1")
        val checker = DatagramSocket(0, loopback)
        val dead = DatagramSocket(0, loopback)
        val deadPort = dead.localPort
        dead.close()
        try {
            val candidate = punchCandidate(deadPort)
            val result = PunchChecker(checker).run(
                peer = listOf(candidate),
                budgetMs = 600,
                perCandidateMs = 600,
            )
            assertFalse(result.success)
            assertEquals(PathReason.TIMEOUT, result.reason)
        } finally {
            checker.close()
        }
    }

    private fun punchCandidate(port: Int): Candidate = Candidate(
        kind = CandidateSpec.KIND_HOST,
        address = "127.0.0.1",
        port = port,
        priority = CandidateSpec.PRIORITY_HOST_V4_PRIVATE,
        family = CandidateSpec.FAMILY_V4,
        scope = CandidateSpec.SCOPE_PRIVATE,
        origin = CandidateSpec.ORIGIN_HOST,
        proto = CandidateSpec.PROTO_UDP,
    )

    /**
     * 对端探测应答器.
     *
     * [sendQueries] 为 true 时同时每 100 ms 主动发 Q, 证明反向可达; 为 false 时只回 R.
     */
    private fun respond(
        checkerPort: Int,
        responder: DatagramSocket,
        loopback: InetAddress,
        running: AtomicBoolean,
        sendQueries: Boolean,
    ) {
        val target = InetSocketAddress(loopback, checkerPort)
        val buffer = ByteArray(2048)
        var lastQuery = 0L
        while (running.get()) {
            val now = System.currentTimeMillis()
            if (sendQueries && now - lastQuery >= ConnectBudget.CHECK_INTERVAL_MS) {
                lastQuery = now
                val query = PunchProbe.encodeRequest(PunchProbe.newToken())
                runCatching { responder.send(DatagramPacket(query, query.size, target)) }
            }
            runCatching { responder.soTimeout = 30 }
            try {
                val packet = DatagramPacket(buffer, buffer.size)
                responder.receive(packet)
                val data = packet.data.copyOfRange(packet.offset, packet.offset + packet.length)
                val decoded = PunchProbe.decode(data) ?: continue
                if (decoded.first) {
                    val reply = PunchProbe.encodeReply(decoded.second)
                    runCatching {
                        responder.send(DatagramPacket(reply, reply.size, packet.address, packet.port))
                    }
                }
            } catch (_: SocketTimeoutException) {
                continue
            } catch (_: Exception) {
                return
            }
        }
    }
}
