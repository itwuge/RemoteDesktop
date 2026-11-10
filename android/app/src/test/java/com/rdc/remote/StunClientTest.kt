// ==========================================================================================
// 文件        : StunClientTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : STUN 客户端的纯 JVM 单测 (spec ipv6-punch §7.5、附录 A.1-S8)
// 架构定位    : 协议层单测; 不依赖 Android 运行时, 用本机回环 UDP 起假 STUN 服务
// 核心功能    : RFC 5769 已知向量解析、事务 ID/消息类型校验、EIM/SYMMETRIC 判定、1500 ms 预算
// 职责边界    : 不测打洞、不测数据面、不测真机网络
// 运行环境    : JDK 17, JUnit 4
// 依赖关系    : StunClient
// ==========================================================================================

package com.rdc.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress

class StunClientTest {

    @Test
    fun `rfc 5769 ipv4 response vector is parsed`() {
        val txid = bytes(0xb7, 0xe7, 0xa7, 0x01, 0xbc, 0x34, 0xd6, 0x86, 0xfa, 0x87, 0xdf, 0xae)
        val response = bytes(
            0x01, 0x01, 0x00, 0x0c, 0x21, 0x12, 0xa4, 0x42,
            0xb7, 0xe7, 0xa7, 0x01, 0xbc, 0x34, 0xd6, 0x86, 0xfa, 0x87, 0xdf, 0xae,
            0x00, 0x20, 0x00, 0x08,
            0x00, 0x01, 0xa1, 0x47, 0xe1, 0x12, 0xa6, 0x43,
        )
        val endpoint = StunClient.parseBindingResponse(response, txid)
        assertEquals("192.0.2.1", endpoint?.address)
        assertEquals(32853, endpoint?.port ?: -1)
        assertEquals(CandidateSpec.FAMILY_V4, endpoint?.family ?: -1)
    }

    @Test
    fun `ipv6 xor mapped address is parsed`() {
        val txid = ByteArray(12) { (it + 1).toByte() }
        val expected = InetAddress.getByName("2001:db8::1").hostAddress
        val response = buildResponse(txid, "2001:db8::1", 0x1234)
        val endpoint = StunClient.parseBindingResponse(response, txid)
        assertEquals(expected, endpoint?.address)
        assertEquals(0x1234, endpoint?.port ?: -1)
        assertEquals(CandidateSpec.FAMILY_V6, endpoint?.family ?: -1)
    }

    @Test
    fun `wrong cookie transaction or message type is rejected`() {
        val txid = ByteArray(12) { it.toByte() }
        val response = buildResponse(txid, "203.0.113.7", 40000)
        assertNull(StunClient.parseBindingResponse(response, ByteArray(12) { (it + 9).toByte() }))
        val wrongType = response.copyOf()
        wrongType[1] = 0x03
        assertNull(StunClient.parseBindingResponse(wrongType, txid))
        val wrongCookie = response.copyOf()
        wrongCookie[4] = 0x00
        assertNull(StunClient.parseBindingResponse(wrongCookie, txid))
        assertNull(StunClient.parseBindingResponse(ByteArray(8), txid))
    }

    @Test
    fun `mapping classification follows unique endpoint rule`() {
        val a = StunEndpoint("203.0.113.7", 40000)
        val b = StunEndpoint("203.0.113.7", 40001)
        assertEquals(StunClient.MAPPING_EIM, StunClient.classifyMapping(listOf(a, a)))
        assertEquals(StunClient.MAPPING_SYMMETRIC, StunClient.classifyMapping(listOf(a, b)))
        assertEquals(StunClient.MAPPING_UNKNOWN, StunClient.classifyMapping(listOf(a)))
        assertEquals(StunClient.MAPPING_UNKNOWN, StunClient.classifyMapping(emptyList()))
    }

    @Test
    fun `two servers with same mapping report eim`() {
        FakeStunServer("203.0.113.7", 40000).use { first ->
            FakeStunServer("203.0.113.7", 40000).use { second ->
                DatagramSocket().use { client ->
                    val result = StunClient.probe(client, listOf(first.server, second.server), 1500)
                    assertEquals(StunClient.MAPPING_EIM, result.mapping)
                    assertEquals(2, result.endpoints.size)
                    assertEquals(2, result.attempts)
                    assertEquals("", result.error)
                }
            }
        }
    }

