// ==========================================================================================
// 文件        : SessionActivity.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 控制端会话窗: 远端画面容器、悬浮手势条、状态条、横竖屏切换与底部抽屉
// 架构定位    : Android 控制端界面层, 只呈现画面与转发输入, 不承载会话业务逻辑
// 核心功能    : TextureView 按等比铺满呈现远端画面; 触摸/键盘转契约输入事件; 旋转不重建界面只迁移布局;
//               竖屏为画面 + 信息卡 + 操作区, 横屏为沉浸全屏画面 + 轻触唤出的浮条 + 底部抽屉
// 职责边界    : 不建连、不解码协议、不决策降级; 所有命令走 ControlContract; 不读写系统剪贴板
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34)
// 依赖关系    : RemoteRenderer、ControlContract、InputEvent、RdcTheme、RdcLog
// ==========================================================================================

package com.rdc.remote

import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.SurfaceTexture
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.TextWatcher
import android.view.GestureDetector
import android.view.Gravity
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.Surface
import android.view.TextureView
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SwitchCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import java.util.Locale

class SessionActivity : AppCompatActivity() {

    private lateinit var root: FrameLayout
    private lateinit var content: LinearLayout
    private lateinit var remoteBox: AspectFrameLayout
    private lateinit var videoView: TextureView
    private var videoSurface: Surface? = null
    private var attachedTexture: SurfaceTexture? = null
    private lateinit var placeholder: TextView
    private lateinit var topRow: LinearLayout
    private lateinit var peerChip: TextView
    private lateinit var topMetrics: TextView
    private lateinit var metricPill: TextView
    private lateinit var remoteLabel: TextView

    private lateinit var portraitBody: ScrollView
    private lateinit var stateBadge: TextView
    private lateinit var stateLine: TextView
    private lateinit var statFps: TextView
    private lateinit var statLatency: TextView
    private lateinit var statBitrate: TextView
    private lateinit var statProfile: TextView
    private lateinit var chipRow: LinearLayout

    private lateinit var gestureBar: LinearLayout
    private lateinit var scaleLabel: TextView
    private lateinit var topLeftItem: TextView
    private lateinit var qualitySheet: LinearLayout
    private lateinit var transferSheet: LinearLayout
    private lateinit var imeSink: EditText

    private val transferCards = mutableListOf<TransferViews>()
    private val clipboardCards = mutableListOf<ClipboardViews>()

    private val renderer = RemoteRenderer()
    private val ticker = Handler(Looper.getMainLooper())
    private val transferHandler = Handler(Looper.getMainLooper())

    private var sessionId: String = ""
    private var profile: String = "auto"
    private var peerId: String = ""
    private var offlinePreview = false

    @Volatile
    private var remoteWidth = 1920
    @Volatile
    private var remoteHeight = 1080

    /** 是否已收到过远端画面帧; 决定占位层是否让位给解码画面. */
    @Volatile
    private var firstFrameSeen = false

    /** 双指拖动累积量, 用于全屏原始像素下的视口平移. */
    private var multiTouchActive = false
    private var lastMultiX = 0f
    private var lastMultiY = 0f

    /** 全屏缩放倍率(1.0 = 原始像素)与视口偏移; 退出全屏时归零重算. */
    private var viewportScale = 0f
    private var viewportPanX = 0f
    private var viewportPanY = 0f

