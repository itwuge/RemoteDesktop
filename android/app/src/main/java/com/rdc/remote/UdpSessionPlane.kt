// ==========================================================================================
// 文件        : UdpSessionPlane.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 打洞后的 UDP 数据面: 16 字节头、1200 字节报、1184 字节分片、ACK 重传、心跳
// 架构定位    : 传输层门面, 对上暴露 InputStream/OutputStream, 对下收发 UDP 数据报
// 核心功能    : 分片/重组; 四字节长度前缀的拆装; 可靠帧 ACK 120/240/480 ms ×3; 1 s PING / 3 s 判死
// 职责边界    : 不做 AEAD、不做业务语义; 载荷就是"现有 AEAD 帧去掉 4 字节长度前缀"的字节
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34); 单测跑 JVM
// 依赖关系    : RdcLog、org.json 无关
// ==========================================================================================

package com.rdc.remote

import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.net.SocketTimeoutException
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * 一条 UDP 数据面.
 *
 * 复用打洞阶段的同一个 DatagramSocket, 端口不变, 保证对端 NAT 映射继续有效.
 * 上层用 [input]/[output] 包住现有 readLoop/writeFrame, 协议字段一层不动.
 */
internal class UdpSessionPlane(
    private val socket: DatagramSocket,
    private val peer: InetSocketAddress,
    private val onPathDead: () -> Unit = {},
) {

    val input: InputStream = PlaneInput()
    val output: OutputStream = PlaneOutput()

    private val closed = AtomicBoolean(false)
    private val frameSeq = AtomicInteger(0)
    private val ready = LinkedBlockingQueue<ByteArray>()
    private val reassembly = LinkedHashMap<Int, Reassembly>()
    private val reassemblyLock = Any()
    private val pendingLock = Any()
    private val pendingAck = HashMap<Int, PendingAck>()
    private val sendLock = Any()
    private val outputLock = Any()

    private var lastRecvNanos = System.nanoTime()
    private var lastPingNanos = 0L
    private var deadNotified = false

    private val receiveThread = Thread({ receiveLoop() }, "udp-plane-recv").apply {
        isDaemon = true
        start()
    }

    fun close() {
        if (closed.compareAndSet(false, true)) {
            runCatching { socket.close() }
            ready.offer(ByteArray(0))
        }
    }

    // ------------------------------------------------------------------ 发送侧

    private inner class PlaneOutput : OutputStream() {
        private val header = ByteArray(4)
        private var headerLen = 0
        private var bodyLength = -1
        private var body = ByteArray(0)
        private var bodyLen = 0

        override fun write(b: Int) {
            write(byteArrayOf(b.toByte()), 0, 1)
        }

        override fun write(b: ByteArray, off: Int, len: Int) {
            if (closed.get()) {
                throw IOException("udp plane closed")
            }
            synchronized(outputLock) {
                var index = off
                val end = off + len
                while (index < end) {
                    if (bodyLength < 0) {
                        header[headerLen] = b[index]
                        headerLen += 1
                        index += 1
                        if (headerLen == 4) {
                            val length = readInt(header, 0)
                            if (length <= 0 || length > MAX_FRAME_BYTES) {
                                throw IOException("非法帧长度: $length")
                            }
                            bodyLength = length
                            body = ByteArray(length)
                            bodyLen = 0
                        }
                    } else {
                        val take = minOf(bodyLength - bodyLen, end - index)
                        System.arraycopy(b, index, body, bodyLen, take)
                        bodyLen += take
                        index += take
                        if (bodyLen == bodyLength) {
                            val frame = body
                            bodyLength = -1
                            headerLen = 0
                            body = ByteArray(0)
                            bodyLen = 0
                            sendFrame(frame, reliable = true)
                        }
                    }
                }
            }
        }

        override fun flush() {
            // 无缓冲写, 不需要 flush
        }
    }

    private fun sendFrame(body: ByteArray, reliable: Boolean) {
        if (body.isEmpty() || body.size > MAX_FRAME_BYTES) {
            throw IOException("帧长度越界: ${body.size}")
        }
        val fragmentCount = (body.size + FRAGMENT_BYTES - 1) / FRAGMENT_BYTES
        if (fragmentCount <= 0 || fragmentCount > MAX_FRAGMENT_COUNT) {
            throw IOException("分片数越界: $fragmentCount")
        }
        val frameId = frameSeq.incrementAndGet()
        val datagrams = ArrayList<ByteArray>(fragmentCount)
        for (index in 0 until fragmentCount) {
            val offset = index * FRAGMENT_BYTES
            val length = minOf(FRAGMENT_BYTES, body.size - offset)
            var flags = 0
            if (index == fragmentCount - 1) {
                flags = flags or FLAG_LAST
            }
            if (reliable) {
                flags = flags or FLAG_RELIABLE
            }
            datagrams.add(
                buildDatagram(
                    type = TYPE_DATA,
                    flags = flags,
                    fragIndex = index,
                    fragCount = fragmentCount,
                    fragLen = length,
                    frameId = frameId,
                    frameLen = body.size,
                    payload = body,
                    payloadOffset = offset,
                    payloadLength = length,
                ),
            )
        }
        if (reliable) {
            synchronized(pendingLock) {
                pendingAck[frameId] = PendingAck(
                    datagrams = datagrams,
                    attempts = 0,
                    nextDueNanos = System.nanoTime() + RETRANSMIT_MS[0] * NANOS_PER_MS,
                )
            }
        }
        for (datagram in datagrams) {
            sendDatagram(datagram)
        }
    }

    private fun sendDatagram(datagram: ByteArray) {
        try {
            synchronized(sendLock) {
                socket.send(DatagramPacket(datagram, datagram.size, peer))
            }
        } catch (error: IOException) {
            if (!closed.get()) {
                RdcLog.warn(TAG, "udp_send_failed", "reason=${error.javaClass.simpleName}")
            }
        }
    }

    private fun sendControl(type: Int, frameId: Int = 0) {
        val datagram = buildDatagram(
            type = type,
            flags = 0,
            fragIndex = 0,
            fragCount = 1,
            fragLen = 0,
            frameId = frameId,
            frameLen = 0,
            payload = null,
            payloadOffset = 0,
            payloadLength = 0,
        )
        sendDatagram(datagram)
    }

    private fun buildDatagram(
        type: Int,
        flags: Int,
        fragIndex: Int,
        fragCount: Int,
        fragLen: Int,
        frameId: Int,
        frameLen: Int,
        payload: ByteArray?,
        payloadOffset: Int,
        payloadLength: Int,
    ): ByteArray {
        val datagram = ByteArray(HEADER_BYTES + payloadLength)
        datagram[0] = ((VERSION shl 4) or type).toByte()
        datagram[1] = flags.toByte()
        writeShort(datagram, 2, fragIndex)
        writeShort(datagram, 4, fragCount)
        writeShort(datagram, 6, fragLen)
        writeInt(datagram, 8, frameId)
        writeInt(datagram, 12, frameLen)
        if (payload != null && payloadLength > 0) {
            System.arraycopy(payload, payloadOffset, datagram, HEADER_BYTES, payloadLength)
        }
        return datagram
    }

    // ------------------------------------------------------------------ 接收侧

    private fun receiveLoop() {
        val buffer = ByteArray(DATAGRAM_BYTES)
        runCatching { socket.soTimeout = TICK_MS }
        while (!closed.get()) {
            try {
                val packet = DatagramPacket(buffer, buffer.size)
                socket.receive(packet)
                lastRecvNanos = System.nanoTime()
                val data = packet.data.copyOfRange(packet.offset, packet.offset + packet.length)
                handleDatagram(data)
            } catch (_: SocketTimeoutException) {
                // 正常节拍, 用于心跳与重传扫描
            } catch (_: IOException) {
                if (!closed.get()) {
                    continue
                }
            }
            tick()
        }
    }

    private fun handleDatagram(data: ByteArray) {
        if (data.size < HEADER_BYTES) {
            return
        }
        val version = (data[0].toInt() ushr 4) and 0x0f
        val type = data[0].toInt() and 0x0f
        if (version != VERSION) {
            return
        }
        val flags = data[1].toInt() and 0xff
        val fragIndex = readShort(data, 2)
        val fragCount = readShort(data, 4)
        val fragLen = readShort(data, 6)
        val frameId = readInt(data, 8)
        val frameLen = readInt(data, 12)
        if (fragLen != data.size - HEADER_BYTES || fragLen > FRAGMENT_BYTES) {
            return
        }
        when (type) {
            TYPE_PING -> sendControl(TYPE_PONG, frameId)
            TYPE_PONG -> Unit
            TYPE_ACK -> {
                synchronized(pendingLock) { pendingAck.remove(frameId) }
            }
            TYPE_DATA -> {
                if (fragCount <= 0 || fragCount > MAX_FRAGMENT_COUNT) {
                    return
                }
                if (frameLen <= 0 || frameLen > MAX_FRAME_BYTES) {
                    return
                }
                if (fragIndex !in 0 until fragCount) {
                    return
                }
                if (flags and FLAG_RELIABLE != 0) {
                    sendControl(TYPE_ACK, frameId)
                }
                reassemble(data, fragIndex, fragCount, frameLen, frameId)
            }
            else -> Unit
        }
    }

    private fun reassemble(
        data: ByteArray,
        fragIndex: Int,
        fragCount: Int,
        frameLen: Int,
        frameId: Int,
    ) {
        var complete: ByteArray? = null
        synchronized(reassemblyLock) {
            var entry = reassembly[frameId]
            if (entry == null) {
                if (reassembly.size >= MAX_CONCURRENT_FRAMES) {
                    val oldest = reassembly.keys.firstOrNull()
                    if (oldest != null) {
                        reassembly.remove(oldest)
                    }
                }
                entry = Reassembly(
                    fragCount = fragCount,
                    frameLen = frameLen,
                    parts = arrayOfNulls(fragCount),
                    lastAtNanos = System.nanoTime(),
                )
                reassembly[frameId] = entry
            }
            if (entry.fragCount != fragCount || entry.frameLen != frameLen) {
                reassembly.remove(frameId)
                return
            }
            if (entry.parts[fragIndex] == null) {
                entry.parts[fragIndex] = data.copyOfRange(HEADER_BYTES, data.size)
                entry.received += 1
            }
            entry.lastAtNanos = System.nanoTime()
            if (entry.received == entry.fragCount) {
                val assembled = ByteArray(FRAME_PREFIX_BYTES + frameLen)
                writeInt(assembled, 0, frameLen)
                var offset = FRAME_PREFIX_BYTES
                for (part in entry.parts) {
                    if (part == null) {
                        reassembly.remove(frameId)
                        return
                    }
                    System.arraycopy(part, 0, assembled, offset, part.size)
                    offset += part.size
                }
                reassembly.remove(frameId)
                complete = assembled
            }
        }
        if (complete != null) {
            ready.offer(complete)
        }
    }

    private inner class PlaneInput : InputStream() {
        private var current: ByteArray? = null
        private var position = 0

        override fun read(): Int {
            val single = ByteArray(1)
            val count = read(single, 0, 1)
            return if (count <= 0) -1 else single[0].toInt() and 0xff
        }

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (len == 0) {
                return 0
            }
            var frame = current
            if (frame == null) {
                while (frame == null) {
                    if (closed.get() && ready.isEmpty()) {
                        return -1
                    }
                    frame = ready.poll(TICK_MS.toLong(), TimeUnit.MILLISECONDS)
                }
                current = frame
                position = 0
            }
            val count = minOf(len, frame.size - position)
            System.arraycopy(frame, position, b, off, count)
            position += count
            if (position >= frame.size) {
                current = null
                position = 0
            }
            return count
        }
    }

    // ------------------------------------------------------------------ 节拍

    private fun tick() {
        val now = System.nanoTime()
        if (now - lastPingNanos >= HEARTBEAT_MS * NANOS_PER_MS) {
            lastPingNanos = now
            sendControl(TYPE_PING)
        }
        if (!deadNotified && now - lastRecvNanos >= PATH_DEAD_MS * NANOS_PER_MS) {
            deadNotified = true
            RdcLog.warn(TAG, "udp_path_dead", "idle_ms=${PATH_DEAD_MS}")
            runCatching { onPathDead() }
        }
        synchronized(reassemblyLock) {
            val expired = reassembly.filterValues {
                now - it.lastAtNanos >= REASSEMBLY_TIMEOUT_MS * NANOS_PER_MS
            }.keys
            for (key in expired) {
                reassembly.remove(key)
            }
        }
        synchronized(pendingLock) {
            val exhausted = ArrayList<Int>()
            for ((frameId, pending) in pendingAck) {
                if (now < pending.nextDueNanos) {
                    continue
                }
                if (pending.attempts >= RETRANSMIT_MS.size) {
                    exhausted.add(frameId)
                    continue
                }
                for (datagram in pending.datagrams) {
                    sendDatagram(datagram)
                }
                pending.attempts += 1
                pending.nextDueNanos = now + RETRANSMIT_MS[pending.attempts] * NANOS_PER_MS
            }
            for (frameId in exhausted) {
                pendingAck.remove(frameId)
                RdcLog.warn(TAG, "udp_ack_exhausted", "frame_id=$frameId")
            }
        }
    }

    private class Reassembly(
        val fragCount: Int,
        val frameLen: Int,
        val parts: Array<ByteArray?>,
        var lastAtNanos: Long,
        var received: Int = 0,
    )

    private class PendingAck(
        val datagrams: List<ByteArray>,
        var attempts: Int,
        var nextDueNanos: Long,
    )

    internal companion object {
        private const val TAG = "RDC_CONNECT"
        const val VERSION = 1
        const val TYPE_DATA = 1
        const val TYPE_PING = 2
        const val TYPE_PONG = 3
        const val TYPE_ACK = 4

        const val FRAME_PREFIX_BYTES = 4
        const val HEADER_BYTES = 16
        const val DATAGRAM_BYTES = 1200
        const val FRAGMENT_BYTES = DATAGRAM_BYTES - HEADER_BYTES
        const val MAX_FRAME_BYTES = 6 * 1024 * 1024
        const val MAX_FRAGMENT_COUNT = 5314
        const val MAX_CONCURRENT_FRAMES = 8

        const val FLAG_LAST = 0x01
        const val FLAG_RELIABLE = 0x02
        const val FLAG_CONTROL = 0x04

        const val HEARTBEAT_MS = 1000
        const val PATH_DEAD_MS = 3000
        const val REASSEMBLY_TIMEOUT_MS = 100
        const val TICK_MS = 50

        private val RETRANSMIT_MS = intArrayOf(120, 240, 480)
        private const val NANOS_PER_MS = 1_000_000L

        private fun writeShort(target: ByteArray, offset: Int, value: Int) {
            target[offset] = ((value ushr 8) and 0xff).toByte()
            target[offset + 1] = (value and 0xff).toByte()
        }

        private fun writeInt(target: ByteArray, offset: Int, value: Int) {
            target[offset] = ((value ushr 24) and 0xff).toByte()
            target[offset + 1] = ((value ushr 16) and 0xff).toByte()
            target[offset + 2] = ((value ushr 8) and 0xff).toByte()
            target[offset + 3] = (value and 0xff).toByte()
        }

        private fun readShort(source: ByteArray, offset: Int): Int =
            ((source[offset].toInt() and 0xff) shl 8) or (source[offset + 1].toInt() and 0xff)

        private fun readInt(source: ByteArray, offset: Int): Int =
            ((source[offset].toInt() and 0xff) shl 24) or
                ((source[offset + 1].toInt() and 0xff) shl 16) or
                ((source[offset + 2].toInt() and 0xff) shl 8) or
                (source[offset + 3].toInt() and 0xff)
    }
}
