// ==========================================================================================
// 文件        : FrameCodecTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : v2 二进制帧编解码与帧版本协商的纯 JVM 单测
// 架构定位    : 协议层单测; 不依赖 Android 运行时、不建网络连接
// 核心功能    : 逐字段校验 28 字节大端头; 截断/长度不符/超限/CRC 错误显式拒绝; v1 兼容解码
// 职责边界    : 只测 FrameCodec, 不测 LanSession 建连
// 运行环境    : JDK 17, JUnit 4
// 依赖关系    : FrameCodec
// ==========================================================================================

package com.rdc.remote

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.util.Base64
import java.util.zip.CRC32

class FrameCodecTest {

    private val payload = byteArrayOf(0x00, 0x00, 0x00, 0x01, 0x67, 0x42, 0x00, 0x1e, 0x25.toByte())

    @Test
    fun `v2 header fields decode exactly`() {
        val packet = buildV2(seq = 4294967295L, pts = 42L, width = 1920, height = 1080, keyframe = true, crc = true)

        val frame = FrameCodec.decodeV2(packet)

        assertEquals(4294967295L, frame.seq)
        assertEquals(42L, frame.pts)
        assertEquals(1920, frame.width)
        assertEquals(1080, frame.height)
        assertTrue(frame.keyframe)
        assertArrayEquals(payload, frame.payload)
    }

    @Test
    fun `v2 without crc flag is accepted`() {
        val packet = buildV2(seq = 1, pts = 7, width = 1280, height = 720, keyframe = false, crc = false)

        val frame = FrameCodec.decodeV2(packet)

        assertFalse(frame.keyframe)
        assertEquals(720, frame.height)
    }

    @Test
    fun `v2 crc mismatch is rejected`() {
        val packet = buildV2(seq = 9, pts = 9, width = 640, height = 360, keyframe = true, crc = true)
        packet[FrameCodec.HEADER_SIZE + 2] = (packet[FrameCodec.HEADER_SIZE + 2].toInt() xor 0xff).toByte()

        assertCode(FrameCodec.FRAME_CRC_MISMATCH) { FrameCodec.decodeV2(packet) }
    }

    @Test
    fun `v2 bad magic is rejected`() {
        val packet = buildV2(seq = 1, pts = 1, width = 16, height = 16, keyframe = true, crc = false)
        packet[0] = 'X'.code.toByte()

        assertCode(FrameCodec.FRAME_BAD_MAGIC) { FrameCodec.decodeV2(packet) }
    }

    @Test
    fun `v2 unknown version is rejected`() {
        val packet = buildV2(seq = 1, pts = 1, width = 16, height = 16, keyframe = true, crc = false)
        packet[2] = 3

        assertCode(FrameCodec.FRAME_UNSUPPORTED_VERSION) { FrameCodec.decodeV2(packet) }
    }

    @Test
    fun `v2 truncated header is rejected`() {
        val packet = buildV2(seq = 1, pts = 1, width = 16, height = 16, keyframe = true, crc = false)

        assertCode(FrameCodec.FRAME_TRUNCATED) { FrameCodec.decodeV2(packet.copyOfRange(0, 20)) }
    }

    @Test
    fun `v2 truncated body is rejected`() {
        val packet = buildV2(seq = 1, pts = 1, width = 16, height = 16, keyframe = true, crc = false)

        assertCode(FrameCodec.FRAME_TRUNCATED) { FrameCodec.decodeV2(packet.copyOfRange(0, packet.size - 1)) }
    }

    @Test
    fun `v2 declared length shorter than actual is rejected`() {
        val packet = buildV2(seq = 1, pts = 1, width = 16, height = 16, keyframe = true, crc = false)
        packet[23] = (packet[23] - 1).toByte()

        assertCode(FrameCodec.FRAME_LENGTH_MISMATCH) { FrameCodec.decodeV2(packet) }
    }

    @Test
    fun `v2 payload above hard limit is rejected before allocation`() {
        val packet = ByteArray(FrameCodec.HEADER_SIZE)
        packet[0] = 0x52
        packet[1] = 0x46
        packet[2] = 2
        writeUInt32(packet, 20, FrameCodec.MAX_PAYLOAD_BYTES + 1L)

        assertCode(FrameCodec.FRAME_TOO_LARGE) { FrameCodec.decodeV2(packet) }
    }

    @Test
    fun `only RF prefix is treated as v2`() {
        assertTrue(FrameCodec.isV2(buildV2(0, 0, 2, 2, true, false)))
        assertFalse(FrameCodec.isV2("{\"kind\":\"video\"}".toByteArray()))
        assertFalse(FrameCodec.isV2(byteArrayOf(0x52)))
    }

