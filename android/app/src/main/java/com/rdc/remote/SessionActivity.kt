// ==========================================================================================
// 文件        : SessionActivity.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 控制端会话窗: 远端画面容器、悬浮手势条、状态条、横竖屏切换与底部抽屉
// 架构定位    : Android 控制端界面层, 只呈现画面与转发输入, 不承载会话业务逻辑
// 核心功能    : SurfaceView 承载远端画面; 触摸/键盘转契约输入事件; 旋转不重建界面只迁移布局;
//               竖屏为画面 + 信息卡 + 操作区, 横屏为全屏画面 + 悬浮手势条 + 底部抽屉
// 职责边界    : 不建连、不解码协议、不决策降级; 所有命令走 ControlContract; 不读写系统剪贴板
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34)
// 依赖关系    : RemoteRenderer、ControlContract、InputEvent、RdcTheme、RdcLog
// ==========================================================================================

package com.rdc.remote

import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.ActivityInfo
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.TextWatcher
import android.view.Gravity
import android.view.MotionEvent
import android.view.SurfaceHolder
import android.view.SurfaceView
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
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SwitchCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat

class SessionActivity : AppCompatActivity() {

    private lateinit var root: FrameLayout
    private lateinit var content: LinearLayout
    private lateinit var remoteBox: AspectFrameLayout
    private lateinit var surfaceView: SurfaceView
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

    private var remoteWidth = 1920
    private var remoteHeight = 1080
    private var landscape = false
    private var immersive = false
    private var sheetOpen: String? = null

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

        sessionId = intent.getStringExtra(EXTRA_SESSION_ID) ?: ""
        profile = intent.getStringExtra(EXTRA_PROFILE) ?: "auto"
        offlinePreview = intent.getBooleanExtra(EXTRA_OFFLINE_PREVIEW, false)
        peerId = intent.getStringExtra(EXTRA_PEER_ID) ?: ""
        remoteWidth = intent.getIntExtra(EXTRA_REMOTE_WIDTH, 1920)
        remoteHeight = intent.getIntExtra(EXTRA_REMOTE_HEIGHT, 1080)
        landscape = resources.configuration.orientation == Configuration.ORIENTATION_LANDSCAPE
        clipboardSyncEnabled = AppPrefs.clipboardSync(this)

