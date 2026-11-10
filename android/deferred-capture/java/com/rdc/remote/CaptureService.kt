// ==========================================================================================
// 文件        : CaptureService.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 前台捕获服务: 持有 MediaProjection, 建立 VirtualDisplay 取帧并转交契约入口
// 架构定位    : Android 采集适配层, 不是业务层
// 核心功能    : 通知渠道与 startForeground(mediaProjection); ImageReader 取帧; 每帧只交元数据给接缝
// 职责边界    : 不编码、不打包、不加密、不发送; 帧数据在 onImageAvailable 内立即释放
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34)
// 依赖关系    : CaptureContract, MainActivity
// ==========================================================================================

package com.rdc.remote

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import android.view.Display
import android.view.Surface
import androidx.core.app.ServiceCompat

class CaptureService : Service() {

    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null
    private val mainHandler = Handler(Looper.getMainLooper())
    private var captureWidth = 0
    private var captureHeight = 0

    private val projectionCallback = object : MediaProjection.Callback() {
        override fun onStop() {
            // 用户撤权或系统回收: 立即收尾, 不留后台捕获
            releaseCapture()
            stopSelf()
        }
    }

    // 横竖屏切换时按新尺寸重建 VirtualDisplay 与 ImageReader, 采集不中断
    private val displayListener = object : DisplayManager.DisplayListener {
        override fun onDisplayAdded(displayId: Int) = Unit

        override fun onDisplayRemoved(displayId: Int) = Unit

        override fun onDisplayChanged(displayId: Int) {
            if (displayId == Display.DEFAULT_DISPLAY) {
                mainHandler.post { rebuildIfSizeChanged() }
            }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> startCapture(intent)
            ACTION_STOP -> {
                releaseCapture()
                stopSelf()
            }
            else -> Log.w(TAG, "收到未知动作, 忽略")
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        releaseCapture()
        super.onDestroy()
    }

    private fun startCapture(intent: Intent) {
        if (projection != null) {
            Log.i(TAG, "捕获已在进行, 忽略重复启动")
            return
        }
        val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, 0)
        val resultData = intent.getParcelableExtra<Intent>(EXTRA_RESULT_DATA)
        if (resultData == null) {
            Log.e(TAG, "缺少 MediaProjection 授权结果, 不启动捕获")
            stopSelf()
            return
        }

        // Android 14 要求先以 mediaProjection 类型进入前台, 再取 MediaProjection
        startForegroundNotification()

        val manager = getSystemService(MediaProjectionManager::class.java)
        val mediaProjection = manager.getMediaProjection(resultCode, resultData)
        if (mediaProjection == null) {
            Log.e(TAG, "授权令牌已失效, 未取得 MediaProjection")
            stopSelf()
            return
        }
        mediaProjection.registerCallback(projectionCallback, mainHandler)
        projection = mediaProjection

        val metrics = resources.displayMetrics
        captureWidth = metrics.widthPixels
        captureHeight = metrics.heightPixels
        val densityDpi = metrics.densityDpi

        startReaderAndDisplay(captureWidth, captureHeight, densityDpi)
        getSystemService(DisplayManager::class.java).registerDisplayListener(displayListener, mainHandler)
        CaptureContract.submitDisplayState(captureWidth, captureHeight, currentRotationDegrees())
        Log.i(TAG, "捕获已启动: ${captureWidth}x${captureHeight} @ ${densityDpi}dpi")
    }

    private fun startReaderAndDisplay(width: Int, height: Int, densityDpi: Int) {
        val mediaProjection = projection ?: return
        val reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, MAX_IMAGES)
        reader.setOnImageAvailableListener({ source ->
            source.acquireLatestImage()?.use { image ->
                // 采集到此为止: 只把帧元数据交给契约入口, 不编码、不发送
                CaptureContract.submitFrame(image.width, image.height, image.timestamp)
            }
        }, mainHandler)
        imageReader = reader

        virtualDisplay = mediaProjection.createVirtualDisplay(
            DISPLAY_NAME,
            width,
            height,
            densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader.surface,
            null,
            mainHandler,
        )
    }

    private fun rebuildIfSizeChanged() {
        if (projection == null) {
            return
        }
        val metrics = resources.displayMetrics
        if (metrics.widthPixels == captureWidth && metrics.heightPixels == captureHeight) {
            return
        }
        // 先换新 Surface 再释放旧的, 保证镜像画面有落点, 中转不中断
        val previousDisplay = virtualDisplay
        val previousReader = imageReader
        captureWidth = metrics.widthPixels
        captureHeight = metrics.heightPixels
        startReaderAndDisplay(captureWidth, captureHeight, metrics.densityDpi)
        previousDisplay?.release()
        previousReader?.close()
        CaptureContract.submitDisplayState(captureWidth, captureHeight, currentRotationDegrees())
        Log.i(TAG, "方向/尺寸变化, 已重建采集面: ${captureWidth}x${captureHeight}")
    }

    private fun currentRotationDegrees(): Int {
        val display = getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)
        return when (display?.rotation) {
            Surface.ROTATION_90 -> 90
            Surface.ROTATION_180 -> 180
            Surface.ROTATION_270 -> 270
            else -> 0
        }
    }

    private fun startForegroundNotification() {
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.service_channel_name),
                NotificationManager.IMPORTANCE_LOW,
            )
            manager.createNotificationChannel(channel)
        }
        val notification: Notification = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.service_notification_title))
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setOngoing(true)
            .build()
        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            notification,
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION,
        )
    }

    private fun releaseCapture() {
        getSystemService(DisplayManager::class.java)?.unregisterDisplayListener(displayListener)
        virtualDisplay?.release()
        virtualDisplay = null
        imageReader?.close()
        imageReader = null
        projection?.unregisterCallback(projectionCallback)
        projection?.stop()
        projection = null
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
    }

    companion object {
        const val ACTION_START = "com.rdc.remote.action.START_CAPTURE"
        const val ACTION_STOP = "com.rdc.remote.action.STOP_CAPTURE"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"

        private const val TAG = "RdcCapture"
        private const val CHANNEL_ID = "rdc_capture"
        private const val NOTIFICATION_ID = 1001
        private const val DISPLAY_NAME = "rdc-capture"
        private const val MAX_IMAGES = 2
    }
}