    @Test
    fun `v1 json video frame decodes base64 payload`() {
        val frame = FrameCodec.decodeV1(
            mapOf(
                "kind" to "video",
                "data" to Base64.getEncoder().encodeToString(payload),
                "seq" to 12,
                "pts" to 34,
                "w" to 1280,
                "h" to 720,
                "key" to true,
                "fps" to 30,
            ),
        )

        assertEquals(12L, frame.seq)
        assertEquals(34L, frame.pts)
        assertEquals(1280, frame.width)
        assertEquals(720, frame.height)
        assertTrue(frame.keyframe)
        assertArrayEquals(payload, frame.payload)
    }

    @Test
    fun `v1 non video message is rejected`() {
        assertCode(FrameCodec.FRAME_KIND_MISMATCH) {
            FrameCodec.decodeV1(mapOf("kind" to "ping"))
        }
    }

    @Test
    fun `v1 invalid base64 is rejected`() {
        assertCode(FrameCodec.FRAME_BAD_PAYLOAD) {
            FrameCodec.decodeV1(mapOf("kind" to "video", "data" to "!!!not-base64!!!"))
        }
    }

    @Test
    fun `frame version negotiation prefers v2 and falls back to v1`() {
        assertEquals(FrameCodec.VERSION_V2, FrameCodec.negotiateVersion(mapOf("frame_versions" to listOf(1, 2))))
        assertEquals(FrameCodec.VERSION_V1, FrameCodec.negotiateVersion(mapOf("frame_versions" to listOf(1))))
        assertEquals(FrameCodec.VERSION_V1, FrameCodec.negotiateVersion(mapOf("frame_versions" to listOf(3, 4))))
        assertEquals(FrameCodec.VERSION_V2, FrameCodec.negotiateVersion(mapOf("frame_version" to 2)))
        assertEquals(FrameCodec.VERSION_V1, FrameCodec.negotiateVersion(mapOf("codecs" to listOf("h264"))))
        assertEquals(FrameCodec.VERSION_V1, FrameCodec.negotiateVersion(null))
        assertEquals(FrameCodec.VERSION_V1, FrameCodec.negotiateVersion(emptyMap<Any, Any>()))
    }

    @Test
    fun `encodeV2 round trips through decodeV2`() {
        val original = FrameCodec.DecodedFrame(
            seq = 65536,
            pts = 90,
            width = 1918,
            height = 1078,
            keyframe = true,
            payload = payload,
        )

        val decoded = FrameCodec.decodeV2(FrameCodec.encodeV2(original, withCrc = true))

        assertEquals(original.seq, decoded.seq)
        assertEquals(original.pts, decoded.pts)
        assertEquals(original.width, decoded.width)
        assertEquals(original.height, decoded.height)
        assertEquals(original.keyframe, decoded.keyframe)
        assertArrayEquals(original.payload, decoded.payload)
    }

    /** 独立按 `>2sBBIqHHII` 组包, 不复用被测的 encodeV2, 避免同错同过. */
    private fun buildV2(
        seq: Long,
        pts: Long,
        width: Int,
        height: Int,
        keyframe: Boolean,
        crc: Boolean,
    ): ByteArray {
        val flags = (if (keyframe) 1 else 0) or (if (crc) 2 else 0)
        val checksum = if (crc) CRC32().apply { update(payload) }.value else 0L
        val out = ByteArrayOutputStream()
        out.write(0x52)
        out.write(0x46)
        out.write(2)
        out.write(flags)
        out.write(longBytes(seq, 4))
        out.write(longBytes(pts, 8))
        out.write(intBytes(width, 2))
        out.write(intBytes(height, 2))
        out.write(longBytes(payload.size.toLong(), 4))
        out.write(longBytes(checksum, 4))
        out.write(payload)
        return out.toByteArray()
    }

    private fun longBytes(value: Long, size: Int): ByteArray =
        ByteArray(size) { index -> (value ushr ((size - 1 - index) * 8)).toByte() }

    private fun intBytes(value: Int, size: Int): ByteArray = longBytes(value.toLong(), size)

    private fun writeUInt32(data: ByteArray, offset: Int, value: Long) {
        for (index in 0 until 4) {
            data[offset + index] = (value ushr ((3 - index) * 8)).toByte()
        }
    }

    private fun assertCode(expected: String, block: () -> Unit) {
        try {
            block()
            fail("应当抛出 $expected")
        } catch (error: FrameCodec.FrameException) {
            assertEquals(expected, error.code)
        }
    }
}
