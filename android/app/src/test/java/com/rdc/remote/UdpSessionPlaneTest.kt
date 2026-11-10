// ==========================================================================================
// 文件        : UdpSessionPlaneTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : UDP 数据面门面单测 (spec ipv6-punch §5.2、§5.3、§5.4)
// 架构定位    : 用 127.0.0.1 上两条真实 UDP 门面验证分片、重组、ACK 与长度前缀复原
// 核心功能    : 常量对齐; 单分片往返; 多分片往返; 流式 write 拆包
// 职责边界    : 不测公网 NAT、不测 AEAD
// 运行环境    : JDK 17, JUnit 4
// 依赖关系    : UdpSessionPlane
// ==========================================================================================

package com.rdc.remote

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress

class UdpSessionPlaneTest {

    @Test
    fun `wire constants match spec`() {
        assertEquals(16, UdpSessionPlane.HEADER_BYTES)
        assertEquals(1200, UdpSessionPlane.DATAGRAM_BYTES)
        assertEquals(1184, UdpSessionPlane.FRAGMENT_BYTES)
        assertEquals(6 * 1024 * 1024, UdpSessionPlane.MAX_FRAME_BYTES)
        assertEquals(5314, UdpSessionPlane.MAX_FRAGMENT_COUNT)
        assertEquals(8, UdpSessionPlane.MAX_CONCURRENT_FRAMES)
        assertEquals(1000, UdpSessionPlane.HEARTBEAT_MS)
        assertEquals(3000, UdpSessionPlane.PATH_DEAD_MS)
    }

    @Test
    fun `single fragment frame round trip`() {
        val pair = newPair()
        try {
            val body = ByteArray(512) { (it % 251).toByte() }
            val payload = frame(body)
            pair.first.output.write(payload)
            pair.first.output.flush()
            val received = readFully(pair.second, payload.size)
            assertArrayEquals(payload, received)
        } finally {
            pair.first.close()
            pair.second.close()
        }
    }

    @Test
    fun `multi fragment frame round trip`() {
        val pair = newPair()
        try {
            // 3000 字节 → 3 个分片, 覆盖 1184 字节边界
            val body = ByteArray(3000) { ((it * 7) % 253).toByte() }
            val payload = frame(body)
            pair.first.output.write(payload)
            pair.first.output.flush()
            val received = readFully(pair.second, payload.size)
            assertArrayEquals(payload, received)
        } finally {
            pair.first.close()
            pair.second.close()
        }
    }

    @Test
    fun `streamed writes are reassembled into frames`() {
        val pair = newPair()
        try {
            val body = ByteArray(1500) { (it % 97).toByte() }
            val payload = frame(body)
            // 模拟 writeFrame 的两段写: 先 4 字节长度, 再帧体
            pair.first.output.write(payload, 0, 4)
            pair.first.output.flush()
            pair.first.output.write(payload, 4, payload.size - 4)
            pair.first.output.flush()
            val received = readFully(pair.second, payload.size)
            assertArrayEquals(payload, received)
        } finally {
            pair.first.close()
            pair.second.close()
        }
    }

    private fun newPair(): Pair<UdpSessionPlane, UdpSessionPlane> {
        val loopback = InetAddress.getByName("127.0.0.1")
        val socketA = DatagramSocket(0, loopback)
        val socketB = DatagramSocket(0, loopback)
        val planeA = UdpSessionPlane(socketA, InetSocketAddress(loopback, socketB.localPort))
        val planeB = UdpSessionPlane(socketB, InetSocketAddress(loopback, socketA.localPort))
        return planeA to planeB
    }

    /** 4 字节大端长度前缀 + 帧体, 与现有 writeFrame 的线上形态一致. */
    private fun frame(body: ByteArray): ByteArray {
        val out = ByteArray(4 + body.size)
        val length = body.size
        out[0] = (length ushr 24).toByte()
        out[1] = (length ushr 16).toByte()
        out[2] = (length ushr 8).toByte()
        out[3] = length.toByte()
        System.arraycopy(body, 0, out, 4, body.size)
        return out
    }

    private fun readFully(plane: UdpSessionPlane, size: Int, timeoutMs: Long = 4000): ByteArray {
        val result = ByteArray(size)
        val reader = Thread {
            var offset = 0
            while (offset < size) {
                val count = plane.input.read(result, offset, size - offset)
                if (count <= 0) {
                    return@Thread
                }
                offset += count
            }
        }
        reader.isDaemon = true
        reader.start()
        reader.join(timeoutMs)
        assertFalse("读取超时", reader.isAlive)
        return result
    }
}
