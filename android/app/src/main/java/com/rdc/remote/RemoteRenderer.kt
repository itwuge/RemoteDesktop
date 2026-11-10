// ==========================================================================================
// 文件        : RemoteRenderer.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 远端画面解码与 Surface 渲染适配: 横竖屏切换时 Surface 迁移而解码不中断
// 架构定位    : Android 控制端媒体适配层, 不承载会话业务逻辑
// 核心功能    : 持有 MediaCodec 解码器与输出 Surface; 旋转后 setOutputSurface 热切换
// 职责边界    : 不建连、不协商、不做降级决策; 不采集屏幕
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34), 硬解优先, 回落软解
// 依赖关系    : 由 SessionActivity 创建; 码流入口暂未接入
// ==========================================================================================

package com.rdc.remote

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.view.Surface

/**
 * 渲染器.
 *
 * 现状: 只有 Surface 管理与旋转迁移路径, 未接入码流 (WORK_FEEDING = false),
 * 因此解码器按需创建、当前保持 idle; 旋转时执行 Surface 迁移与状态保留日志,
 * 用来说明「重建 Surface 不清会话、不清状态」这条路径成立, 不作为解码实测结论.
 */
internal class RemoteRenderer {

    private var codec: MediaCodec? = null
    private var outputSurface: Surface? = null
    private var rotationDegrees = 0
    private var videoWidth = 0
    private var videoHeight = 0

    /** 码流入口是否已接入; PoC 阶段恒为 false. */
    val feeding: Boolean = false

    /** 解码器是否在跑. */
    val running: Boolean
        get() = codec != null

    /** 当前旋转角度, 0/90/180/270. */
    val rotation: Int
        get() = rotationDegrees

    /**
     * 挂载新的输出 Surface.
     *
     * 解码器已运行时走 setOutputSurface 热切换, 不 stop/configure, 解码不出中断;
     * 解码器未运行时只记录待用 Surface.
     */
    fun attachSurface(surface: Surface, width: Int, height: Int) {
        outputSurface = surface
        videoWidth = width
        videoHeight = height
        val active = codec
        if (active != null) {
            try {
                active.setOutputSurface(surface)
                RdcLog.info(TAG, "surface_hot_switch", "size=${width}x$height rotation=$rotationDegrees")
            } catch (error: IllegalStateException) {
                RdcLog.warn(TAG, "surface_hot_switch_failed", "reason=${error.message}")
            }
        } else {
            RdcLog.info(TAG, "surface_attached", "size=${width}x$height rotation=$rotationDegrees codec=idle")
        }
    }

    /** Surface 销毁 (旋转重建、界面退到后台) 时调用; 解码器保留, 等新 Surface. */
    fun detachSurface() {
        outputSurface = null
        RdcLog.info(TAG, "surface_detached", "codec=${if (codec != null) "running" else "idle"}")
    }

    /**
     * 更新方向. 只改方向记录与画面尺寸预期, 不重建解码器.
     *
     * @return 方向是否发生变化
     */
    fun updateOrientation(degrees: Int, width: Int, height: Int): Boolean {
        val changed = degrees != rotationDegrees || width != videoWidth || height != videoHeight
        rotationDegrees = degrees
        videoWidth = width
        videoHeight = height
        if (changed) {
            RdcLog.info(
                TAG,
                "orientation_updated",
                "rotation=$degrees size=${width}x$height codec=${if (codec != null) "running" else "idle"}",
            )
        }
        return changed
    }

    /**
     * 按需创建 AVC 解码器; 码流未接入时不调用, 避免空转占用硬解资源.
     */
    @Suppress("unused")
    fun ensureCodec(width: Int, height: Int): Boolean {
        if (codec != null) {
            return true
        }
        val surface = outputSurface ?: return false
        return try {
            val format = MediaFormat.createVideoFormat(MIME_AVC, width, height)
            format.setInteger(
                MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface,
            )
            codec = MediaCodec.createDecoderByType(MIME_AVC).apply {
                configure(format, surface, null, 0)
                start()
            }
            RdcLog.info(TAG, "codec_started", "size=${width}x$height")
            true
        } catch (error: MediaCodec.CodecException) {
            RdcLog.error(TAG, "codec_create_failed", "kind=codec reason=${error.message}")
            codec = null
            false
        } catch (error: IllegalArgumentException) {
            RdcLog.error(TAG, "codec_create_failed", "kind=argument reason=${error.message}")
            codec = null
            false
        } catch (error: IllegalStateException) {
            RdcLog.error(TAG, "codec_create_failed", "kind=state reason=${error.message}")
            codec = null
            false
        }
    }

    /** 释放解码器; 界面销毁时调用. */
    fun release() {
        codec?.let {
            try {
                it.stop()
            } catch (error: IllegalStateException) {
                RdcLog.warn(TAG, "codec_stop_failed", "reason=${error.message}")
            }
            it.release()
        }
        codec = null
        outputSurface = null
        RdcLog.info(TAG, "released")
    }

    companion object {
        private const val TAG = "Render"
        private const val MIME_AVC = MediaFormat.MIMETYPE_VIDEO_AVC
    }
}