        clipboardLastStatus = getString(R.string.session_clipboard_idle)
        buildViews()
        applyOrientationLayout()
        applySheet()
        refreshTransfer()
        refreshClipboard()
        refreshStatus()

        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    if (landscape) {
                        backToPortrait()
                    } else {
                        finishSession()
                    }
                }
            },
        )

        RdcLog.info(
            "Session",
            "page_ready",
            "session=$sessionId profile=$profile offline=$offlinePreview " +
                "orientation=${if (landscape) "landscape" else "portrait"} wired=${ControlContract.WIRED}",
        )
    }

    override fun onResume() {
        super.onResume()
        ticker.post(metricsTick)
        if (transferRunning && !transferPaused && transferAvailable()) {
            transferHandler.postDelayed(transferTick, TRANSFER_TICK_MS)
        }
    }

    override fun onPause() {
        ticker.removeCallbacks(metricsTick)
        transferHandler.removeCallbacks(transferTick)
        super.onPause()
    }

    override fun onDestroy() {
        ticker.removeCallbacks(metricsTick)
        transferHandler.removeCallbacks(transferTick)
        renderer.release()
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
        surfaceView = SurfaceView(this).apply {
            holder.addCallback(surfaceCallback)
            // 读屏把远端画面识别为一块可触摸区域, 而不是空白
            contentDescription = getString(R.string.a11y_remote_surface)
            setOnTouchListener { view, event ->
                if (event.actionMasked == MotionEvent.ACTION_UP) {
                    view.performClick()
                }
                onRemoteTouch(event)
            }
        }
        placeholder = labelView(getString(R.string.session_placeholder), 12f, RdcColor.FAINT, Gravity.CENTER)

        peerChip = TextView(this).apply {
            text = getString(R.string.session_peer_format, shortPeer())
            textSize = 9.5f
            setTextColor(Color.parseColor("#DBE4F0"))
            setPadding(dp(8), dp(5), dp(8), dp(5))
            roundedBackground(RdcColor.OVERLAY, 7, OVERLAY_BORDER)
            typeface = Typeface.MONOSPACE
        }
        topMetrics = TextView(this).apply {
            text = getString(R.string.session_metrics_placeholder, profile)
            textSize = 9f
            setTextColor(Color.parseColor("#C7D3E2"))
            setPadding(dp(8), dp(5), dp(8), dp(5))
            roundedBackground(RdcColor.OVERLAY, 6, OVERLAY_BORDER)
            typeface = Typeface.MONOSPACE
            visibility = View.GONE
        }
        metricPill = TextView(this).apply {
            text = getString(R.string.session_metrics_placeholder, profile)
            textSize = 9f
            setTextColor(Color.parseColor("#C7D3E2"))
            setPadding(dp(8), dp(4), dp(8), dp(4))
            roundedBackground(RdcColor.OVERLAY, 6, OVERLAY_BORDER)
            typeface = Typeface.MONOSPACE
        }
        remoteLabel = TextView(this).apply {
            text = getString(R.string.session_remote_label)
            textSize = 8.5f
            setTextColor(Color.parseColor("#8FA0B4"))
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

        remoteBox.addView(surfaceView, matchParent())
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
            textSize = 9f
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
            actionButton(getString(R.string.session_landscape_fullscreen), RdcColor.ACCENT, Color.WHITE) {
                enterLandscape()
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
        return grid
    }

    private fun actionButton(text: String, background: Int, textColor: Int, onClick: () -> Unit): TextView =
        actionView(text, 11.5f, textColor, background, 10, RdcColor.LINE_STRONG, 39).apply {
            layoutParams = null
            setOnClickListener { onClick() }
        }

    private fun actionParams(startMargin: Int) = LinearLayout.LayoutParams(
        0,
        dp(39),
        1f,
    ).apply { leftMargin = dp(startMargin) }

    private fun roundButton(text: String, onClick: () -> Unit): TextView =
        TextView(this).apply {
            this.text = text
            textSize = 15f
            setTextColor(Color.parseColor("#DBE4F0"))
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
            textSize = 12f
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
            text = item
            textSize = 10f
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
            textSize = 9f
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
            layoutParams = null
            setOnClickListener { onClick() }
        }

    private fun smallParams(startMargin: Int) = LinearLayout.LayoutParams(
        0,
        dp(30),
        1f,
    ).apply { leftMargin = dp(startMargin) }

    // ---------------------------------------------------------------- 横屏手势条与抽屉

    private fun buildGestureBar(): LinearLayout {
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(6), dp(4), dp(6), dp(4))
            roundedBackground(Color.parseColor("#D91A1D22"), 999, RdcColor.LINE_STRONG)
        }
        bar.addView(gestureItem(getString(R.string.session_back)) { backToPortrait() })
        bar.addView(gestureItem(getString(R.string.session_fullscreen)) { toggleImmersive() })
        bar.addView(divider())
        bar.addView(gestureItem(getString(R.string.session_keyboard)) { toggleKeyboard() })
        bar.addView(gestureItem(getString(R.string.session_quality)) { openSheet(SHEET_QUALITY) })
        bar.addView(divider())
        bar.addView(gestureItem(getString(R.string.session_transfer)) { openSheet(SHEET_TRANSFER) })
        return bar
    }

    private fun gestureItem(text: String, onClick: () -> Unit): TextView =
        TextView(this).apply {
            this.text = text
            textSize = 11.5f
            setTextColor(RdcColor.TEXT)
            gravity = Gravity.CENTER
            setPadding(dp(12), dp(9), dp(12), dp(9))
            isClickable = true
            isFocusable = true
            setOnClickListener { onClick() }
        }

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
                    getString(R.string.session_quality_option, item, qualityNote(item)),
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
            textSize = 9f
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
            roundedBackground(Color.parseColor("#F21A1D22"), 16, RdcColor.LINE_STRONG)
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

    /** 按当前方向重排: 竖屏为画面 + 信息卡 + 操作区, 横屏为全屏画面 + 悬浮手势条. */
    private fun applyOrientationLayout() {
        if (landscape) {
            remoteBox.aspectEnabled = false
            remoteBox.ratio = 9f / 16f
            remoteBox.layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1f,
            )
            portraitBody.visibility = View.GONE
            gestureBar.visibility = View.VISIBLE
            topMetrics.visibility = View.VISIBLE
            remoteBox.roundedBackground(RdcColor.SURFACE, 0)
        } else {
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
            gestureBar.visibility = View.GONE
            topMetrics.visibility = View.GONE
            remoteBox.roundedBackground(RdcColor.SURFACE, 16)
        }
        // 旋转会重建 Surface, 由 surfaceChanged 重新挂载; 方向记录同步给渲染器
        renderer.updateOrientation(rotationDegrees(), remoteWidth, remoteHeight)
        applySheet()
        refreshStatus()
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        landscape = newConfig.orientation == Configuration.ORIENTATION_LANDSCAPE
        if (!landscape) {
            immersive = false
        }
        applyOrientationLayout()
        RdcLog.info(
            "Session",
            "orientation_changed",
            "orientation=${if (landscape) "landscape" else "portrait"} " +
                "rotation=${rotationDegrees()} surface=${surfaceView.width}x${surfaceView.height} " +
                "session=$sessionId profile=$profile state_kept=true",
        )
    }

    private fun enterLandscape() {
        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE
        RdcLog.info("Session", "enter_landscape", "session=$sessionId")
    }

    /** 横屏返回竖屏会话: 不结束会话, 只切回竖屏布局. */
    @SuppressLint("SourceLockedOrientationActivity")
    private fun backToPortrait() {
        closeSheet()
        // 设计稿要求「返回」回到竖屏会话, 这里短暂锁定竖屏; 不做多窗口适配
        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
        RdcLog.info("Session", "back_to_portrait", "session=$sessionId")
    }

    private fun onHeaderBack() {
        if (landscape) {
            backToPortrait()
        } else {
            finishSession()
        }
    }

    private fun openSheet(which: String) {
        sheetOpen = if (sheetOpen == which) null else which
        if (sheetOpen != null && immersive) {
            immersive = false
            WindowInsetsControllerCompat(window, root).show(WindowInsetsCompat.Type.systemBars())
        }
        applySheet()
        RdcLog.info("Session", "sheet_toggle", "sheet=${sheetOpen ?: "none"}")
    }

    private fun closeSheet() {
        sheetOpen = null
        applySheet()
    }

    private fun applySheet() {
        val visible = landscape && sheetOpen != null
        qualitySheet.visibility = if (visible && sheetOpen == SHEET_QUALITY) View.VISIBLE else View.GONE
        transferSheet.visibility = if (visible && sheetOpen == SHEET_TRANSFER) View.VISIBLE else View.GONE
        gestureBar.visibility = if (landscape) View.VISIBLE else View.GONE
        topRow.visibility = if (immersive) View.GONE else View.VISIBLE
        metricPill.visibility = if (immersive) View.GONE else View.VISIBLE
        remoteLabel.visibility = if (immersive) View.GONE else View.VISIBLE
    }

    private fun toggleImmersive() {
        immersive = !immersive
        val controller = WindowInsetsControllerCompat(window, root)
        if (immersive) {
            controller.hide(WindowInsetsCompat.Type.systemBars())
            controller.systemBarsBehavior =
                WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            closeSheet()
        } else {
            controller.show(WindowInsetsCompat.Type.systemBars())
        }
        applySheet()
        RdcLog.info("Session", "immersive", "enabled=$immersive")
    }

    // ---------------------------------------------------------------- 状态刷新

    private fun refreshStatus() {
        val status = ControlContract.getSessionStatus(sessionId)
        val metrics = ControlContract.getSessionMetrics(sessionId)
        val connected = status.ok && !offlinePreview

        stateLine.text = if (connected) {
            getString(R.string.session_state_online, status.data["transport_kind"]?.toString() ?: "—")
        } else {
            getString(R.string.session_state_offline)
        }
        stateBadge.text = getString(if (connected) R.string.session_badge_online else R.string.session_badge_offline)
        stateBadge.setTextColor(if (connected) RdcColor.OK else RdcColor.WARN)

        val fps = metricValue(metrics, "fps")
        val p50 = metricValue(metrics, "p50_ms")
        val p95 = metricValue(metrics, "p95_ms")
        val bitrate = metricValue(metrics, "bitrate_kbps")
        statFps.text = fps
        statLatency.text = if (p50 == "—" && p95 == "—") "—" else "$p50 / $p95"
        statBitrate.text = if (bitrate == "—") "—" else "$bitrate kbps"
        statProfile.text = profile

        val pill = getString(
            R.string.session_metrics_value,
            if (p50 == "—") "—" else p50,
            if (fps == "—") "—" else fps,
            profile,
        )
        metricPill.text = pill
        topMetrics.text = pill
        refreshChipSelection()
        placeholder.visibility = if (connected) View.GONE else View.VISIBLE
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

    /** 触摸转指针事件: 归一化到远端画面内容区, 黑边不产生事件. */
    private fun onRemoteTouch(event: MotionEvent): Boolean {
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

    /** 等比适配远端画面, 返回内容区归一化坐标; 落在黑边上返回 null. */
    private fun mapToRemote(touchX: Float, touchY: Float): Pair<Float, Float>? {
        val viewWidth = surfaceView.width.toFloat()
        val viewHeight = surfaceView.height.toFloat()
        if (viewWidth <= 0f || viewHeight <= 0f) {
            return null
        }
        val rotated = rotationDegrees() % 180 != 0
        val sourceWidth = if (rotated) remoteHeight.toFloat() else remoteWidth.toFloat()
        val sourceHeight = if (rotated) remoteWidth.toFloat() else remoteHeight.toFloat()
        val scale = minOf(viewWidth / sourceWidth, viewHeight / sourceHeight)
        val contentWidth = sourceWidth * scale
        val contentHeight = sourceHeight * scale
        val left = (viewWidth - contentWidth) / 2f
        val top = (viewHeight - contentHeight) / 2f
        if (touchX < left || touchX > left + contentWidth || touchY < top || touchY > top + contentHeight) {
            return null
        }
        val x = ((touchX - left) / contentWidth).coerceIn(0f, 1f)
        val y = ((touchY - top) / contentHeight).coerceIn(0f, 1f)
        return x to y
    }

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

    private val surfaceCallback = object : SurfaceHolder.Callback {
        override fun surfaceCreated(holder: SurfaceHolder) {
            renderer.attachSurface(holder.surface, surfaceView.width, surfaceView.height)
            RdcLog.info("Session", "surface_created", "size=${surfaceView.width}x${surfaceView.height}")
        }

        override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int) {
            renderer.attachSurface(holder.surface, width, height)
            RdcLog.info("Session", "surface_changed", "size=${width}x$height session_kept=true")
        }

        override fun surfaceDestroyed(holder: SurfaceHolder) {
            renderer.detachSurface()
            RdcLog.info("Session", "surface_destroyed", "session_kept=true")
        }
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

        /** 覆盖层描边: 白色 12% 透明度. */
        private val OVERLAY_BORDER = Color.parseColor("#1FFFFFFF")
    }
}