    @Test
    fun `two servers with different mapping report symmetric`() {
        FakeStunServer("203.0.113.7", 40000).use { first ->
            FakeStunServer("203.0.113.7", 40001).use { second ->
                DatagramSocket().use { client ->
                    val result = StunClient.probe(client, listOf(first.server, second.server), 1500)
                    assertEquals(StunClient.MAPPING_SYMMETRIC, result.mapping)
                    assertEquals(2, result.endpoints.map { it.port }.toSet().size)
                }
            }
        }
    }

    @Test
    fun `budget expires after three sends per server`() {
        DatagramSocket(0, InetAddress.getByName("127.0.0.1")).use { silentA ->
            DatagramSocket(0, InetAddress.getByName("127.0.0.1")).use { silentB ->
                val servers = listOf(
                    StunServer("127.0.0.1", silentA.localPort),
                    StunServer("127.0.0.1", silentB.localPort),
                )
                DatagramSocket().use { client ->
                    val result = StunClient.probe(client, servers, StunClient.TOTAL_BUDGET_MS)
                    assertEquals("stun_timeout", result.error)
                    assertEquals(StunClient.MAPPING_UNKNOWN, result.mapping)
                    assertTrue(result.endpoints.isEmpty())
                    assertEquals(StunClient.MAX_SENDS * servers.size, result.attempts)
                    assertTrue(
                        "elapsed=${result.elapsedMs}",
                        result.elapsedMs >= StunClient.TOTAL_BUDGET_MS - 100,
                    )
                    assertTrue("elapsed=${result.elapsedMs}", result.elapsedMs < 4000)
                }
            }
        }
    }

    private class FakeStunServer(address: String, port: Int) : AutoCloseable {

        private val socket = DatagramSocket(0, InetAddress.getByName("127.0.0.1"))
        val server = StunServer("127.0.0.1", socket.localPort)

        @Volatile
        private var running = true

        private val thread = Thread({
            val buffer = ByteArray(2048)
            while (running) {
                try {
                    val packet = DatagramPacket(buffer, buffer.size)
                    socket.receive(packet)
                    val request = packet.data.copyOfRange(packet.offset, packet.offset + packet.length)
                    if (request.size < 20) {
                        continue
                    }
                    val response = buildResponse(request.copyOfRange(8, 20), address, port)
                    socket.send(DatagramPacket(response, response.size, packet.address, packet.port))
                } catch (_: Exception) {
                    break
                }
            }
        }, "fake-stun").apply {
            isDaemon = true
            start()
        }

        override fun close() {
            running = false
            socket.close()
            thread.join(500)
        }
    }

    private companion object {

        const val MAGIC_COOKIE = 0x2112A442

        fun buildResponse(txid: ByteArray, address: String, port: Int): ByteArray {
            val ip = InetAddress.getByName(address).address
            val value = ByteArray(4 + ip.size)
            value[0] = 0
            value[1] = if (ip.size == 4) 1 else 2
            val xport = port xor (MAGIC_COOKIE ushr 16)
            value[2] = ((xport ushr 8) and 0xff).toByte()
            value[3] = (xport and 0xff).toByte()
            val cookie = bytes(0x21, 0x12, 0xa4, 0x42)
            val mask = if (ip.size == 4) cookie else cookie + ByteArray(12)
            for (index in ip.indices) {
                value[4 + index] = (ip[index].toInt() xor mask[index].toInt()).toByte()
            }
            val packet = ByteArray(20 + 4 + value.size)
            packet[0] = 0x01
            packet[1] = 0x01
            val length = 4 + value.size
            packet[2] = ((length ushr 8) and 0xff).toByte()
            packet[3] = (length and 0xff).toByte()
            packet[4] = 0x21
            packet[5] = 0x12
            packet[6] = 0xa4.toByte()
            packet[7] = 0x42
            System.arraycopy(txid, 0, packet, 8, 12)
            packet[20] = 0x00
            packet[21] = 0x20
            packet[22] = 0
            packet[23] = value.size.toByte()
            System.arraycopy(value, 0, packet, 24, value.size)
            return packet
        }

        fun bytes(vararg values: Int): ByteArray = ByteArray(values.size) { values[it].toByte() }
    }
}