    /** 双指缩放与双击还原; 单指事件仍直通远端鼠标操作. */
    private val scaleDetector: ScaleGestureDetector by lazy {
        ScaleGestureDetector(this, object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScale(detector: ScaleGestureDetector): Boolean {
                val state = currentViewport()
                applyViewport(
                    ViewportMath.zoomAt(
                        state,
                        state.scale * detector.scaleFactor,
                        detector.focusX,
                        detector.focusY,
                    ),
                )
                return true
            }
        })
    }

    private val doubleTapDetector: GestureDetector by lazy {
        GestureDetector(this, object : GestureDetector.SimpleOnGestureListener() {
            override fun onDoubleTap(e: MotionEvent): Boolean {
                resetViewport()
                return true
            }
        })
    }

    private var fullscreen = false
    private var sheetOpen: String? = null

    /** 上一次输出的铺满日志, 避免布局回调重复刷屏. */
    private var lastViewportLog = ""

    /** 竖屏操作区按钮与横屏浮条条目, 供真机自动化按坐标点击时读取. */
    private val actionButtons = mutableListOf<TextView>()
    private val gestureItems = mutableListOf<View>()
    private var lastActionLog = ""
    private var lastBarLog = ""

    /** 浮条自动淡出计时. */
    private val hideBarRunnable = Runnable { fadeOutFloatingBar() }

    /** 文件传输示意状态; 只驱动界面, 不产生真实读写. */
    private var transferProgress = 0
    private var transferRunning = false
    private var transferPaused = false
    private var transferCancelled = false
    private var transferCompleted = false
    private val transferFileName = "demo-clip-2026-11-10.mp4"

    /** 剪贴板示意状态; 不访问系统剪贴板. */
    private var clipboardSyncEnabled = true
    private var clipboardLastStatus = ""

    private val metricsTick = object : Runnable {
        override fun run() {
            refreshStatus()
            ticker.postDelayed(this, 1000)
        }
    }

    private val transferTick = object : Runnable {
        override fun run() {
            if (!transferRunning || transferPaused || !transferAvailable()) {
                return
            }
            transferProgress = (transferProgress + TRANSFER_STEP).coerceAtMost(100)
            if (transferProgress >= 100) {
                transferRunning = false
                transferCompleted = true
                refreshTransfer()
                RdcLog.info("Session", "transfer_sim_done", "progress=100")
                return
            }
            refreshTransfer()
            transferHandler.postDelayed(this, TRANSFER_TICK_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 与主界面共用外观设置, 构建界面前先定好深/浅色
        RdcColor.dark = AppPrefs.darkTheme(this)
        sessionId = intent.getStringExtra(EXTRA_SESSION_ID) ?: ""
        profile = intent.getStringExtra(EXTRA_PROFILE) ?: "auto"
        offlinePreview = intent.getBooleanExtra(EXTRA_OFFLINE_PREVIEW, false)
        peerId = intent.getStringExtra(EXTRA_PEER_ID) ?: ""
        remoteWidth = intent.getIntExtra(EXTRA_REMOTE_WIDTH, 1920)
        remoteHeight = intent.getIntExtra(EXTRA_REMOTE_HEIGHT, 1080)
        clipboardSyncEnabled = AppPrefs.clipboardSync(this)

        clipboardLastStatus = getString(R.string.session_clipboard_idle)
        buildViews()
        if (!offlinePreview) {
            placeholder.text = "已连接，等待首帧画面…"
            LanSession.setVideoSink { frame -> onVideoFrame(frame) }
        }
        applySessionLayout()
        applySheet()
        refreshTransfer()
        refreshClipboard()
        refreshStatus()

        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    if (fullscreen) {
                        exitFullscreen()
                    } else {
                        finishSession()
                    }
                }
            },
        )

        RdcLog.info(
            "Session",
            "page_ready",
            "session=$sessionId profile=$profile offline=$offlinePreview orientation=portrait wired=${ControlContract.WIRED}",
        )
    }

    override fun onResume() {
        super.onResume()
        ticker.post(metricsTick)
        // 从后台或系统弹窗回来时, 全屏仍保持隐藏系统栏
        if (fullscreen) {
            setSystemBarsHidden(true)
        }
        if (transferRunning && !transferPaused && transferAvailable()) {
            transferHandler.postDelayed(transferTick, TRANSFER_TICK_MS)
        }
    }

    override fun onPause() {
        ticker.removeCallbacks(metricsTick)
        ticker.removeCallbacks(hideBarRunnable)
        transferHandler.removeCallbacks(transferTick)
        super.onPause()
    }

    override fun onDestroy() {
        ticker.removeCallbacks(metricsTick)
        ticker.removeCallbacks(hideBarRunnable)
        transferHandler.removeCallbacks(transferTick)
        LanSession.setVideoSink(null)
        renderer.release()
        videoSurface?.release()
        videoSurface = null
        RdcLog.info("Session", "page_destroyed", "session=$sessionId")
        super.onDestroy()
    }

    // ---------------------------------------------------------------- 界面骨架

    private fun buildViews() {
        root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }

        content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        remoteBox = AspectFrameLayout(this).apply {
            setBackgroundColor(RdcColor.SURFACE)
            setPadding(0, 0, 0, 0)
        }
        videoView = TextureView(this).apply {
            surfaceTextureListener = textureListener
            // 读屏把远端画面识别为一块可触摸区域, 而不是空白
            contentDescription = getString(R.string.a11y_remote_surface)
            setOnTouchListener { view, event ->
                if (event.actionMasked == MotionEvent.ACTION_UP) {
                    view.performClick()
                }
                onRemoteTouch(event)
            }
            addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> applyViewportMatrix() }
        }
        placeholder = labelView(getString(R.string.session_placeholder), 12f, RdcColor.FAINT, Gravity.CENTER)

        peerChip = TextView(this).apply {
            text = getString(R.string.session_peer_format, shortPeer())
            textSize = RdcText.TINY
            setTextColor(RdcColor.MEDIA_TEXT)
            setPadding(dp(8), dp(5), dp(8), dp(5))
            roundedBackground(RdcColor.OVERLAY, 7, OVERLAY_BORDER)
            typeface = Typeface.MONOSPACE
        }
        topMetrics = TextView(this).apply {
            text = getString(R.string.session_metrics_placeholder, qualityLabel(profile))
            textSize = RdcText.MICRO
            setTextColor(RdcColor.MEDIA_TEXT)
            setPadding(dp(8), dp(5), dp(8), dp(5))
            roundedBackground(RdcColor.OVERLAY, 6, OVERLAY_BORDER)
            typeface = Typeface.MONOSPACE
            visibility = View.GONE
        }
        metricPill = TextView(this).apply {
            text = getString(R.string.session_metrics_placeholder, qualityLabel(profile))
            textSize = RdcText.MICRO
            setTextColor(RdcColor.MEDIA_TEXT)
            setPadding(dp(8), dp(4), dp(8), dp(4))
            roundedBackground(RdcColor.OVERLAY, 6, OVERLAY_BORDER)
            typeface = Typeface.MONOSPACE
        }
        remoteLabel = TextView(this).apply {
            text = getString(R.string.session_remote_label)
            textSize = RdcText.MICRO
            setTextColor(RdcColor.MEDIA_TEXT_STRONG)
        }

        topRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(9), dp(9), dp(9), 0)
        }
        topRow.addView(roundButton("←") { onHeaderBack() })
        topRow.addView(
            peerChip,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { leftMargin = dp(7) },
        )
        topRow.addView(
            View(this),
            LinearLayout.LayoutParams(0, 1, 1f),
        )
        topRow.addView(topMetrics)

        remoteBox.addView(videoView, matchParent())
        remoteBox.addView(placeholder, matchParent())
        remoteBox.addView(
            topRow,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.TOP,
            ),
        )
        remoteBox.addView(
            metricPill,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM or Gravity.START,
            ).apply {
                leftMargin = dp(9)
                bottomMargin = dp(9)
            },
        )
        remoteBox.addView(
            remoteLabel,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM or Gravity.END,
            ).apply {
                rightMargin = dp(9)
                bottomMargin = dp(9)
            },
        )

        portraitBody = ScrollView(this).apply {
            isFillViewport = true
            visibility = View.GONE
        }
        portraitBody.addView(buildPortraitBody())

        gestureBar = buildGestureBar()
        gestureBar.visibility = View.GONE
        qualitySheet = buildQualitySheet()
        transferSheet = buildTransferSheet()
        qualitySheet.visibility = View.GONE
        transferSheet.visibility = View.GONE

        imeSink = EditText(this).apply {
            alpha = 0f
            width = 1
            height = 1
            addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) = Unit
                override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) = Unit
                override fun afterTextChanged(s: Editable?) {
                    val text = s?.toString().orEmpty()
                    if (text.isNotEmpty()) {
                        forwardImeText(text)
                        setText("")
                    }
                }
            })
        }

        content.addView(remoteBox)
        content.addView(portraitBody)
        root.addView(content, matchParent())
        root.addView(
            gestureBar,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL,
            ).apply { bottomMargin = dp(20) },
        )
        root.addView(qualitySheet, sheetParams())
        root.addView(transferSheet, sheetParams())
        root.addView(imeSink, FrameLayout.LayoutParams(1, 1, Gravity.BOTTOM))
        setContentView(root)
    }

    private fun buildPortraitBody(): LinearLayout {
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(12), dp(14), dp(24))
        }

        val titleRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.TOP
        }
        val titleColumn = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        titleColumn.addView(labelView(getString(R.string.session_title), 14f, RdcColor.TEXT))
        stateLine = labelView(getString(R.string.session_state_offline), 9.5f, RdcColor.FAINT).apply {
            setPadding(0, dp(2), 0, 0)
        }
        titleColumn.addView(stateLine)
        titleRow.addView(titleColumn)
        stateBadge = TextView(this).apply {
            text = getString(R.string.session_badge_offline)
            textSize = RdcText.MICRO
            setTextColor(RdcColor.WARN)
            setPadding(dp(8), dp(4), dp(8), dp(4))
            roundedBackground(RdcColor.PANEL_2, 999, RdcColor.LINE)
        }
        titleRow.addView(stateBadge)
        column.addView(titleRow)

        val grid = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(10), 0, 0)
        }
        val firstRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        statFps = statCell(firstRow, getString(R.string.session_stat_fps), 0)
        statLatency = statCell(firstRow, getString(R.string.session_stat_latency), 7)
        val secondRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(7), 0, 0)
        }
        statBitrate = statCell(secondRow, getString(R.string.session_stat_bitrate), 0)
        statProfile = statCell(secondRow, getString(R.string.session_stat_profile), 7)
        grid.addView(firstRow)
        grid.addView(secondRow)
        column.addView(grid)

        chipRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(10), 0, 0)
        }
        for (item in ControlContract.DIRECT_PROFILES) {
            val chip = qualityChip(item)
            chipRow.addView(
                chip,
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ).apply { rightMargin = dp(6) },
            )
        }
        column.addView(
            HorizontalScrollView(this).apply {
                isHorizontalScrollBarEnabled = false
                addView(chipRow)
            },
        )

        column.addView(buildTransferCard())
        column.addView(buildClipboardCard())
        column.addView(buildActionGrid())
        refreshChipSelection()
        return column
    }

    /** 2x2 操作区: 横屏全屏、键盘、画质、断开. */
    private fun buildActionGrid(): LinearLayout {
        val grid = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(12), 0, 0)
        }
        val row1 = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        row1.addView(
            actionButton(getString(R.string.session_fullscreen), RdcColor.ACCENT, Color.WHITE) {
                enterFullscreen()
            },
            actionParams(0),
        )
        row1.addView(
            actionButton(getString(R.string.session_keyboard), RdcColor.PANEL_3, RdcColor.TEXT) {
                toggleKeyboard()
            },
            actionParams(8),
        )
        val row2 = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(8), 0, 0)
        }
        row2.addView(
            actionButton(getString(R.string.session_quality), RdcColor.PANEL_3, RdcColor.TEXT) {
                openSheet(SHEET_QUALITY)
            },
            actionParams(0),
        )
        row2.addView(
            actionButton(getString(R.string.session_disconnect), RdcColor.PANEL_3, RdcColor.DANGER) {
                finishSession()
            },
            actionParams(8),
        )
        grid.addView(row1)
        grid.addView(row2)
        grid.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> logActionButtons() }
        return grid
    }

    /** 记录竖屏操作区按钮的屏幕坐标与可用状态, 真机自动化据此点击. */
    private fun logActionButtons() {
        val parts = actionButtons.map { button ->
            val loc = IntArray(2)
            button.getLocationOnScreen(loc)
            "${button.text}=${loc[0]},${loc[1]},${button.width}x${button.height},enabled=${button.isEnabled}"
        }
        val line = parts.joinToString("|")
        if (line.isNotBlank() && line != lastActionLog) {
            lastActionLog = line
            RdcLog.info("Session", "action_buttons", line)
        }
    }

    private fun actionButton(text: String, background: Int, textColor: Int, onClick: () -> Unit): TextView =
        actionView(text, 11.5f, textColor, background, 10, RdcColor.LINE_STRONG, 39).apply {
            layoutParams = LinearLayout.LayoutParams(0, RdcMetrics.BUTTON, 1f)
            setOnClickListener { onClick() }
        }.also { actionButtons += it }

    private fun actionParams(startMargin: Int) = LinearLayout.LayoutParams(
        0,
        dp(39),
        1f,
    ).apply { leftMargin = dp(startMargin) }

    private fun roundButton(text: String, onClick: () -> Unit): TextView =
        TextView(this).apply {
            this.text = text
            textSize = RdcText.HEADLINE
            setTextColor(RdcColor.MEDIA_TEXT)
            gravity = Gravity.CENTER
            isClickable = true
            isFocusable = true
            roundedBackground(RdcColor.OVERLAY, 8, OVERLAY_BORDER)
            layoutParams = LinearLayout.LayoutParams(dp(32), dp(32))
            setOnClickListener { onClick() }
        }

    private fun statCell(row: LinearLayout, label: String, startMarginDp: Int): TextView {
        val cell = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(10), dp(8), dp(10), dp(8))
            roundedBackground(RdcColor.PANEL_2, 9, RdcColor.LINE)
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply {
                leftMargin = dp(startMarginDp)
            }
        }
        cell.addView(labelView(label, 9f, RdcColor.FAINT))
        val value = TextView(this).apply {
            text = "—"
            textSize = RdcText.BODY
            setTextColor(RdcColor.TEXT)
            typeface = Typeface.MONOSPACE
            setPadding(0, dp(2), 0, 0)
        }
        cell.addView(value)
        row.addView(cell)
        return value
    }

    private fun qualityChip(item: String): TextView =
        TextView(this).apply {
            text = qualityLabel(item)
            textSize = RdcText.TINY
            setTextColor(RdcColor.MUTED)
            setPadding(dp(9), dp(5), dp(9), dp(5))
            isClickable = true
            isFocusable = true
            roundedBackground(RdcColor.PANEL_2, 999, RdcColor.LINE_STRONG)
            setOnClickListener { applyProfile(item) }
        }

    // ---------------------------------------------------------------- 卡片

    private fun card(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(12), dp(10), dp(12), dp(12))
        roundedBackground(RdcColor.PANEL, 11, RdcColor.LINE)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(10) }
    }

    private fun cardHead(title: String): LinearLayout {
        val head = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        head.addView(
            labelView(title, 12f, RdcColor.TEXT).apply {
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            },
        )
        return head
    }

    private fun buildTransferCard(): LinearLayout {
        val card = card()
        val head = cardHead(getString(R.string.session_transfer_title))
        val badge = TextView(this).apply {
            text = getString(R.string.session_transfer_state_waiting)
            textSize = RdcText.MICRO
            setTextColor(RdcColor.MUTED)
            setPadding(dp(8), dp(3), dp(8), dp(3))
            roundedBackground(RdcColor.PANEL_2, 999, RdcColor.LINE)
        }
        head.addView(badge)
        card.addView(head)

        val stateRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(6), 0, 0)
        }
        val state = labelView(getString(R.string.session_transfer_state_waiting), 10f, RdcColor.MUTED)
        val pct = labelView("0%", 10f, RdcColor.MUTED, Gravity.END)
        stateRow.addView(state, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        stateRow.addView(pct)
        card.addView(stateRow)

        val progress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            progress = 0
            progressTintList = ColorStateList.valueOf(RdcColor.ACCENT)
            progressBackgroundTintList = ColorStateList.valueOf(RdcColor.LINE)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(3),
            ).apply { topMargin = dp(6) }
        }
        card.addView(progress)

        val meta = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(6), 0, 0)
        }
        val rate = labelView(getString(R.string.session_transfer_rate, "—"), 9.5f, RdcColor.MUTED)
        val eta = labelView(getString(R.string.session_transfer_eta, "—"), 9.5f, RdcColor.MUTED, Gravity.END)
        meta.addView(rate, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        meta.addView(eta)
        card.addView(meta)

        val file = labelView(getString(R.string.session_transfer_file, "—"), 9.5f, RdcColor.FAINT).apply {
            setPadding(0, dp(4), 0, 0)
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.MIDDLE
        }
        card.addView(file)

        val ops1 = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(8), 0, 0)
        }
        val pick = smallButton(getString(R.string.session_transfer_pick)) { toast(getString(R.string.session_transfer_pick_note)) }
        val pause = smallButton(getString(R.string.session_transfer_pause)) { toggleTransferPause() }
        ops1.addView(pick, smallParams(0))
        ops1.addView(pause, smallParams(6))
        card.addView(ops1)

        val ops2 = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(6), 0, 0)
        }
        val cancel = smallButton(getString(R.string.session_transfer_cancel)) { cancelTransfer() }
        val retry = smallButton(getString(R.string.session_transfer_retry)) { retryTransfer() }
        ops2.addView(cancel, smallParams(0))
        ops2.addView(retry, smallParams(6))
        card.addView(ops2)

        card.addView(labelView(getString(R.string.session_transfer_note), 9f, RdcColor.FAINT).apply {
            setPadding(0, dp(6), 0, 0)
        })

        transferCards.add(
            TransferViews(card, badge, state, pct, progress, rate, eta, file, pick, pause, cancel, retry),
        )
        return card
    }

    private fun buildClipboardCard(): LinearLayout {
        val card = card()
        val head = cardHead(getString(R.string.session_clipboard_title))
        val toggle = SwitchCompat(this).apply {
            isChecked = clipboardSyncEnabled
            setOnCheckedChangeListener { _, checked -> onClipboardSyncChanged(checked) }
        }
        head.addView(toggle)
        card.addView(head)

        val status = labelView(clipboardLastStatus, 10f, RdcColor.MUTED).apply {
            setPadding(0, dp(6), 0, 0)
        }
        card.addView(status)

        val ops = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(8), 0, 0)
        }
        val push = smallButton(getString(R.string.session_clipboard_send)) { sendLocalClipboardText() }
        val pull = smallButton(getString(R.string.session_clipboard_pull)) { pullRemoteClipboardText() }
        ops.addView(push, smallParams(0))
        ops.addView(pull, smallParams(6))
        card.addView(ops)
        card.addView(labelView(getString(R.string.session_clipboard_note), 9f, RdcColor.FAINT).apply {
            setPadding(0, dp(6), 0, 0)
        })

        clipboardCards.add(ClipboardViews(card, toggle, status, push, pull))
        return card
    }

    private fun smallButton(text: String, onClick: () -> Unit): TextView =
        actionView(text, 10f, RdcColor.TEXT, RdcColor.PANEL_3, 8, RdcColor.LINE_STRONG, 30).apply {
            layoutParams = LinearLayout.LayoutParams(0, RdcMetrics.BUTTON_SMALL, 1f)
            setOnClickListener { onClick() }
        }

    private fun smallParams(startMargin: Int) = LinearLayout.LayoutParams(
        0,
        dp(30),
        1f,
    ).apply { leftMargin = dp(startMargin) }

    // ---------------------------------------------------------------- 横屏手势条与抽屉

    /** 全屏浮条: 轻触画面唤出, 3 秒后自动淡出; 不含任何常驻控件. */
    private fun buildGestureBar(): LinearLayout {
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(6), dp(4), dp(6), dp(4))
            roundedBackground(RdcColor.OVERLAY, 999, RdcColor.LINE_STRONG)
        }
        scaleLabel = TextView(this).apply {
            text = getString(R.string.session_zoom_fit)
            textSize = RdcText.BODY_SMALL
            setTextColor(RdcColor.FAINT)
            gravity = Gravity.CENTER
            setPadding(dp(10), dp(9), dp(10), dp(9))
            typeface = Typeface.MONOSPACE
        }
        bar.addView(scaleLabel)
        bar.addView(divider())
        bar.addView(gestureItem(getString(R.string.session_reset_view)) { resetViewport() })
        topLeftItem = gestureItem(getString(R.string.session_home_top_left)) { moveViewportToTopLeft() }
        bar.addView(thinDivider())
        bar.addView(topLeftItem)
        bar.addView(divider())
        bar.addView(gestureItem(getString(R.string.session_keyboard)) { toggleKeyboard() })
        bar.addView(divider())
        bar.addView(gestureItem(getString(R.string.session_quality)) { openSheet(SHEET_QUALITY) })
        bar.addView(divider())
        bar.addView(gestureItem(getString(R.string.session_transfer)) { openSheet(SHEET_TRANSFER) })
        bar.addView(divider())
        bar.addView(gestureItem(getString(R.string.session_disconnect)) { finishSession() })
        bar.addView(divider())
        bar.addView(gestureItem(getString(R.string.session_back)) { exitFullscreen() })
        return bar
    }

    private fun thinDivider(): View = View(this).apply {
        setBackgroundColor(RdcColor.LINE_STRONG)
        layoutParams = LinearLayout.LayoutParams(dp(1), dp(12)).apply {
            leftMargin = dp(2)
            rightMargin = dp(2)
        }
    }

    private fun gestureItem(text: String, onClick: () -> Unit): TextView =
        TextView(this).apply {
            this.text = text
            textSize = RdcText.BODY_SMALL
            setTextColor(RdcColor.TEXT)
            gravity = Gravity.CENTER
            setPadding(dp(8), dp(9), dp(8), dp(9))
            isClickable = true
            isFocusable = true
            setOnClickListener { onClick() }
        }.also { gestureItems += it }

    private fun divider(): View = View(this).apply {
        setBackgroundColor(RdcColor.LINE_STRONG)
        layoutParams = LinearLayout.LayoutParams(dp(1), dp(16)).apply {
            leftMargin = dp(2)
            rightMargin = dp(2)
        }
    }

    private fun buildQualitySheet(): LinearLayout {
        val sheet = sheetContainer(getString(R.string.session_quality_title)) { closeSheet() }
        for (item in ControlContract.DIRECT_PROFILES) {
            sheet.addView(
                actionView(
                    getString(R.string.session_quality_option, qualityLabel(item), qualityNote(item)),
                    11f,
                    RdcColor.TEXT,
                    RdcColor.PANEL_2,
                    9,
                    RdcColor.LINE,
                    38,
                ).apply {
                    gravity = Gravity.CENTER_VERTICAL or Gravity.START
                    setPadding(dp(12), 0, dp(12), 0)
                    setOnClickListener {
                        applyProfile(item)
                        closeSheet()
                    }
                },
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ).apply { topMargin = dp(6) },
            )
        }
        return sheet
    }

    private fun buildTransferSheet(): LinearLayout {
        val sheet = sheetContainer(getString(R.string.session_transfer_sheet_title)) { closeSheet() }

        val head = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        head.addView(
            labelView(getString(R.string.session_transfer_title), 12f, RdcColor.TEXT).apply {
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            },
        )
        val badge = TextView(this).apply {
            text = getString(R.string.session_transfer_state_waiting)
            textSize = RdcText.MICRO
            setTextColor(RdcColor.MUTED)
            setPadding(dp(8), dp(3), dp(8), dp(3))
            roundedBackground(RdcColor.PANEL_2, 999, RdcColor.LINE)
        }
        head.addView(badge)
        sheet.addView(head)

        val stateRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(6), 0, 0)
        }
        val state = labelView(getString(R.string.session_transfer_state_waiting), 10f, RdcColor.MUTED)
        val pct = labelView("0%", 10f, RdcColor.MUTED, Gravity.END)
        stateRow.addView(state, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        stateRow.addView(pct)
        sheet.addView(stateRow)

        val progress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            progress = 0
            progressTintList = ColorStateList.valueOf(RdcColor.ACCENT)
            progressBackgroundTintList = ColorStateList.valueOf(RdcColor.LINE)
        }
        sheet.addView(
            progress,
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(3)).apply {
                topMargin = dp(6)
            },
        )

        val meta = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(6), 0, 0)
        }
        val rate = labelView(getString(R.string.session_transfer_rate, "—"), 9.5f, RdcColor.MUTED)
        val eta = labelView(getString(R.string.session_transfer_eta, "—"), 9.5f, RdcColor.MUTED, Gravity.END)
        meta.addView(rate, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        meta.addView(eta)
        sheet.addView(meta)

        val file = labelView(getString(R.string.session_transfer_file, "—"), 9.5f, RdcColor.FAINT).apply {
            setPadding(0, dp(4), 0, 0)
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.MIDDLE
        }
        sheet.addView(file)

        val ops = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(8), 0, 0)
        }
        val pick = smallButton(getString(R.string.session_transfer_pick)) { toast(getString(R.string.session_transfer_pick_note)) }
        val pause = smallButton(getString(R.string.session_transfer_pause)) { toggleTransferPause() }
        val cancel = smallButton(getString(R.string.session_transfer_cancel)) { cancelTransfer() }
        val retry = smallButton(getString(R.string.session_transfer_retry)) { retryTransfer() }
        ops.addView(pick, smallParams(0))
        ops.addView(pause, smallParams(6))
        ops.addView(cancel, smallParams(6))
        ops.addView(retry, smallParams(6))
        sheet.addView(ops)

        val clipHead = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(12), 0, 0)
        }
        clipHead.addView(
            labelView(getString(R.string.session_clipboard_title), 12f, RdcColor.TEXT).apply {
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            },
        )
        val toggle = SwitchCompat(this).apply {
            isChecked = clipboardSyncEnabled
            setOnCheckedChangeListener { _, checked -> onClipboardSyncChanged(checked) }
        }
        clipHead.addView(toggle)
        sheet.addView(clipHead)

        val clipStatus = labelView(clipboardLastStatus, 10f, RdcColor.MUTED).apply {
            setPadding(0, dp(6), 0, 0)
        }
        sheet.addView(clipStatus)

        val clipOps = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(8), 0, 0)
        }
        val push = smallButton(getString(R.string.session_clipboard_send)) { sendLocalClipboardText() }
        val pull = smallButton(getString(R.string.session_clipboard_pull)) { pullRemoteClipboardText() }
        clipOps.addView(push, smallParams(0))
        clipOps.addView(pull, smallParams(6))
        sheet.addView(clipOps)
        sheet.addView(labelView(getString(R.string.session_clipboard_note), 9f, RdcColor.FAINT).apply {
            setPadding(0, dp(6), 0, 0)
        })

        transferCards.add(
            TransferViews(sheet, badge, state, pct, progress, rate, eta, file, pick, pause, cancel, retry),
        )
        clipboardCards.add(ClipboardViews(sheet, toggle, clipStatus, push, pull))
        return sheet
    }

    private fun sheetContainer(title: String, onClose: () -> Unit): LinearLayout {
        val sheet = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(10), dp(14), dp(14))
            roundedBackground(RdcColor.OVERLAY, 16, RdcColor.LINE_STRONG)
        }
        val head = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        head.addView(
            labelView(title, 12f, RdcColor.TEXT).apply {
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            },
        )
        head.addView(
            labelView(getString(R.string.session_sheet_close), 11f, RdcColor.MUTED).apply {
                isClickable = true
                isFocusable = true
                setPadding(dp(8), dp(4), dp(8), dp(4))
                setOnClickListener { onClose() }
            },
        )
        sheet.addView(head)
        return sheet
    }

    private fun sheetParams() = FrameLayout.LayoutParams(
        FrameLayout.LayoutParams.MATCH_PARENT,
        FrameLayout.LayoutParams.WRAP_CONTENT,
        Gravity.BOTTOM,
    ).apply {
        leftMargin = dp(10)
        rightMargin = dp(10)
        bottomMargin = dp(10)
    }

    // ---------------------------------------------------------------- 布局切换

    /** 会话布局: 窗口态为画面 + 信息卡 + 操作区, 全屏态为可缩放的沉浸画面(固定竖屏). */
    private fun applySessionLayout() {
        if (fullscreen) {
            // 全屏: 关闭固定比例, 远端按适应屏幕显示, 可放大到 4 倍后双指拖动查看
            remoteBox.aspectEnabled = false
            remoteBox.ratio = 9f / 16f
            remoteBox.layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1f,
            )
            portraitBody.visibility = View.GONE
            topRow.visibility = View.GONE
            metricPill.visibility = View.GONE
            remoteLabel.visibility = View.GONE
            hideFloatingBar()
            setSystemBarsHidden(true)
            remoteBox.roundedBackground(RdcColor.SURFACE, 0)
        } else {
            // 回到窗口态: 倍率与偏移不残留, 按窗口尺寸重新适配
            viewportScale = 0f
            viewportPanX = 0f
            viewportPanY = 0f
            remoteBox.aspectEnabled = true
            remoteBox.layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            )
            portraitBody.visibility = View.VISIBLE
            portraitBody.layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1f,
            )
            topRow.visibility = View.VISIBLE
            metricPill.visibility = View.VISIBLE
            hideFloatingBar()
            setSystemBarsHidden(false)
            remoteBox.roundedBackground(RdcColor.SURFACE, 16)
        }
        renderer.updateOrientation(rotationDegrees(), remoteWidth, remoteHeight)
        // 进入全屏按适应屏幕的默认档重算并钳制
        applyDefaultViewport()
        applySheet()
        refreshStatus()
    }

    private fun enterFullscreen() {
        if (fullscreen) {
            return
        }
        fullscreen = true
        applySessionLayout()
        RdcLog.info("Session", "enter_fullscreen", "session=$sessionId")
    }

    /** 全屏「返回」: 不结束会话, 只切回窗口态. */
    private fun exitFullscreen() {
        if (!fullscreen) {
            return
        }
        closeSheet()
        fullscreen = false
        applySessionLayout()
        RdcLog.info("Session", "exit_fullscreen", "session=$sessionId")
    }

    private fun onHeaderBack() {
        if (fullscreen) {
            exitFullscreen()
        } else {
            finishSession()
        }
    }

    private fun openSheet(which: String) {
        sheetOpen = if (sheetOpen == which) null else which
        hideFloatingBar()
        applySheet()
        RdcLog.info("Session", "sheet_toggle", "sheet=${sheetOpen ?: "none"}")
    }

    private fun closeSheet() {
        sheetOpen = null
        applySheet()
    }

    private fun applySheet() {
        val visible = fullscreen && sheetOpen != null
        qualitySheet.visibility = if (visible && sheetOpen == SHEET_QUALITY) View.VISIBLE else View.GONE
        transferSheet.visibility = if (visible && sheetOpen == SHEET_TRANSFER) View.VISIBLE else View.GONE
        // 全屏下不保留任何常驻控件; 浮条只在轻触后短暂出现
        topRow.visibility = if (fullscreen) View.GONE else View.VISIBLE
        metricPill.visibility = if (fullscreen) View.GONE else View.VISIBLE
        if (fullscreen) {
            remoteLabel.visibility = View.GONE
        }
    }

    /** 全屏隐藏系统状态栏与导航栏; 退出全屏立即恢复. */
    private fun setSystemBarsHidden(hidden: Boolean) {
        val controller = WindowInsetsControllerCompat(window, root)
        if (hidden) {
            controller.systemBarsBehavior =
                WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            controller.hide(WindowInsetsCompat.Type.systemBars())
        } else {
            controller.show(WindowInsetsCompat.Type.systemBars())
        }
        RdcLog.info("Session", "system_bars", "hidden=$hidden")
    }

    /** 轻触画面: 唤出浮条并重置 3 秒淡出计时. */
    private fun showFloatingBar() {
        if (!fullscreen) {
            return
        }
        ticker.removeCallbacks(hideBarRunnable)
        gestureBar.animate().cancel()
        gestureBar.visibility = View.VISIBLE
        gestureBar.animate().alpha(1f).setDuration(FADE_IN_MS).start()
        ticker.postDelayed(hideBarRunnable, FLOATING_BAR_MS)
        RdcLog.info("Session", "floating_bar_shown", "timeout_ms=$FLOATING_BAR_MS")
        // 浮条内容与坐标落日志: 会话页持续出帧, uiautomator 取不到层级, 真机自动化只能读日志
        gestureBar.postDelayed({ logFloatingBarItems() }, 120)
    }

    /** 记录浮条可见条目、倍率文字、可用状态与屏幕坐标, 供真机截图核对与坐标点击. */
    private fun logFloatingBarItems() {
        val views = mutableListOf<View>(scaleLabel)
        views += gestureItems
        val parts = views.map { view ->
            val loc = IntArray(2)
            view.getLocationOnScreen(loc)
            val label = (view as? TextView)?.text?.toString().orEmpty()
            "$label=${loc[0]},${loc[1]},${view.width}x${view.height}," +
                "enabled=${view.isEnabled},alpha=${String.format(Locale.US, "%.2f", view.alpha)}"
        }
        lastBarLog = parts.joinToString("|")
        RdcLog.info("Session", "floating_bar_items", lastBarLog)
    }

    private fun fadeOutFloatingBar() {
        if (gestureBar.visibility != View.VISIBLE) {
            return
        }
        gestureBar.animate().cancel()
        gestureBar.animate().alpha(0f).setDuration(FADE_OUT_MS).withEndAction {
            gestureBar.visibility = View.GONE
            gestureBar.alpha = 1f
        }.start()
        RdcLog.info("Session", "floating_bar_hidden", "reason=timeout")
    }

    private fun hideFloatingBar() {
        ticker.removeCallbacks(hideBarRunnable)
        gestureBar.animate().cancel()
        gestureBar.visibility = View.GONE
        gestureBar.alpha = 1f
    }

    /** 按当前视口重算显示矩阵: 全屏按倍率缩放, 窗口态按适应比例. */
    private fun applyViewportMatrix() {
        if (!::videoView.isInitialized) {
            return
        }
        val state = currentViewport()
        // 窗口态每次布局都按适应屏幕重算; 只有全屏态才记住用户手动调的倍率与偏移
        if (fullscreen) {
            viewportScale = state.scale
            viewportPanX = state.panX
            viewportPanY = state.panY
        }
        val matrix = Matrix()
        matrix.setValues(ViewportMath.matrixValues(state))
        videoView.setTransform(matrix)
        updateScaleLabel(state)
        val log = "mode=${if (fullscreen) "fullscreen" else "window"} " +
            "scale=${String.format(Locale.US, "%.2f", state.scale)} " +
            "min=${String.format(Locale.US, "%.2f", ViewportMath.minScale(state))} " +
            "max=${String.format(Locale.US, "%.2f", ViewportMath.maxScale(state))} " +
            "source=${state.sourceWidth.toInt()}x${state.sourceHeight.toInt()} " +
            "view=${state.viewWidth.toInt()}x${state.viewHeight.toInt()} " +
            "content=${ViewportMath.contentWidth(state).toInt()}x${ViewportMath.contentHeight(state).toInt()} " +
            "offset=${ViewportMath.contentLeft(state).toInt()},${ViewportMath.contentTop(state).toInt()}"
        if (log != lastViewportLog) {
            lastViewportLog = log
            RdcLog.info("Session", "viewport_fit", log)
        }
    }

    /** 浮条倍率与「回到左上角」可用状态. */
    private fun updateScaleLabel(state: Viewport) {
        if (::scaleLabel.isInitialized) {
            val fit = ViewportMath.minScale(state)
            // 等于默认档(适应屏幕)时显示「适应」, 否则显示当前倍率
            val atFit = kotlin.math.abs(state.scale - fit) <= fit * 0.005f
            scaleLabel.text = if (atFit) {
                getString(R.string.session_zoom_fit)
            } else {
                getString(R.string.session_zoom_format, ViewportMath.multiplier(state))
            }
        }
        if (::topLeftItem.isInitialized) {
            val canTopLeft = ViewportMath.canPanToTopLeft(state)
            topLeftItem.isEnabled = canTopLeft
            topLeftItem.alpha = if (canTopLeft) 1f else 0.35f
        }
    }

    /** 应用新的视口状态(已夹取), 刷新画面与倍率. */
    private fun applyViewport(state: Viewport) {
        val clamped = ViewportMath.clamp(state)
        viewportScale = clamped.scale
        viewportPanX = clamped.panX
        viewportPanY = clamped.panY
        applyViewportMatrix()
    }

    /** 全屏可缩放; 窗口态固定缩到能看见整个桌面. */
    private fun currentViewport(): Viewport {
        val probe = Viewport(
            viewWidth = videoView.width.toFloat(),
            viewHeight = videoView.height.toFloat(),
            sourceWidth = remoteWidth.toFloat(),
            sourceHeight = remoteHeight.toFloat(),
        )
        // 0 表示用户未手动调整: 默认档 = 适应屏幕
        val default = ViewportMath.defaultScale(probe)
        val base = probe.copy(
            scale = if (viewportScale > 0f) viewportScale else default,
            panX = viewportPanX,
            panY = viewportPanY,
        )
        return ViewportMath.clamp(base)
    }

    /** 进入全屏后回到默认档(适应屏幕), 偏移清零; 界面固定竖屏, 旋转不触发重算. */
    private fun applyDefaultViewport() {
        viewportScale = 0f
        viewportPanX = 0f
        viewportPanY = 0f
        applyViewportMatrix()
        val resolved = currentViewport()
        RdcLog.info(
            "Session",
            "viewport_default",
            "mode=${if (fullscreen) "fullscreen" else "window"} " +
                "scale=${String.format(Locale.US, "%.4f", resolved.scale)}",
        )
    }

    // ---------------------------------------------------------------- 状态刷新

    private fun refreshStatus() {
        val status = ControlContract.getSessionStatus(sessionId)
        val metrics = ControlContract.getSessionMetrics(sessionId)
        // A8: 只消费命令层 state 字段, 不在界面自造状态
        val reportedState = status.data["state"]?.toString()?.lowercase().orEmpty()
        val reportedReason = status.data["reason"]?.toString().orEmpty()
        val failed = reportedState == "failed"
        val reconnecting = reportedState == "reconnecting"
        val connected = status.ok && !offlinePreview && !failed
        val recovering = reconnecting || (connected && firstFrameSeen && renderer.recovering)

        stateLine.text = when {
            failed -> if (reportedReason.isNotBlank()) {
                "连接失败 · $reportedReason"
            } else {
                "连接失败，请重试或断开"
            }
            reconnecting -> getString(
                R.string.session_state_recovering,
                transportLabel(status.data["transport_kind"]?.toString()),
            )
            connected -> getString(
                R.string.session_state_online,
                transportLabel(status.data["transport_kind"]?.toString()),
            )
            else -> getString(R.string.session_state_offline)
        }
        stateBadge.text = when {
            failed -> getString(R.string.session_badge_failed)
            connected -> getString(R.string.session_badge_online)
            else -> getString(R.string.session_badge_offline)
        }
        stateBadge.setTextColor(
            when {
                failed -> RdcColor.DANGER
                connected -> RdcColor.OK
                else -> RdcColor.WARN
            },
        )

        val fps = metricValue(metrics, "fps")
        val p50 = metricValue(metrics, "p50_ms")
        val p95 = metricValue(metrics, "p95_ms")
        val bitrate = metricValue(metrics, "bitrate_kbps")
        val samples = metrics.data["latency_samples"]?.toString()
        // 实际档位以被控端 ready 报出的分辨率为准; 尚未收到 ready 时退回用户选择
        val reportedProfile = metricValue(metrics, "quality_profile")
        val profileText = if (reportedProfile == "—") qualityLabel(profile) else reportedProfile
        // 样本不足时显示实际样本数, 不留空
        val latencyText = when {
            p50 != "—" && p95 != "—" -> "$p50 / $p95"
            samples != null -> getString(R.string.session_latency_pending, samples)
            else -> "—"
        }
        statFps.text = fps
        statLatency.text = latencyText
        statBitrate.text = if (bitrate == "—") "—" else "$bitrate 千比特/秒"
        statProfile.text = profileText

        val pill = if (p50 == "—" && samples != null) {
            getString(
                R.string.session_metrics_pending,
                latencyText,
                if (fps == "—") "—" else fps,
                profileText,
            )
        } else {
            getString(
                R.string.session_metrics_value,
                if (p50 == "—") "—" else p50,
                if (fps == "—") "—" else fps,
                profileText,
            )
        }
        metricPill.text = pill
        topMetrics.text = pill
        refreshChipSelection()
        val showingVideo = connected && firstFrameSeen && renderer.running
        placeholder.text = when {
            failed -> if (reportedReason.isNotBlank()) {
                "连接失败：$reportedReason\n请重试或断开"
            } else {
                "连接失败，请重试或断开"
            }
            recovering -> getString(R.string.session_recovering)
            else -> getString(R.string.session_placeholder)
        }
        placeholder.visibility = if (showingVideo && !recovering && !failed) View.GONE else View.VISIBLE
        remoteLabel.visibility = if (fullscreen || (showingVideo && !recovering)) View.GONE else View.VISIBLE
        attachSessionCopyMenu(failed, reportedReason)
    }

    /** 长按占位层: 中文菜单复制失败原因与会话标识. */
    private fun attachSessionCopyMenu(failed: Boolean, reason: String) {
        placeholder.isLongClickable = true
        placeholder.setOnLongClickListener {
            val entries = mutableListOf<Pair<String, String>>()
            if (failed && reason.isNotBlank()) {
                entries.add("失败原因" to reason)
            }
            if (sessionId.isNotBlank()) {
                entries.add("会话标识" to sessionId)
            }
            if (peerId.isNotBlank()) {
                entries.add("对端设备" to peerId)
            }
            if (entries.isEmpty()) {
                return@setOnLongClickListener true
            }
            AlertDialog.Builder(this)
                .setTitle("复制内容")
                .setItems(entries.map { "复制${it.first}" }.toTypedArray()) { _, index ->
                    val (label, value) = entries[index]
                    val manager = getSystemService(ClipboardManager::class.java)
                    manager.setPrimaryClip(ClipData.newPlainText(label, value))
                    toast("已复制$label")
                }
                .setNegativeButton("取消", null)
                .show()
            true
        }
    }

    /** 远端帧到达: 送入硬解; 画面尺寸变化时同步给触摸坐标映射. */
    private fun onVideoFrame(frame: LanSession.VideoFrame) {
        if (remoteWidth != frame.width || remoteHeight != frame.height) {
            remoteWidth = frame.width
            remoteHeight = frame.height
            root.post { applyViewportMatrix() }
        }
        renderer.feed(frame)
        if (!firstFrameSeen) {
            firstFrameSeen = true
            RdcLog.info(
                "Session",
                "first_frame",
                "size=${frame.width}x${frame.height} key=${frame.key} profile=$profile",
            )
            root.post { refreshStatus() }
        }
    }

    private fun metricValue(result: CommandResult, key: String): String {
        if (!result.ok) {
            return "—"
        }
        return result.data[key]?.toString() ?: "—"
    }

    private fun refreshChipSelection() {
        for (index in 0 until chipRow.childCount) {
            val chip = chipRow.getChildAt(index) as? TextView ?: continue
            val selected = chip.text.toString() == profile
            chip.setTextColor(if (selected) RdcColor.ACCENT_STRONG else RdcColor.MUTED)
            chip.roundedBackground(
                if (selected) RdcColor.ACCENT_SOFT else RdcColor.PANEL_2,
                999,
                if (selected) RdcColor.ACCENT else RdcColor.LINE_STRONG,
            )
        }
    }

    private fun applyProfile(selected: String) {
        val result = ControlContract.applyQualityProfile(sessionId, selected)
        if (result.errorCode != null) {
            toast(result.message)
            profile = "auto"
        } else {
            profile = selected
            toast(result.message)
        }
        refreshStatus()
        RdcLog.info("Session", "profile_applied", "profile=$profile ok=${result.ok}")
    }

    private fun qualityNote(item: String): String = when (item) {
        "auto" -> getString(R.string.session_quality_auto_note)
        "4K60" -> getString(R.string.session_quality_4k_note)
        "1440p60" -> getString(R.string.session_quality_1440_note)
        "1080p60" -> getString(R.string.session_quality_1080p60_note)
        else -> getString(R.string.session_quality_1080p30_note)
    }

    // ---------------------------------------------------------------- 文件传输示意

    private fun transferAvailable(): Boolean =
        !offlinePreview && sessionId.isNotBlank() && sessionId != PREVIEW_SESSION_ID

    private fun refreshTransfer() {
        val available = transferAvailable()
        val remaining = (100 - transferProgress) * TRANSFER_TICK_MS / (TRANSFER_STEP * 1000)
        for (views in transferCards) {
            views.root.alpha = if (available) 1f else 0.45f
            views.badge.text = getString(transferStateRes())
            views.state.text = getString(transferStateRes())
            views.pct.text = getString(R.string.session_transfer_percent, transferProgress)
            views.progress.progress = transferProgress
            views.rate.text = getString(R.string.session_transfer_rate, "—")
            views.eta.text = getString(
                R.string.session_transfer_eta,
                if (available && (transferRunning || transferPaused)) {
                    getString(R.string.session_transfer_eta_seconds, remaining.coerceAtLeast(0))
                } else {
                    "—"
                },
            )
            views.file.text = getString(
                R.string.session_transfer_file,
                if (available) transferFileName else "—",
            )
            views.pause.text = getString(
                if (transferPaused) R.string.session_transfer_resume else R.string.session_transfer_pause,
            )
            views.pick.isEnabled = available && !transferRunning && !transferPaused
            views.pause.isEnabled = available && (transferRunning || transferPaused) && !transferCompleted
            views.cancel.isEnabled = available && (transferRunning || transferPaused) && !transferCompleted
            views.retry.isEnabled = available && (transferCancelled || transferCompleted)
            views.pick.alpha = if (views.pick.isEnabled) 1f else 0.4f
            views.pause.alpha = if (views.pause.isEnabled) 1f else 0.4f
            views.cancel.alpha = if (views.cancel.isEnabled) 1f else 0.4f
            views.retry.alpha = if (views.retry.isEnabled) 1f else 0.4f
        }
    }

    private fun transferStateRes(): Int = when {
        transferCancelled -> R.string.session_transfer_state_cancelled
        transferCompleted -> R.string.session_transfer_state_done
        transferPaused -> R.string.session_transfer_state_paused
        transferRunning -> R.string.session_transfer_state_running
        else -> R.string.session_transfer_state_waiting
    }

    private fun toggleTransferPause() {
        if (!transferAvailable() || transferCancelled || transferCompleted) {
            return
        }
        transferPaused = !transferPaused
        transferHandler.removeCallbacks(transferTick)
        if (!transferPaused) {
            transferRunning = true
            transferHandler.postDelayed(transferTick, TRANSFER_TICK_MS)
        }
        refreshTransfer()
        RdcLog.info("Session", "transfer_sim_pause", "paused=$transferPaused")
    }

    private fun cancelTransfer() {
        if (!transferAvailable()) {
            return
        }
        transferHandler.removeCallbacks(transferTick)
        transferRunning = false
        transferPaused = false
        transferCancelled = true
        transferCompleted = false
        refreshTransfer()
        RdcLog.info("Session", "transfer_sim_cancel", "progress=$transferProgress")
    }

    private fun retryTransfer() {
        if (!transferAvailable()) {
            return
        }
        transferProgress = 0
        transferRunning = true
        transferPaused = false
        transferCancelled = false
        transferCompleted = false
        refreshTransfer()
        transferHandler.removeCallbacks(transferTick)
        transferHandler.postDelayed(transferTick, TRANSFER_TICK_MS)
        RdcLog.info("Session", "transfer_sim_retry")
    }

    // ---------------------------------------------------------------- 剪贴板示意

    private fun refreshClipboard() {
        val available = transferAvailable()
        for (views in clipboardCards) {
            views.toggle.isChecked = clipboardSyncEnabled
            views.status.text = clipboardLastStatus
            views.push.isEnabled = available && clipboardSyncEnabled
            views.pull.isEnabled = available && clipboardSyncEnabled
            views.push.alpha = if (views.push.isEnabled) 1f else 0.4f
            views.pull.alpha = if (views.pull.isEnabled) 1f else 0.4f
        }
    }

    private fun onClipboardSyncChanged(enabled: Boolean) {
        clipboardSyncEnabled = enabled
        clipboardLastStatus = getString(
            if (enabled) R.string.session_clipboard_idle else R.string.session_clipboard_off,
        )
        refreshClipboard()
        toast(
            getString(
                if (enabled) R.string.session_clipboard_enabled_toast
                else R.string.session_clipboard_disabled_toast,
            ),
        )
        RdcLog.info("Session", "clipboard_switch", "enabled=$enabled")
    }

    private fun sendLocalClipboardText() {
        if (!clipboardSyncEnabled || !transferAvailable()) {
            return
        }
        val size = getString(R.string.session_clipboard_demo_text).toByteArray().size
        clipboardLastStatus = getString(R.string.session_clipboard_status_local, size)
        refreshClipboard()
        toast(getString(R.string.session_clipboard_sent_toast))
        RdcLog.info("Session", "clipboard_push_sim", "bytes=$size")
    }

    private fun pullRemoteClipboardText() {
        if (!clipboardSyncEnabled || !transferAvailable()) {
            return
        }
        clipboardLastStatus = getString(R.string.session_clipboard_status_remote, REMOTE_CLIPBOARD_BYTES)
        refreshClipboard()
        toast(getString(R.string.session_clipboard_pulled_toast))
        RdcLog.info("Session", "clipboard_pull_sim", "bytes=$REMOTE_CLIPBOARD_BYTES")
    }

    // ---------------------------------------------------------------- 输入上行

    /** 触摸转指针事件: 归一化到远端画面内容区并钳制边界; 全屏轻触唤出浮条. */
    private fun onRemoteTouch(event: MotionEvent): Boolean {
        // 双指缩放与双击还原由手势识别器消费, 单指点击/拖动仍按鼠标事件下发
        scaleDetector.onTouchEvent(event)
        if (event.pointerCount == 1) {
            doubleTapDetector.onTouchEvent(event)
        }
        if (event.pointerCount >= 2) {
            return handleMultiTouch(event)
        }
        if (multiTouchActive) {
            // 双指抬起过程中残留的单指不当作点击
            if (event.actionMasked == MotionEvent.ACTION_UP ||
                event.actionMasked == MotionEvent.ACTION_CANCEL
            ) {
                multiTouchActive = false
            }
            return true
        }
        if (event.actionMasked == MotionEvent.ACTION_UP) {
            showFloatingBar()
            // 落点日志: 真机自动化用坐标点击时据此核对归一化位置
            val state = currentViewport()
            val mapped = ViewportMath.mapToNormalized(state, event.x, event.y)
            if (mapped != null) {
                val origin = IntArray(2)
                videoView.getLocationOnScreen(origin)
                RdcLog.info(
                    "Session",
                    "remote_tap",
                    "view=${event.x.toInt()},${event.y.toInt()} " +
                        "origin=${origin[0]},${origin[1]} " +
                        "normalized=${String.format(Locale.US, "%.4f", mapped.first)}," +
                        String.format(Locale.US, "%.4f", mapped.second) +
                        " content=${ViewportMath.contentLeft(state).toInt()}," +
                        "${ViewportMath.contentTop(state).toInt()}," +
                        "${ViewportMath.contentWidth(state).toInt()}x" +
                        "${ViewportMath.contentHeight(state).toInt()}",
                )
            }
        }
        val normalized = mapToRemote(event.x, event.y) ?: return true
        val action = when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> InputEvent.Pointer.Action.DOWN
            MotionEvent.ACTION_MOVE -> InputEvent.Pointer.Action.MOVE
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> InputEvent.Pointer.Action.UP
            else -> return true
        }
        val (x, y) = normalized
        ControlContract.injectInput(InputEvent.Pointer(action, x, y))
        return true
    }

    /** 双指拖动平移视口: 只在全屏原始像素下生效; 单指始终留给远端鼠标操作. */
    private fun handleMultiTouch(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_POINTER_DOWN -> {
                multiTouchActive = true
                lastMultiX = averageX(event)
                lastMultiY = averageY(event)
            }
            MotionEvent.ACTION_MOVE -> {
                if (!multiTouchActive) {
                    return true
                }
                val x = averageX(event)
                val y = averageY(event)
                val deltaX = x - lastMultiX
                val deltaY = y - lastMultiY
                lastMultiX = x
                lastMultiY = y
                if (deltaX != 0f || deltaY != 0f) {
                    panViewport(deltaX, deltaY)
                }
            }
            MotionEvent.ACTION_POINTER_UP -> {
                // 剩余指针不足两根就结束双指平移, 避免后续单指拖动继续带动视口
                multiTouchActive = event.pointerCount - 1 >= 2
            }
            MotionEvent.ACTION_CANCEL -> multiTouchActive = false
        }
        return true
    }

    /** 平移视口并按边界夹取; 只移动显示, 不改远端画面内容. */
    private fun panViewport(deltaX: Float, deltaY: Float) {
        val before = currentViewport()
        val moved = ViewportMath.panBy(before, deltaX, deltaY)
        if (moved.panX == before.panX && moved.panY == before.panY) {
            return
        }
        viewportPanX = moved.panX
        viewportPanY = moved.panY
        applyViewportMatrix()
        RdcLog.info(
            "Session",
            "viewport_pan",
            "dx=${deltaX.toInt()} dy=${deltaY.toInt()} " +
                "pan=${ViewportMath.contentLeft(moved).toInt()},${ViewportMath.contentTop(moved).toInt()}",
        )
    }

    /** 浮条「还原画面」/双击: 回默认档(适应屏幕)并居中. */
    private fun resetViewport() {
        val target = ViewportMath.reset(currentViewport())
        applyViewport(target)
        showFloatingBar()
        RdcLog.info(
            "Session",
            "viewport_reset",
            "mode=${if (fullscreen) "fullscreen" else "window"} " +
                "scale=${String.format(Locale.US, "%.4f", target.scale)}",
        )
    }

    /** 浮条「回到左上角」: 仅画面超出屏幕时可用. */
    private fun moveViewportToTopLeft() {
        val state = currentViewport()
        if (!ViewportMath.canPanToTopLeft(state)) {
            return
        }
        val moved = ViewportMath.topLeft(state)
        applyViewport(moved)
        showFloatingBar()
        RdcLog.info(
            "Session",
            "viewport_top_left",
            "offset=${ViewportMath.contentLeft(moved).toInt()},${ViewportMath.contentTop(moved).toInt()}",
        )
    }

    private fun averageX(event: MotionEvent): Float {
        var total = 0f
        for (index in 0 until event.pointerCount) {
            total += event.getX(index)
        }
        return total / event.pointerCount.coerceAtLeast(1)
    }

    private fun averageY(event: MotionEvent): Float {
        var total = 0f
        for (index in 0 until event.pointerCount) {
            total += event.getY(index)
        }
        return total / event.pointerCount.coerceAtLeast(1)
    }

    /** 等比铺满后把触摸换算为远端归一化坐标; 画面外触摸钳制到最近的画面边缘. */
    private fun mapToRemote(touchX: Float, touchY: Float): Pair<Float, Float>? =
        ViewportMath.mapToNormalized(currentViewport(), touchX, touchY)

    private fun forwardImeText(text: String) {
        for (char in text) {
            ControlContract.injectInput(InputEvent.Key(InputEvent.Key.Action.DOWN, "char:$char"))
            ControlContract.injectInput(InputEvent.Key(InputEvent.Key.Action.UP, "char:$char"))
        }
    }

    override fun onKeyDown(keyCode: Int, event: android.view.KeyEvent): Boolean {
        if (keyCode == android.view.KeyEvent.KEYCODE_BACK) {
            return super.onKeyDown(keyCode, event)
        }
        ControlContract.injectInput(
            InputEvent.Key(InputEvent.Key.Action.DOWN, android.view.KeyEvent.keyCodeToString(keyCode)),
        )
        return true
    }

    override fun onKeyUp(keyCode: Int, event: android.view.KeyEvent): Boolean {
        if (keyCode == android.view.KeyEvent.KEYCODE_BACK) {
            return super.onKeyUp(keyCode, event)
        }
        ControlContract.injectInput(
            InputEvent.Key(InputEvent.Key.Action.UP, android.view.KeyEvent.keyCodeToString(keyCode)),
        )
        return true
    }

    private fun toggleKeyboard() {
        val manager = getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
        if (manager.isAcceptingText) {
            manager.hideSoftInputFromWindow(imeSink.windowToken, 0)
        } else {
            imeSink.requestFocus()
            manager.showSoftInput(imeSink, InputMethodManager.SHOW_IMPLICIT)
        }
        RdcLog.info("Session", "keyboard_toggle", "accepting=${manager.isAcceptingText}")
    }

    // ---------------------------------------------------------------- Surface 与生命周期

    /** TextureView 接管 SurfaceTexture: 尺寸变化时热切换输出面并重算铺满矩阵. */
    private val textureListener = object : TextureView.SurfaceTextureListener {
        override fun onSurfaceTextureAvailable(texture: SurfaceTexture, width: Int, height: Int) {
            attachVideoSurface(texture, width, height)
        }

        override fun onSurfaceTextureSizeChanged(texture: SurfaceTexture, width: Int, height: Int) {
            attachVideoSurface(texture, width, height)
        }

        override fun onSurfaceTextureDestroyed(texture: SurfaceTexture): Boolean {
            renderer.detachSurface()
            videoSurface?.release()
            videoSurface = null
            attachedTexture = null
            RdcLog.info("Session", "texture_destroyed", "session_kept=true")
            return true
        }

        override fun onSurfaceTextureUpdated(texture: SurfaceTexture) = Unit
    }

    private fun attachVideoSurface(texture: SurfaceTexture, width: Int, height: Int) {
        if (attachedTexture !== texture || videoSurface?.isValid != true) {
            videoSurface?.release()
            videoSurface = Surface(texture)
            attachedTexture = texture
        }
        renderer.attachSurface(videoSurface!!, width, height)
        applyViewportMatrix()
        // 旋转或尺寸变化时 TextureView 会重置变换, 布局完成后再写一次
        videoView.post { applyViewportMatrix() }
        RdcLog.info("Session", "texture_attached", "size=${width}x$height")
    }

    private fun rotationDegrees(): Int {
        val display = (getSystemService(Context.DISPLAY_SERVICE) as android.hardware.display.DisplayManager)
            .getDisplay(android.view.Display.DEFAULT_DISPLAY)
        return when (display?.rotation) {
            android.view.Surface.ROTATION_90 -> 90
            android.view.Surface.ROTATION_180 -> 180
            android.view.Surface.ROTATION_270 -> 270
            else -> 0
        }
    }

    private fun finishSession() {
        // 退出全屏回到窗口态: 先恢复系统栏, 再结束会话
        setSystemBarsHidden(false)
        val result = ControlContract.stopSession(sessionId)
        RdcLog.info("Session", "session_stopped", "session=$sessionId message=${result.message}")
        finish()
    }

    private fun shortPeer(): String {
        if (peerId.isBlank()) {
            return getString(R.string.session_peer_unknown)
        }
        return if (peerId.length > 12) {
            peerId.take(6) + "…" + peerId.takeLast(5)
        } else {
            peerId
        }
    }

    private fun matchParent() = FrameLayout.LayoutParams(
        FrameLayout.LayoutParams.MATCH_PARENT,
        FrameLayout.LayoutParams.MATCH_PARENT,
    )

    private fun toast(text: String) {
        android.widget.Toast.makeText(this, text, android.widget.Toast.LENGTH_SHORT).show()
    }

    /** 传输面板引用的控件集合; 竖屏卡片与横屏抽屉各有一份, 状态统一刷新. */
    private data class TransferViews(
        val root: LinearLayout,
        val badge: TextView,
        val state: TextView,
        val pct: TextView,
        val progress: ProgressBar,
        val rate: TextView,
        val eta: TextView,
        val file: TextView,
        val pick: TextView,
        val pause: TextView,
        val cancel: TextView,
        val retry: TextView,
    )

    /** 剪贴板面板引用的控件集合. */
    private data class ClipboardViews(
        val root: LinearLayout,
        val toggle: SwitchCompat,
        val status: TextView,
        val push: TextView,
        val pull: TextView,
    )

    /** 固定的 16:9 远端画面容器; 横屏时关闭比例约束铺满屏幕. */
    private class AspectFrameLayout(context: Context) : FrameLayout(context) {
        var aspectEnabled = true
        var ratio = 9f / 16f

        override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
            if (!aspectEnabled) {
                super.onMeasure(widthMeasureSpec, heightMeasureSpec)
                return
            }
            val width = MeasureSpec.getSize(widthMeasureSpec)
            val height = (width * ratio).toInt()
            super.onMeasure(
                MeasureSpec.makeMeasureSpec(width, MeasureSpec.EXACTLY),
                MeasureSpec.makeMeasureSpec(height, MeasureSpec.EXACTLY),
            )
        }
    }

    companion object {
        const val EXTRA_SESSION_ID = "session_id"
        const val EXTRA_PROFILE = "profile"
        const val EXTRA_OFFLINE_PREVIEW = "offline_preview"
        const val EXTRA_PEER_ID = "peer_id"
        const val EXTRA_REMOTE_WIDTH = "remote_width"
        const val EXTRA_REMOTE_HEIGHT = "remote_height"

        private const val SHEET_QUALITY = "quality"
        private const val SHEET_TRANSFER = "transfer"
        private const val PREVIEW_SESSION_ID = "preview"

        private const val TRANSFER_TICK_MS = 400L
        private const val TRANSFER_STEP = 4
        private const val REMOTE_CLIPBOARD_BYTES = 128

        /** 浮条出现后 3 秒自动淡出. */
        private const val FLOATING_BAR_MS = 3_000L
        private const val FADE_IN_MS = 150L
        private const val FADE_OUT_MS = 250L

        /** 覆盖层描边: 白色 12% 透明度. */
        private val OVERLAY_BORDER = RdcColor.MEDIA_LINE
    }
}
