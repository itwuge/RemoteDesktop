// ==========================================================================================
// 文件        : RemoteRenderer.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 远端 H.264 硬解与 Surface 渲染; 横竖屏切换时 Surface 迁移而解码不中断
// 架构定位    : Android 控制端媒体适配层, 不承载会话业务逻辑
// 核心功能    : MediaCodec 硬解 Annex B 码流; SurfaceView 输出; setOutputSurface 热切换
// 职责边界    : 不建连、不协商、不做降级决策; 不采集屏幕
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35), 硬解优先
// 依赖关系    : 由 SessionActivity 创建; 码流来自 LanSession
// ==========================================================================================

package com.rdc.remote

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.view.Surface
import java.nio.ByteBuffer
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * 渲染器.
 *
 * 解码线程从队列取帧, 首帧关键帧里抽 SPS/PPS 作为 csd-0/csd-1;
 * Surface 变化时优先 setOutputSurface 热切换, 失败才重建解码器.
 */
internal class RemoteRenderer {

    private data class Frame(
        val width: Int,
        val height: Int,
        val fps: Int,
        val key: Boolean,
        val pts: Long,
        val data: ByteArray,
    )

    private val lock = Any()
    private val queue = LinkedBlockingQueue<Frame>(8)
    private val bufferInfo = MediaCodec.BufferInfo()

    private var codec: MediaCodec? = null
    private var outputSurface: Surface? = null
    private var surfaceDirty = false
    private var rotationDegrees = 0
    private var videoWidth = 0
    private var videoHeight = 0
    private var csd0: ByteArray? = null
    private var csd1: ByteArray? = null
    private var decoderThread: Thread? = null

    @Volatile
    private var released = false

    /** 码流入口已接入且解码线程在跑. */
    @Volatile
    var feeding: Boolean = false
        private set

    /** 解码器是否在跑. */
    val running: Boolean
        get() = codec != null

    /** 画面缺失参数集/解码失败后, 正在等待下一个关键帧; 界面据此提示"画面恢复中". */
    @Volatile
    var recovering: Boolean = false
        private set

    /** 当前旋转角度, 0/90/180/270. */
    val rotation: Int
        get() = rotationDegrees

    /** 挂载输出 Surface; 已配置解码器时热切换, 否则等下一帧按需创建. */
    fun attachSurface(surface: Surface, width: Int, height: Int) {
        synchronized(lock) {
            outputSurface = surface
            videoWidth = width
            videoHeight = height
            if (codec != null) {
                surfaceDirty = true
            }
        }
        ensureDecoderThread()
        RdcLog.info(TAG, "surface_attached", "size=${width}x$height rotation=$rotationDegrees")
    }

    /** Surface 销毁时只记为空面, 解码器状态保留, 输出帧丢弃等新面. */
    fun detachSurface() {
        synchronized(lock) {
            outputSurface = null
            surfaceDirty = false
        }
        RdcLog.info(TAG, "surface_detached", "codec=${if (codec != null) "running" else "idle"}")
    }

    /** 更新方向记录, 不重建解码器. */
    fun updateOrientation(degrees: Int, width: Int, height: Int): Boolean {
        val changed = degrees != rotationDegrees || width != videoWidth || height != videoHeight
        rotationDegrees = degrees
        videoWidth = width
        videoHeight = height
        if (changed) {
            RdcLog.info(TAG, "orientation_updated", "rotation=$degrees size=${width}x$height")
        }
        return changed
    }

    /** 送入一帧 H.264 码流; 非阻塞, 队列满时丢弃最旧帧. */
    fun feed(frame: LanSession.VideoFrame) {
        if (released) {
            return
        }
        if (frame.key) {
            synchronized(lock) {
                if (csd0 == null) {
                    extractCsd(frame.data)
                }
            }
        }
        val item = Frame(frame.width, frame.height, frame.fps, frame.key, frame.pts, frame.data)
        while (!queue.offer(item)) {
            queue.poll()
        }
        ensureDecoderThread()
    }

    private fun ensureDecoderThread() {
        if (decoderThread != null) {
            return
        }
        released = false
        feeding = true
        decoderThread = Thread({ decodeLoop() }, "render-decode").apply { start() }
    }

    private fun decodeLoop() {
        while (!released) {
            val frame = try {
                queue.poll(300, TimeUnit.MILLISECONDS)
            } catch (_: InterruptedException) {
                null
            } ?: continue
            var pending = false
            synchronized(lock) {
                applySurfaceLocked()
                if (codec == null && outputSurface != null && csd0 != null) {
                    configureLocked(frame.width, frame.height)
                }
                pending = codec == null
            }
            if (pending) {
                // 还缺 Surface 或参数集时把关键帧放回队首, 避免整段黑屏
                if (frame.key) {
                    queue.offer(frame)
                } else {
                    // 非关键帧无参数集可解, 明确进入"等待关键帧"状态
                    recovering = true
                }
                try {
                    Thread.sleep(20)
                } catch (_: InterruptedException) {
                }
                continue
            }
            feedAndDrain(frame)
        }
    }

