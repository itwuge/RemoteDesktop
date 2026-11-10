// ==========================================================================================
// 文件        : FrameCodec.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 视频帧线格式编解码: v2 定长二进制头 + 原始 H.264 净荷, 兼容 v1 JSON+base64
// 架构定位    : 协议封装层, 与 Python 参考实现 src/rdc/media/frame.py 逐字段对齐
// 核心功能    : decodeV2 / decodeV1 / 自动识别; negotiateVersion 帧版本协商; 显式错误码
// 职责边界    : 不建连、不发送、不做画质决策; 不依赖 Android 框架, 纯 JVM 可单测
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35); JVM 单测
// 依赖关系    : 被 LanSession 调用
// ==========================================================================================

package com.rdc.remote

import java.util.Base64
import java.util.zip.CRC32

/**
 * v2 视频帧编解码.
 *
 * 定长头 28 字节, 大端 `>2sBBIqHHII`: magic "RF" / version / flags / seq / pts /
 * width / height / payload_len / crc32(payload). 净荷是编码器输出的原始字节,
 * 不做 base64、不塞进 JSON. v1 `{"kind":"video","data":<base64>}` 只保留解码兼容.
 */
internal object FrameCodec {

    const val VERSION_V1 = 1
    const val VERSION_V2 = 2
    const val HEADER_SIZE = 28
    const val MAX_PAYLOAD_BYTES = 8 * 1024 * 1024

    const val FRAME_TRUNCATED = "FRAME_TRUNCATED"
    const val FRAME_LENGTH_MISMATCH = "FRAME_LENGTH_MISMATCH"
    const val FRAME_BAD_MAGIC = "FRAME_BAD_MAGIC"
    const val FRAME_UNSUPPORTED_VERSION = "FRAME_UNSUPPORTED_VERSION"
    const val FRAME_CRC_MISMATCH = "FRAME_CRC_MISMATCH"
    const val FRAME_TOO_LARGE = "FRAME_TOO_LARGE"
    const val FRAME_BAD_PAYLOAD = "FRAME_BAD_PAYLOAD"
    const val FRAME_KIND_MISMATCH = "FRAME_KIND_MISMATCH"

    private const val FLAG_KEYFRAME = 0x01
    private const val FLAG_CRC32 = 0x02
    private const val MAGIC_FIRST = 0x52
    private const val MAGIC_SECOND = 0x46

    /** 本端支持的帧版本, 按优先级从高到低; v1 永远兜底. */
    val LOCAL_VERSIONS = listOf(VERSION_V2, VERSION_V1)

    /** 一帧视频的元数据与原始净荷. */
    data class DecodedFrame(
        val seq: Long,
        val pts: Long,
        val width: Int,
        val height: Int,
        val keyframe: Boolean,
        val payload: ByteArray,
    )

    /** 帧解码失败; code 为模块局部错误码, 尚未进入契约错误码表. */
    class FrameException(val code: String, message: String) : IllegalArgumentException(message)

    /** 线上明文是否以 v2 魔数 "RF" 开头. */
    fun isV2(packet: ByteArray): Boolean =
        packet.size >= 2 && packet[0].toInt() == MAGIC_FIRST && packet[1].toInt() == MAGIC_SECOND

    /** 解码 v2 二进制帧; 魔数、版本、截断、长度不符、超限与 CRC 不符均显式拒绝. */
    fun decodeV2(packet: ByteArray): DecodedFrame {
        if (packet.size < HEADER_SIZE) {
            throw FrameException(
                FRAME_TRUNCATED,
                "帧头只有 ${packet.size} 字节, 至少需要 $HEADER_SIZE",
            )
        }
        if (!isV2(packet)) {
            throw FrameException(FRAME_BAD_MAGIC, "魔数不符")
        }
        val version = packet[2].toInt() and 0xff
        if (version != VERSION_V2) {
            throw FrameException(FRAME_UNSUPPORTED_VERSION, "未知帧版本: $version")
        }
        val flags = packet[3].toInt() and 0xff
        val seq = readUInt32(packet, 4)
        val pts = readInt64(packet, 8)
        val width = readUInt16(packet, 16)
        val height = readUInt16(packet, 18)
        val payloadLength = readUInt32(packet, 20)
        val checksum = readUInt32(packet, 24)
        if (payloadLength > MAX_PAYLOAD_BYTES) {
            throw FrameException(
                FRAME_TOO_LARGE,
                "声明净荷 $payloadLength 字节超过上限 $MAX_PAYLOAD_BYTES",
            )
        }
        val bodyLength = packet.size - HEADER_SIZE
        if (bodyLength < payloadLength) {
            throw FrameException(
                FRAME_TRUNCATED,
                "净荷被截断: 声明 $payloadLength, 实际 $bodyLength",
            )
        }
        if (bodyLength > payloadLength) {
            throw FrameException(
                FRAME_LENGTH_MISMATCH,
                "长度字段与实际不符: 声明 $payloadLength, 实际 $bodyLength",
            )
        }
        val payload = packet.copyOfRange(HEADER_SIZE, packet.size)
        if (flags and FLAG_CRC32 != 0 && crc32(payload) != checksum) {
            throw FrameException(FRAME_CRC_MISMATCH, "净荷 CRC32 与帧头不一致")
        }
        return DecodedFrame(
            seq = seq,
            pts = pts,
            width = width,
            height = height,
            keyframe = flags and FLAG_KEYFRAME != 0,
            payload = payload,
        )
    }