    private fun applySurfaceLocked() {
        val active = codec ?: return
        if (!surfaceDirty) {
            return
        }
        val surface = outputSurface
        surfaceDirty = false
        if (surface == null) {
            return
        }
        try {
            active.setOutputSurface(surface)
            RdcLog.info(TAG, "surface_hot_switch", "size=${videoWidth}x$videoHeight rotation=$rotationDegrees")
        } catch (error: Exception) {
            RdcLog.warn(TAG, "surface_hot_switch_failed", "reason=${error.message}")
            releaseCodecLocked()
        }
    }

    private fun configureLocked(width: Int, height: Int) {
        val surface = outputSurface ?: return
        try {
            val format = MediaFormat.createVideoFormat(MIME_AVC, width, height)
            format.setInteger(
                MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface,
            )
            csd0?.let { format.setByteBuffer("csd-0", ByteBuffer.wrap(it)) }
            csd1?.let { format.setByteBuffer("csd-1", ByteBuffer.wrap(it)) }
            // 桌面编码器单帧最大约 200 KB; 留足空间避免输入缓冲不够
            format.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 4 * 1024 * 1024)
            val decoder = MediaCodec.createDecoderByType(MIME_AVC)
            decoder.configure(format, surface, null, 0)
            decoder.start()
            codec = decoder
            RdcLog.info(TAG, "codec_started", "size=${width}x$height hard=true")
        } catch (error: Exception) {
            RdcLog.error(TAG, "codec_create_failed", "reason=${error.message}")
            codec = null
        }
    }

    private fun feedAndDrain(frame: Frame) {
        val active = codec ?: return
        try {
            val index = active.dequeueInputBuffer(10_000)
            if (index >= 0) {
                val buffer = active.getInputBuffer(index)
                if (buffer != null) {
                    buffer.clear()
                    if (frame.data.size > buffer.capacity()) {
                        RdcLog.warn(
                            TAG,
                            "input_buffer_small",
                            "need=${frame.data.size} capacity=${buffer.capacity()}",
                        )
                        active.queueInputBuffer(index, 0, 0, 0, 0)
                        return
                    }
                    buffer.put(frame.data)
                    val flags = if (frame.key) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0
                    val ptsUs = frame.pts * 1_000_000L / frame.fps.coerceAtLeast(1)
                    active.queueInputBuffer(index, 0, frame.data.size, ptsUs, flags)
                }
            }
            drain(active)
        } catch (error: Exception) {
            RdcLog.warn(TAG, "decode_failed", "reason=${error.message}")
            recovering = true
            synchronized(lock) { releaseCodecLocked() }
        }
    }

    private fun drain(active: MediaCodec) {
        while (true) {
            val index = active.dequeueOutputBuffer(bufferInfo, 0)
            if (index == MediaCodec.INFO_TRY_AGAIN_LATER) {
                return
            }
            if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                val format = active.outputFormat
                RdcLog.info(
                    TAG,
                    "output_format",
                    "size=${format.getInteger(MediaFormat.KEY_WIDTH)}x${format.getInteger(MediaFormat.KEY_HEIGHT)}",
                )
                continue
            }
            if (index >= 0) {
                val render = outputSurface != null
                active.releaseOutputBuffer(index, render)
                if (render) {
                    recovering = false
                }
            }
        }
    }

    /** 从 Annex B 关键帧中抽 SPS(7) 与 PPS(8) 作为 csd; 提取规则见 H264AnnexB. */
    private fun extractCsd(data: ByteArray) {
        val params = H264AnnexB.extractParamSets(data)
        if (csd0 == null) {
            csd0 = params.sps
        }
        if (csd1 == null) {
            csd1 = params.pps
        }
        if (csd0 == null || csd1 == null) {
            RdcLog.warn(TAG, "param_sets_missing", "sps=${csd0 != null} pps=${csd1 != null}")
        }
    }

    private fun releaseCodecLocked() {
        codec?.let {
            try {
                it.stop()
            } catch (_: IllegalStateException) {
            }
            it.release()
        }
        codec = null
    }

    /** 释放解码器与线程; 界面销毁时调用. */
    fun release() {
        released = true
        decoderThread?.interrupt()
        decoderThread = null
        queue.clear()
        synchronized(lock) {
            releaseCodecLocked()
            outputSurface = null
        }
        feeding = false
        RdcLog.info(TAG, "released")
    }

    companion object {
        private const val TAG = "Render"
        private const val MIME_AVC = MediaFormat.MIMETYPE_VIDEO_AVC
    }
}