    /** 解码 v1 JSON 视频帧; 只做兼容, 不再用 v1 编码新帧. */
    fun decodeV1(message: Map<String, Any?>): DecodedFrame {
        if (message["kind"]?.toString() != "video") {
            throw FrameException(FRAME_KIND_MISMATCH, "不是视频帧消息: ${message["kind"]}")
        }
        val text = message["data"] as? String
            ?: throw FrameException(FRAME_BAD_PAYLOAD, "v1 视频帧缺少字符串 data")
        val payload = try {
            Base64.getDecoder().decode(text)
        } catch (error: IllegalArgumentException) {
            throw FrameException(FRAME_BAD_PAYLOAD, "v1 视频帧 base64 损坏: ${error.message}")
        }
        return DecodedFrame(
            seq = intField(message, "seq"),
            pts = intField(message, "pts"),
            width = intField(message, "w").toInt(),
            height = intField(message, "h").toInt(),
            keyframe = message["key"] == true,
            payload = payload,
        )
    }

    /** 按对端 media_caps 协商帧版本; 未声明或只支持 v1 时降级 v1. */
    fun negotiateVersion(peerCaps: Map<*, *>?): Int {
        if (peerCaps.isNullOrEmpty()) {
            return VERSION_V1
        }
        val declared = peerCaps["frame_versions"] ?: peerCaps["video_frame_versions"]
        val versions: Set<Int> = when (declared) {
            is Iterable<*> -> declared.mapNotNull { (it as? Number)?.toInt() }.toSet()
            is Array<*> -> declared.mapNotNull { (it as? Number)?.toInt() }.toSet()
            else -> (peerCaps["frame_version"] as? Number)?.let { setOf(it.toInt()) } ?: emptySet()
        }
        if (versions.isEmpty()) {
            return VERSION_V1
        }
        for (version in LOCAL_VERSIONS) {
            if (version in versions) {
                return version
            }
        }
        return VERSION_V1
    }

    /** 供测试与发送侧构造 v2 帧; 生产发送端在被控端 (Python). */
    fun encodeV2(frame: DecodedFrame, withCrc: Boolean = false): ByteArray {
        val payload = frame.payload
        require(frame.seq in 0..0xFFFFFFFFL) { "seq 越界: ${frame.seq}" }
        require(frame.width in 0..0xFFFF) { "尺寸越界: ${frame.width}x${frame.height}" }
        require(frame.height in 0..0xFFFF) { "尺寸越界: ${frame.width}x${frame.height}" }
        require(payload.size <= MAX_PAYLOAD_BYTES) { "净荷 ${payload.size} 字节超过上限 $MAX_PAYLOAD_BYTES" }
        var flags = if (frame.keyframe) FLAG_KEYFRAME else 0
        var checksum = 0L
        if (withCrc) {
            flags = flags or FLAG_CRC32
            checksum = crc32(payload)
        }
        val header = ByteArray(HEADER_SIZE)
        header[0] = MAGIC_FIRST.toByte()
        header[1] = MAGIC_SECOND.toByte()
        header[2] = VERSION_V2.toByte()
        header[3] = flags.toByte()
        writeUInt32(header, 4, frame.seq)
        writeInt64(header, 8, frame.pts)
        writeUInt16(header, 16, frame.width)
        writeUInt16(header, 18, frame.height)
        writeUInt32(header, 20, payload.size.toLong())
        writeUInt32(header, 24, checksum)
        return header + payload
    }

    private fun intField(message: Map<String, Any?>, name: String): Long {
        val value = message[name] ?: 0L
        if (value is Boolean || value !is Number) {
            throw FrameException(FRAME_BAD_PAYLOAD, "v1 视频帧字段 $name 不是整数: $value")
        }
        return value.toLong()
    }

    private fun readUInt16(data: ByteArray, offset: Int): Int =
        ((data[offset].toInt() and 0xff) shl 8) or (data[offset + 1].toInt() and 0xff)

    private fun readUInt32(data: ByteArray, offset: Int): Long {
        var value = 0L
        for (index in offset until offset + 4) {
            value = (value shl 8) or (data[index].toLong() and 0xff)
        }
        return value
    }

    private fun readInt64(data: ByteArray, offset: Int): Long {
        var value = 0L
        for (index in offset until offset + 8) {
            value = (value shl 8) or (data[index].toLong() and 0xff)
        }
        return value
    }

    private fun writeUInt16(data: ByteArray, offset: Int, value: Int) {
        data[offset] = (value ushr 8).toByte()
        data[offset + 1] = value.toByte()
    }

    private fun writeUInt32(data: ByteArray, offset: Int, value: Long) {
        for (index in 0 until 4) {
            data[offset + index] = (value ushr ((3 - index) * 8)).toByte()
        }
    }

    private fun writeInt64(data: ByteArray, offset: Int, value: Long) {
        for (index in 0 until 8) {
            data[offset + index] = (value ushr ((7 - index) * 8)).toByte()
        }
    }

    private fun crc32(payload: ByteArray): Long = CRC32().apply { update(payload) }.value
}
