// ==========================================================================================
// 文件        : MainActivity.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 控制端主壳: 4 格底部导航 (连接 / 设备 / 消息 / 我的)
// 架构定位    : Android 界面层, 只收集参数、调用契约命令、呈现结果
// 核心功能    : 常驻底部导航; 4 页真实可达; 会话窗全屏时导航不出现; 返回保持原 tab 与滚动位置
// 职责边界    : 不采集、不编码、不连网、不解码; 不做登录; 不虚构设备与会话数据
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34)
// 依赖关系    : ControlContract、AppPrefs、RdcTheme、RdcLog、SessionActivity 等
// ==========================================================================================

package com.rdc.remote

import android.app.Dialog
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.ColorDrawable
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.InputFilter
import android.text.InputType
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.Window
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SwitchCompat
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : AppCompatActivity() {

    private lateinit var contentHost: FrameLayout
    private lateinit var pages: List<View>
    private lateinit var tabCells: List<TabCell>

    private lateinit var peerInput: EditText
    private lateinit var codeInput: EditText
    private lateinit var addressInput: EditText
    private lateinit var connectStatus: TextView

    /** 连接失败后出现的「复制失败原因」, 空闲与成功时隐藏. */
    private var connectReasonCopy: TextView? = null
    private lateinit var connectButton: TextView
    private lateinit var recentList: LinearLayout

    private lateinit var messageList: LinearLayout
    private lateinit var messageEmpty: LinearLayout
    private lateinit var messageChips: LinearLayout
    private lateinit var messageSummary: TextView
    private lateinit var messageGate: LinearLayout
    private var messageFilter = FILTER_ALL

    /** A4: 服务端消息缓存; 未读与已读水位只取接口返回值. */
    private var remoteMessages: List<AccountMessage> = emptyList()
    private var messageUnread = 0
    private var messageTotal = 0
    private var messageReadUntil = ""
    private var messageLoading = false
    private var messageError: String? = null
    private var messagesLoadedFor: String? = null

    private lateinit var devicesSearch: EditText
    private lateinit var devicesOnlineGroup: LinearLayout
    private lateinit var devicesOfflineGroup: LinearLayout
    private lateinit var devicesEmptyHost: LinearLayout
    private lateinit var devicesChips: LinearLayout
    private var deviceQuery = ""
    private var deviceFilter = FILTER_DEVICE_ALL

    /** 设备页账号卡文字, 登录态变化时就地更新, 不重建整页. */
    private var devicesAccountTitle: TextView? = null
    private var devicesAccountNote: TextView? = null
    private var devicesAccountAction: TextView? = null
    private var devicesAccountCard: LinearLayout? = null

    /** 连接页受信设备区块; 与设备页共用同一份账号中心设备列表. */
    private var connectTrustedHost: LinearLayout? = null
    private var connectTrustedNote: TextView? = null

    /** 账号中心设备列表缓存; 只由 RdcAccountService 填充, 界面不自行拼装. */
    private var remoteDeviceItems: List<AccountDeviceInfo> = emptyList()
    private var remoteDevicesLoadedFor: String? = null
    private var remoteDevicesLoading = false
    private var remoteDevicesError: String? = null

    /** 「我的」页统计与消息入口角标, 数据来自账号中心设备列表. */
    private var profileStatBound: TextView? = null
    private var profileStatTrusted: TextView? = null
    private var profileStatOnline: TextView? = null
    private var profileUnreadBadge: TextView? = null

    /** A10: 设备页可见且应用在前台时每 15 秒刷新一次在线状态. */
    private var foreground = false
    private val devicesPoll = Handler(Looper.getMainLooper())
    private val devicesPollTick = object : Runnable {
        override fun run() {
            if (!foreground || currentTab != TAB_DEVICES) {
                return
            }
            loadRemoteDevices(force = true, quiet = true)
            devicesPoll.postDelayed(this, DEVICES_POLL_MS)
        }
    }

    /** 构建「我的」页时的账号签名, 用于返回页面后判断是否需要重建. */
    private var profileAccountSignature = GUEST_ACCOUNT

    private var currentTab = TAB_CONNECT

    /** 诊断导出走系统文件选择器, 只写脱敏字段. */
    private val exportLauncher = registerForActivityResult(
        ActivityResultContracts.CreateDocument("text/plain"),
    ) { uri: Uri? ->
        if (uri == null) {
            RdcLog.info("Profile", "diagnostics_cancelled")
            return@registerForActivityResult
        }
        writeDiagnostics(uri)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 深色为默认; 切换后重建 Activity 以整体换色
        RdcColor.dark = AppPrefs.darkTheme(this)
        currentTab = savedInstanceState?.getInt(KEY_TAB, TAB_CONNECT) ?: TAB_CONNECT

        contentHost = FrameLayout(this).apply { setBackgroundColor(RdcColor.BG) }
        pages = listOf(
            buildConnectPage(),
            buildDevicesPage(),
            buildMessagesPage(),
            buildProfilePage(),
        )
        for (page in pages) {
            contentHost.addView(
                page,
                FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.MATCH_PARENT,
                    FrameLayout.LayoutParams.MATCH_PARENT,
                ),
            )
            page.visibility = View.GONE
        }

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(RdcColor.BG)
        }
        root.addView(contentHost, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        root.addView(
            View(this).apply { setBackgroundColor(RdcColor.LINE) },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1)),
        )
        val nav = buildBottomNav()
        root.addView(nav, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, RdcMetrics.NAV_TAB_BAR))
        setContentView(root)

        selectTab(currentTab, log = false)
        refreshBadge()
        restoreScroll(savedInstanceState)

        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    if (currentTab != TAB_CONNECT) {
                        selectTab(TAB_CONNECT)
                    } else {
                        finish()
                    }
                }
            },
        )

        RdcLog.info(
            "Home",
            "page_ready",
            "role=control tabs=4 tab=$currentTab wired=${ControlContract.WIRED}",
        )
    }

    override fun onResume() {
        super.onResume()
        foreground = true
        refreshRecent()
        refreshMessages()
        refreshBadge()
        refreshAccountSurfaces()
        if (currentTab == TAB_DEVICES) {
            loadRemoteDevices(force = true)
            startDevicesPolling()
        } else {
            loadRemoteDevices()
        }
        if (currentTab == TAB_MESSAGES) {
            loadMessages()
        }
    }

    override fun onPause() {
        // A10: 退到后台立即停止在线状态轮询
        foreground = false
        stopDevicesPolling()
        super.onPause()
    }

    private fun startDevicesPolling() {
        devicesPoll.removeCallbacks(devicesPollTick)
        if (foreground && currentTab == TAB_DEVICES) {
            devicesPoll.postDelayed(devicesPollTick, DEVICES_POLL_MS)
        }
    }

    private fun stopDevicesPolling() {
        devicesPoll.removeCallbacks(devicesPollTick)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putInt(KEY_TAB, currentTab)
        // 进程被回收后重建时, 除 tab 外也恢复各页滚动位置
        for (index in pages.indices) {
            val offset = (pages[index] as? ScrollView)?.scrollY ?: 0
            outState.putInt(KEY_SCROLL + index, offset)
        }
        super.onSaveInstanceState(outState)
    }

    /** 恢复各页滚动位置; 旋转由 configChanges 兜住, 这里只处理真正的重建. */
    private fun restoreScroll(state: Bundle?) {
        if (state == null) {
            return
        }
        for (index in pages.indices) {
            val offset = state.getInt(KEY_SCROLL + index, 0)
            if (offset <= 0) {
                continue
            }
            val page = pages[index] as? ScrollView ?: continue
            page.post { page.scrollTo(0, offset) }
        }
    }

    // ---------------------------------------------------------------- 底部导航

    private fun buildBottomNav(): LinearLayout {
        val nav = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setBackgroundColor(RdcColor.PANEL_2)
        }
        val specs = listOf(
            Triple(TAB_CONNECT, R.string.tab_connect, R.drawable.ic_tab_connect),
            Triple(TAB_DEVICES, R.string.tab_devices, R.drawable.ic_tab_devices),
            Triple(TAB_MESSAGES, R.string.tab_messages, R.drawable.ic_tab_messages),
            Triple(TAB_PROFILE, R.string.tab_profile, R.drawable.ic_tab_profile),
        )
        tabCells = specs.map { (index, labelRes, iconRes) ->
            val cell = buildTabCell(index, getString(labelRes), iconRes)
            nav.addView(cell.root, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f))
            cell
        }
        return nav
    }

    private fun buildTabCell(index: Int, label: String, iconRes: Int): TabCell {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            isClickable = true
            isFocusable = true
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_YES
            contentDescription = label
            pressableBackground(Color.TRANSPARENT, RdcColor.PANEL_3, 12)
            setOnClickListener { selectTab(index) }
        }
        val iconWrap = FrameLayout(this).apply {
            layoutParams = LinearLayout.LayoutParams(dp(28), dp(24))
        }
        val icon = ImageView(this).apply {
            setImageResource(iconRes)
            setColorFilter(RdcColor.MUTED)
            // 装饰图标: 朗读交给父节点, 避免读屏重复播报
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            layoutParams = FrameLayout.LayoutParams(dp(22), dp(22), Gravity.CENTER)
        }
        val dot = View(this).apply {
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(RdcColor.DANGER)
                setStroke(dp(1), RdcColor.PANEL_2)
            }
            visibility = View.GONE
            // 未读状态由父节点 contentDescription 播报, 红点本身不单独朗读
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            layoutParams = FrameLayout.LayoutParams(dp(7), dp(7), Gravity.TOP or Gravity.END).apply {
                topMargin = dp(1)
                rightMargin = dp(1)
            }
        }
        iconWrap.addView(icon)
        iconWrap.addView(dot)
        val text = labelView(label, 10f, RdcColor.MUTED, Gravity.CENTER)
        root.addView(iconWrap)
        root.addView(text)
        return TabCell(root, icon, text, dot)
    }

    private fun selectTab(index: Int, log: Boolean = true) {
        currentTab = index
        for (i in pages.indices) {
            pages[i].visibility = if (i == index) View.VISIBLE else View.GONE
        }
        for (i in tabCells.indices) {
            val selected = i == index
            tabCells[i].icon.setColorFilter(if (selected) RdcColor.ACCENT else RdcColor.MUTED)
            tabCells[i].label.setTextColor(if (selected) RdcColor.ACCENT else RdcColor.MUTED)
            tabCells[i].label.typeface = if (selected) Typeface.DEFAULT_BOLD else Typeface.DEFAULT
            // 选中态写进无障碍描述, 读屏可直接播报"已选中"
            tabCells[i].root.isSelected = selected
            tabCells[i].root.contentDescription = describeTab(i, selected)
        }
        if (log) {
            RdcLog.info("Home", "tab_selected", "tab=$index")
        }
        // A10: 只有设备页可见时轮询; 离开设备页立即停止
        if (index == TAB_DEVICES) {
            loadRemoteDevices()
            startDevicesPolling()
        } else {
            stopDevicesPolling()
        }
        if (index == TAB_MESSAGES) {
            loadMessages()
        }
    }

    // ---------------------------------------------------------------- 连接页

    private fun buildConnectPage(): ScrollView {
        val column = pageColumn()
        column.addView(buildBrandRow())
        column.addView(
            TextView(this).apply {
                text = getString(R.string.home_title)
                textSize = RdcText.PAGE_TITLE
                setTextColor(RdcColor.TEXT)
                // 与原型 .p-head 一致: 标题与说明左对齐
                gravity = RdcAlign.PAGE_HEADER
                setPadding(0, dp(16), 0, dp(4))
            },
        )
        column.addView(
            note("输入设备码与连接码，或从受信设备直接发起。", 11.5f, RdcColor.MUTED)
                .apply { gravity = RdcAlign.PAGE_HEADER },
        )
        column.addView(buildConnectHero())
        column.addView(buildTrustedCard())

        val form = card()
        val formHead = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        formHead.addView(
            labelView("连接码连接", 12f, RdcColor.TEXT).apply {
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            },
        )
        formHead.addView(note("未受信设备", 9.5f, RdcColor.FAINT))
        form.addView(formHead)
        form.addView(note("电脑端打开会话后显示设备码与 6 位临时连接码；连接码每次不同。", 9.5f, RdcColor.FAINT))

        form.addView(fieldLabel("电脑地址"))
        addressInput = EditText(this).apply {
            hint = "电脑地址:端口；IPv6 用 [地址]:端口"
            setText(LanSession.DEFAULT_ENDPOINT)
            setTextColor(RdcColor.TEXT)
            setHintTextColor(RdcColor.FAINT)
            textSize = RdcText.BODY_STRONG
            typeface = Typeface.MONOSPACE
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            setSingleLine()
            setPadding(dp(12), 0, dp(12), 0)
            roundedBackground(RdcColor.INPUT, 11, RdcColor.LINE_STRONG)
        }
        form.addView(addressInput, fieldParams())

        form.addView(fieldLabel(getString(R.string.home_peer_label)))
        peerInput = EditText(this).apply {
            hint = getString(R.string.home_peer_hint)
            setTextColor(RdcColor.TEXT)
            setHintTextColor(RdcColor.FAINT)
            textSize = RdcText.BODY_STRONG
            typeface = Typeface.MONOSPACE
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            filters = arrayOf(InputFilter.LengthFilter(PEER_ID_MAX_LENGTH))
            setSingleLine()
        }
        form.addView(inputWithCopy(peerInput) { peerInput.text.toString() })

        form.addView(fieldLabel(getString(R.string.home_code_label)))
        codeInput = EditText(this).apply {
            hint = getString(R.string.home_code_hint)
            setTextColor(RdcColor.TEXT)
            setHintTextColor(RdcColor.FAINT)
            textSize = RdcText.BODY_STRONG
            typeface = Typeface.MONOSPACE
            inputType = InputType.TYPE_CLASS_NUMBER
            filters = arrayOf(InputFilter.LengthFilter(ACCESS_CODE_LENGTH))
            setSingleLine()
        }
        form.addView(inputWithCopy(codeInput) { codeInput.text.toString() })

        connectStatus = labelView(getString(R.string.home_status_idle), 11.5f, RdcColor.MUTED).apply {
            setPadding(0, dp(10), 0, 0)
            // 连接结果变化时让读屏主动播报
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        }
        form.addView(connectStatus)
        connectReasonCopy = link("复制失败原因") {
            val reason = connectStatus.text.toString()
            if (reason.isBlank()) {
                toast("暂无可复制内容")
            } else {
                copyToClipboard(reason, "失败原因")
            }
        }.apply { visibility = View.GONE }
        form.addView(connectReasonCopy)

        connectButton = actionView(
            getString(R.string.home_connect),
            13.5f,
            Color.WHITE,
            RdcColor.ACCENT,
            12,
            heightDp = 47,
            pressedBackground = RdcColor.ACCENT_PRESS,
        ).apply {
            setOnClickListener { connect() }
        }
        form.addView(
            connectButton,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(12) },
        )
        column.addView(form)

        column.addView(link(getString(R.string.home_perm_entry)) {
            startActivity(Intent(this, PermissionActivity::class.java))
            RdcLog.info("Home", "open_permissions")
        })

        column.addView(buildRecent())
        column.addView(link(getString(R.string.home_preview_entry)) { openPreview() })

        column.addView(
            note(getString(R.string.home_footnote), 9.5f, RdcColor.FAINT).apply {
                gravity = Gravity.CENTER
                setPadding(0, dp(18), 0, 0)
            },
        )
        return pageScroll(column)
    }

    private fun buildBrandRow(): LinearLayout {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        row.addView(
            TextView(this).apply {
                text = "R"
                textSize = RdcText.HEADLINE
                setTextColor(Color.WHITE)
                gravity = Gravity.CENTER
                background = GradientDrawable().apply {
                    shape = GradientDrawable.RECTANGLE
                    cornerRadius = dp(8).toFloat()
                    colors = intArrayOf(RdcColor.BRAND_FROM, RdcColor.BRAND_TO)
                    orientation = GradientDrawable.Orientation.TL_BR
                }
                layoutParams = LinearLayout.LayoutParams(dp(28), dp(28))
            },
        )

        val who = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(9), 0, 0, 0)
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        who.addView(labelView(getString(R.string.app_name), 12.5f, RdcColor.TEXT))
        who.addView(note(getString(R.string.brand_sub), 9.5f, RdcColor.FAINT))
        row.addView(who)

        row.addView(roleBadge())
        return row
    }

    private fun buildConnectHero(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(14), dp(14), dp(14), dp(14))
        roundedBackground(RdcColor.PANEL, 14, RdcColor.LINE)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(16) }
        addView(labelView("远程协助，从手机发起", 15f, RdcColor.TEXT))
        addView(
            note(
                "安卓端只做控制端：连接你已登录的桌面设备，不提供被控端与屏幕墙入口。",
                10f,
                RdcColor.MUTED,
            ).apply { setPadding(0, dp(6), 0, 0) },
        )
        val tags = LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(10), 0, 0)
        }
        tags.addView(tagChip("控制端", RdcColor.ACCENT_STRONG, RdcColor.ACCENT_SOFT))
        tags.addView(tagChip("端到端加密", RdcColor.MUTED, RdcColor.PANEL_3))
        tags.addView(tagChip("6 位连接码", RdcColor.MUTED, RdcColor.PANEL_3))
        addView(tags)
    }

    private fun tagChip(text: String, textColor: Int, background: Int): TextView = TextView(this).apply {
        this.text = text
        textSize = RdcText.MICRO
        setTextColor(textColor)
        setPadding(dp(8), dp(3), dp(8), dp(3))
        roundedBackground(background, 999, RdcColor.LINE)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { rightMargin = dp(6) }
    }

    /** 连接页受信设备: 只呈现账号中心返回且在线、非本机的受信设备. */
    private fun buildTrustedCard(): LinearLayout {
        val card = card()
        val head = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        head.addView(
            labelView("受信设备", 12f, RdcColor.TEXT).apply {
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            },
        )
        head.addView(
            labelView("刷新", 10.5f, RdcColor.ACCENT).apply {
                setPadding(dp(8), dp(4), 0, dp(4))
                isClickable = true
                isFocusable = true
                pressableText(this, RdcColor.ACCENT, RdcColor.ACCENT_STRONG)
                setOnClickListener {
                    loadRemoteDevices(force = true)
                    RdcLog.info("Connect", "trusted_refresh")
                }
            },
        )
        card.addView(head)
        connectTrustedNote = note("", 9.5f, RdcColor.FAINT)
        card.addView(connectTrustedNote)
        connectTrustedHost = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(6), 0, 0)
        }
        card.addView(connectTrustedHost)
        card.addView(
            note("受信免码连接待互控线接入后开放；当前统一使用 6 位连接码。", 9f, RdcColor.FAINT)
                .apply { setPadding(0, dp(8), 0, 0) },
        )
        refreshConnectTrusted()
        return card
    }

    private fun refreshConnectTrusted() {
        val host = connectTrustedHost ?: return
        host.removeAllViews()
        val noteView = connectTrustedNote ?: return
        val session = RdcAccountService.session(this)
        if (session?.remote != true) {
            noteView.visibility = View.GONE
            host.addView(
                stateBox(
                    R.drawable.ic_lucide_shield,
                    "登录后读取受信设备",
                    "未登录仍可使用下方连接码连接；受信设备一键连接需要先登录。",
                    "去登录",
                ) { openAccount() },
            )
            return
        }
        if (remoteDevicesLoading && remoteDeviceItems.isEmpty()) {
            noteView.visibility = View.VISIBLE
            noteView.gravity = RdcAlign.CARD_STATE_TEXT
            noteView.text = "正在读取受信设备…"
            return
        }
        if (remoteDevicesError != null && remoteDeviceItems.isEmpty()) {
            noteView.visibility = View.GONE
            host.addView(
                stateBox(
                    R.drawable.ic_lucide_info,
                    "受信设备读取失败",
                    remoteDevicesError.orEmpty(),
                    "重试",
                ) { loadRemoteDevices(force = true) },
            )
            return
        }
        val rows = remoteDeviceItems
            .filter { it.trusted && it.online && !it.current }
            .sortedByDescending { it.lastConnectedAt }
        if (rows.isEmpty()) {
            noteView.visibility = View.GONE
            host.addView(
                stateBox(
                    R.drawable.ic_tab_devices,
                    "还没有受信设备",
                    "在桌面端设备列表把目标设备设为受信，或直接输入设备码与连接码连接。",
                    "重新读取",
                ) { loadRemoteDevices(force = true) },
            )
            return
        }
        noteView.visibility = View.VISIBLE
        noteView.gravity = Gravity.START
        noteView.text = "共 ${rows.size} 台在线受信设备"
        for (item in rows) {
            host.addView(trustedDeviceRow(item))
        }
    }

    private fun trustedDeviceRow(item: AccountDeviceInfo): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(10), dp(9), dp(10), dp(9))
            pressableBackground(RdcColor.PANEL_2, RdcColor.PRESS, 11, RdcColor.LINE)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(6) }
            addView(iconTile(platformIcon(item.platform), 34, RdcColor.MUTED, RdcColor.PANEL_3))
            val texts = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(10), 0, dp(8), 0)
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            }
            texts.addView(labelView(item.displayName, 12f, RdcColor.TEXT))
            texts.addView(
                labelView(
                    "${shortDeviceId(item.deviceId)} · 上次 ${relativeDeviceTime(item.lastConnectedAt)}",
                    9.5f,
                    RdcColor.FAINT,
                ),
            )
            addView(texts)
            addView(
                actionView(
                    "连接",
                    10.5f,
                    RdcColor.ACCENT_STRONG,
                    RdcColor.ACCENT_SOFT,
                    9,
                    RdcColor.ACCENT,
                    32,
                    RdcColor.PANEL_3,
                ).apply {
                    setOnClickListener { useDeviceCode(item.deviceId) }
                    layoutParams = LinearLayout.LayoutParams(dp(64), dp(32))
                },
            )
        }

    /** 最近连接: 与受信设备 / 连接码连接同宽同内边距, 空态内容居中. */
    private fun buildRecent(): LinearLayout {
        val card = card()
        val head = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        head.addView(
            labelView(getString(R.string.home_recent), 12f, RdcColor.TEXT).apply {
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            },
        )
        head.addView(
            TextView(this@MainActivity).apply {
                text = "连接成功后会保存在本机"
                textSize = RdcText.TINY
                setTextColor(RdcColor.FAINT)
            },
        )
        card.addView(head)

        recentList = LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(9), 0, 0)
        }
        card.addView(recentList)
        return card
    }

    // ---------------------------------------------------------------- 设备页

    private fun buildDevicesPage(): ScrollView {
        val column = pageColumn()
        val head = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        val headTexts = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        headTexts.addView(
            TextView(this).apply {
                text = getString(R.string.tab_devices)
                textSize = RdcText.METRIC
                setTextColor(RdcColor.TEXT)
            },
        )
        headTexts.addView(note("同账号设备、在线状态与受信标记。", 10f, RdcColor.MUTED))
        head.addView(headTexts)
        head.addView(
            FrameLayout(this).apply {
                background = roundedDrawable(RdcColor.PANEL_2, 10)
                layoutParams = LinearLayout.LayoutParams(dp(34), dp(34))
                isClickable = true
                isFocusable = true
                contentDescription = "绑定新设备"
                setOnClickListener { openBindPanel() }
                addView(
                    iconView(R.drawable.ic_lucide_plus, 18, RdcColor.MUTED),
                    FrameLayout.LayoutParams(dp(18), dp(18)).apply { gravity = Gravity.CENTER },
                )
            },
        )
        column.addView(head)

        devicesChips = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(12), 0, 0)
        }
        val filters = listOf(
            FILTER_DEVICE_ALL to "全部",
            FILTER_DEVICE_ONLINE to "在线",
            FILTER_DEVICE_TRUSTED to "受信",
        )
        for ((value, label) in filters) {
            devicesChips.addView(
                filterChip(label, value == deviceFilter) { setDeviceFilter(value) },
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ).apply { rightMargin = dp(6) },
            )
        }
        column.addView(devicesChips)

        val search = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(12), 0, dp(12), 0)
            roundedBackground(RdcColor.PANEL, 12, RdcColor.LINE)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(44),
            ).apply { topMargin = dp(10) }
        }
        search.addView(iconView(R.drawable.ic_lucide_search, 17, RdcColor.FAINT))
        devicesSearch = EditText(this).apply {
            hint = "搜索设备名称或设备码"
            setHintTextColor(RdcColor.FAINT)
            setTextColor(RdcColor.TEXT)
            textSize = RdcText.BODY
            setSingleLine()
            setPadding(dp(8), 0, 0, 0)
            background = null
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f)
            addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
                override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) = Unit
                override fun afterTextChanged(s: Editable?) {
                    deviceQuery = s?.toString().orEmpty().trim()
                    refreshDevices()
                }
            })
        }
        search.addView(devicesSearch)
        column.addView(search)

        val accountCard = card()
        devicesAccountCard = accountCard
        val deviceSession = RdcAccountService.session(this)
        devicesAccountTitle = labelView(
            when {
                deviceSession?.remote == true -> "已登录：${deviceSession.name}"
                deviceSession != null -> "本地账号：${deviceSession.name}"
                else -> "登录后查看设备"
            },
            12f,
            RdcColor.TEXT,
        )
        devicesAccountNote = note(
            when {
                deviceSession?.remote == true -> "账号中心 · 正在同步设备列表…"
                deviceSession != null -> "本地账号，不跨设备同步；当前无云端设备。"
                else -> "登录同一账号后，桌面设备会自动出现在这里，并显示在线与受信状态。"
            },
            9.5f,
            RdcColor.FAINT,
        )
        accountCard.addView(devicesAccountTitle)
        accountCard.addView(devicesAccountNote)
        devicesAccountAction = actionView(
            "去登录",
            11.5f,
            RdcColor.ACCENT_STRONG,
            RdcColor.ACCENT_SOFT,
            10,
            RdcColor.ACCENT,
            36,
            RdcColor.PANEL_3,
        ).apply {
            setOnClickListener { openAccount() }
            visibility = if (deviceSession == null) View.VISIBLE else View.GONE
            layoutParams = LinearLayout.LayoutParams(dp(96), dp(36)).apply { topMargin = dp(8) }
        }
        accountCard.addView(devicesAccountAction)
        column.addView(accountCard)

        val groups = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        devicesOnlineGroup = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        devicesOfflineGroup = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        devicesEmptyHost = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        groups.addView(devicesOnlineGroup)
        groups.addView(devicesOfflineGroup)
        groups.addView(devicesEmptyHost)
        column.addView(groups)

        column.addView(
            note("在线状态每 15 秒刷新 · 数据来自账号中心设备接口；最近连接时间来自设备记录。", 9.5f, RdcColor.FAINT)
                .apply { setPadding(0, dp(12), 0, 0) },
        )
        column.addView(
            note(getString(R.string.devices_auto_confirm_locked), 9.5f, RdcColor.FAINT).apply {
                setPadding(0, dp(8), 0, 0)
            },
        )
        refreshDevices()
        loadRemoteDevices()
        return pageScroll(column)
    }

    /** 设备页只呈现真实来源: 账号中心设备列表; 无数据时给空态与引导. */
    private fun refreshDevices() {
        if (!::devicesOnlineGroup.isInitialized) {
            return
        }
        devicesOnlineGroup.removeAllViews()
        devicesOfflineGroup.removeAllViews()
        devicesEmptyHost.removeAllViews()

        val session = RdcAccountService.session(this)
        val devices = ArrayList<DemoDevice>()
        for (item in remoteDeviceItems) {
            devices.add(
                DemoDevice(
                    name = item.displayName,
                    code = item.deviceId,
                    system = platformLabel(item.platform),
                    lastConnected = item.lastConnectedAt,
                    lastSeen = item.lastSeenAt,
                    online = item.online,
                    trusted = item.trusted,
                    current = item.current,
                    trustSource = item.trustSource,
                    trustedAt = item.trustedAt,
                    demo = false,
                ),
            )
        }
        if (AppPrefs.demoData(this)) {
            devices.add(
                DemoDevice(
                    "演示电脑 A", "DEMO-7K2M-9QX4", "桌面电脑", "", "刚刚", true, true, false, "owner", "", true,
                ),
            )
            devices.add(
                DemoDevice(
                    "演示笔记本 B", "DEMO-3H8P-2VT6", "桌面电脑", "", "3 天前", false, false, false, "", "", true,
                ),
            )
        }
        val query = deviceQuery.lowercase(Locale.US)
        val visible = devices.filter { device ->
            val filterOk = when (deviceFilter) {
                FILTER_DEVICE_ONLINE -> device.online
                FILTER_DEVICE_TRUSTED -> device.trusted
                else -> true
            }
            filterOk && matches(device, query)
        }
        val online = visible.filter { it.online }
        val offline = visible.filter { !it.online }

        if (online.isNotEmpty()) {
            devicesOnlineGroup.addView(groupHeader("在线", online.size))
            online.forEach { devicesOnlineGroup.addView(deviceCard(it)) }
        }
        if (offline.isNotEmpty()) {
            devicesOfflineGroup.addView(groupHeader("离线", offline.size))
            offline.forEach { devicesOfflineGroup.addView(deviceCard(it)) }
        }

        if (visible.isEmpty()) {
            // 未登录时空态卡已带「去登录」, 顶部账号同步卡不再重复出现
            devicesAccountCard?.visibility = if (session == null) View.GONE else View.VISIBLE
            val body: String
            val title: String
            val primary: String
            val onPrimary: () -> Unit
            val secondary: String?
            when {
                query.isNotEmpty() -> {
                    title = "没有匹配的设备"
                    body = "没有找到名称或设备码包含「$deviceQuery」的设备。"
                    primary = "清空搜索"
                    onPrimary = { devicesSearch.setText("") }
                    secondary = "查看全部"
                }
                deviceFilter != FILTER_DEVICE_ALL -> {
                    title = "没有符合条件的设备"
                    body = "当前筛选没有设备；切换到「全部」查看账号下所有设备。"
                    primary = "查看全部"
                    onPrimary = { setDeviceFilter(FILTER_DEVICE_ALL) }
                    secondary = null
                }
                session?.remote == true -> {
                    title = "这个账号还没有设备"
                    body = "在桌面端登录同一账号后会自动出现；也可以复制桌面端设备码，回到这里用连接码连接。"
                    primary = "用连接码连接"
                    onPrimary = { selectTab(TAB_CONNECT) }
                    secondary = "刷新"
                }
                session != null -> {
                    title = "本地账号没有云端设备"
                    body = "本地账号不跨设备同步；登录账号中心后可自动同步同账号设备。"
                    primary = "用连接码连接"
                    onPrimary = { selectTab(TAB_CONNECT) }
                    secondary = "登录账号中心"
                }
                else -> {
                    title = "登录后查看设备"
                    body = "登录同一账号后，桌面设备会自动出现在这里，并显示在线与受信状态。"
                    primary = "去登录"
                    onPrimary = { openAccount() }
                    secondary = "用连接码连接"
                }
            }
            devicesEmptyHost.addView(
                emptyStateCard(
                    iconRes = R.drawable.ic_tab_devices,
                    title = title,
                    body = body,
                    primary = primary,
                    onPrimary = onPrimary,
                    secondary = secondary,
                    onSecondary = {
                        when {
                            query.isNotEmpty() || deviceFilter != FILTER_DEVICE_ALL -> {
                                devicesSearch.setText("")
                                setDeviceFilter(FILTER_DEVICE_ALL)
                            }
                            secondary == "刷新" -> loadRemoteDevices(force = true)
                            secondary == "登录账号中心" || secondary == "去登录" -> openAccount()
                            else -> selectTab(TAB_CONNECT)
                        }
                    },
                ),
            )
        } else {
            devicesAccountCard?.visibility = View.VISIBLE
        }
    }

    private data class DemoDevice(
        val name: String,
        val code: String,
        val system: String,
        val lastConnected: String,
        val lastSeen: String,
        val online: Boolean,
        val trusted: Boolean,
        val current: Boolean,
        val trustSource: String,
        val trustedAt: String,
        val demo: Boolean,
    )

    private fun matches(device: DemoDevice, query: String): Boolean =
        query.isEmpty() || device.name.lowercase(Locale.US).contains(query) ||
            device.code.lowercase(Locale.US).contains(query)

    private fun platformLabel(platform: String): String = when (platform.lowercase(Locale.US)) {
        "windows", "linux", "macos" -> "桌面电脑"
        "android" -> "安卓设备"
        else -> "未知平台"
    }

    /** A3: 平台图标; 桌面端统一用笔记本图标, 安卓用手机图标. */
    private fun platformIcon(platform: String): Int =
        if (platform.equals("android", ignoreCase = true)) {
            R.drawable.ic_lucide_smartphone
        } else {
            R.drawable.ic_tab_devices
        }

    private fun trustSourceLabel(source: String): String = when (source.lowercase(Locale.US)) {
        "device" -> "设备自身"
        "owner" -> "账号所有者"
        else -> "未知来源"
    }

    /** 审计结果只显示中文; 未知枚举值原样返回, 不凭空造词. */
    private fun resultLabel(value: String): String = when (value.lowercase(Locale.US)) {
        "ok", "success", "allowed" -> "成功"
        "denied", "reject", "rejected" -> "已拒绝"
        "failed", "fail", "error" -> "失败"
        "pending" -> "待处理"
        else -> value
    }

    private fun shortDeviceId(id: String): String =
        if (id.length > 8) "${id.take(4)}…${id.takeLast(4)}" else id

    private fun relativeDeviceTime(iso: String): String =
        if (iso.isBlank()) "未知" else iso.take(16).replace('T', ' ')

    private fun lastSeenLabel(iso: String): String =
        if (iso.isBlank()) "未知" else iso.take(16).replace('T', ' ')

    /** 一键连接入口: 受信免码未接入, 统一把设备码填进连接页, 由用户补 6 位连接码. */
    private fun useDeviceCode(deviceId: String) {
        selectTab(TAB_CONNECT)
        peerInput.setText(deviceId)
        codeInput.requestFocus()
        toast("已填入设备码，请输入 6 位连接码")
        RdcLog.info("Devices", "use_device_code", "device_id=${shortDeviceId(deviceId)}")
    }

    private fun setDeviceFilter(value: Int) {
        deviceFilter = value
        for (index in 0 until devicesChips.childCount) {
            val chip = devicesChips.getChildAt(index) as? TextView ?: continue
            val selected = index == value
            chip.setTextColor(if (selected) RdcColor.ACCENT_STRONG else RdcColor.MUTED)
            chip.pressableBackground(
                if (selected) RdcColor.ACCENT_SOFT else RdcColor.PANEL_2,
                RdcColor.PRESS,
                999,
                if (selected) RdcColor.ACCENT else RdcColor.LINE_STRONG,
            )
            chip.isSelected = selected
            chip.contentDescription = describeChip(chip.text.toString(), selected)
        }
        refreshDevices()
        RdcLog.info("Devices", "filter_changed", "filter=$value")
    }

    private fun filterChip(label: String, selected: Boolean, onClick: () -> Unit): TextView = TextView(this).apply {
        text = label
        textSize = RdcText.TINY
        setTextColor(if (selected) RdcColor.ACCENT_STRONG else RdcColor.MUTED)
        setPadding(dp(10), dp(5), dp(10), dp(5))
        isClickable = true
        isFocusable = true
        isSelected = selected
        contentDescription = describeChip(label, selected)
        pressableBackground(
            if (selected) RdcColor.ACCENT_SOFT else RdcColor.PANEL_2,
            RdcColor.PRESS,
            999,
            if (selected) RdcColor.ACCENT else RdcColor.LINE_STRONG,
        )
        setOnClickListener { onClick() }
    }

    /**
     * 拉取账号中心设备列表; 只在后台线程发起, 结果回主线程刷新界面.
     * 未登录或本机账号时清空缓存, 不发网络请求.
     */
    private fun loadRemoteDevices(force: Boolean = false, quiet: Boolean = false) {
        if (!::devicesOnlineGroup.isInitialized) {
            return
        }
        val session = RdcAccountService.session(this)
        if (session?.remote != true) {
            remoteDeviceItems = emptyList()
            remoteDevicesLoadedFor = null
            remoteDevicesError = null
            remoteDevicesLoading = false
            refreshDevices()
            refreshConnectTrusted()
            updateProfileStats()
            return
        }
        val signature = "${session.accountId}|${session.deviceId}"
        if (!force && remoteDevicesLoadedFor == signature) {
            return
        }
        if (remoteDevicesLoading) {
            return
        }
        remoteDevicesLoading = true
        remoteDevicesError = null
        if (!quiet) {
            refreshAccountSurfaces()
            refreshConnectTrusted()
        }
        val application = applicationContext
        Thread({
            val outcome = RdcAccountService.devices(application)
            runOnUiThread {
                remoteDevicesLoading = false
                if (outcome.ok) {
                    remoteDeviceItems = outcome.items
                    remoteDevicesLoadedFor = signature
                    remoteDevicesError = null
                } else {
                    // 轮询失败保留上一次成功结果, 避免在线列表闪断
                    remoteDevicesError = outcome.message
                }
                refreshDevices()
                refreshConnectTrusted()
                updateProfileStats()
                refreshAccountSurfaces()
            }
        }, "devices-sync").start()
    }

    private fun groupHeader(title: String, count: Int): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(18), 0, dp(6))
            addView(
                labelView(title, 11f, RdcColor.MUTED).apply {
                    layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                },
            )
            addView(note("$count 台", 9.5f, RdcColor.FAINT))
        }

    private fun deviceCard(device: DemoDevice): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(10), dp(12), dp(10))
            pressableBackground(RdcColor.PANEL, RdcColor.PRESS, 11, RdcColor.LINE)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(6) }
            isClickable = true
            isFocusable = true

            val top = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
            }
            top.addView(statusDot(device.online))
            top.addView(
                labelView(device.name, 12.5f, RdcColor.TEXT).apply {
                    setPadding(dp(8), 0, dp(6), 0)
                    layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                },
            )
            when {
                device.current -> top.addView(miniBadge("本机", RdcColor.ACCENT_STRONG, RdcColor.ACCENT_SOFT))
                device.demo -> top.addView(miniBadge("示例", RdcColor.WARN, RdcColor.PANEL_3))
                else -> top.addView(
                    actionView(
                        if (device.online) "连接" else "离线",
                        10.5f,
                        if (device.online) RdcColor.ACCENT_STRONG else RdcColor.FAINT,
                        if (device.online) RdcColor.ACCENT_SOFT else RdcColor.PANEL_2,
                        8,
                        if (device.online) RdcColor.ACCENT else RdcColor.LINE,
                        30,
                        RdcColor.PANEL_3,
                    ).apply {
                        isEnabled = device.online
                        layoutParams = LinearLayout.LayoutParams(dp(60), dp(30))
                        setOnClickListener {
                            if (device.online) {
                                useDeviceCode(device.code)
                            }
                        }
                    },
                )
            }
            addView(top)

            val chips = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL
                setPadding(0, dp(7), 0, 0)
            }
            chips.addView(systemChip(device.system))
            chips.addView(
                miniBadge(
                    if (device.online) "在线" else "离线",
                    if (device.online) RdcColor.OK else RdcColor.MUTED,
                    RdcColor.PANEL_3,
                ).apply {
                    layoutParams = LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.WRAP_CONTENT,
                        ViewGroup.LayoutParams.WRAP_CONTENT,
                    ).apply { leftMargin = dp(5) }
                },
            )
            if (device.trusted) {
                chips.addView(
                    miniBadge("受信", RdcColor.OK, RdcColor.PANEL_3).apply {
                        layoutParams = LinearLayout.LayoutParams(
                            ViewGroup.LayoutParams.WRAP_CONTENT,
                            ViewGroup.LayoutParams.WRAP_CONTENT,
                        ).apply { leftMargin = dp(5) }
                    },
                )
            }
            addView(chips)
            addView(
                labelView(shortDeviceId(device.code), 10.5f, RdcColor.FAINT).apply {
                    typeface = Typeface.MONOSPACE
                    setPadding(0, dp(7), 0, 0)
                },
            )
            addView(note("最近连接：${relativeDeviceTime(device.lastConnected)}", 9.5f, RdcColor.FAINT))
            if (device.trusted) {
                val trustedAt = relativeDeviceTime(device.trustedAt)
                addView(
                    note(
                        "受信来源：${trustSourceLabel(device.trustSource)} · 授信于 $trustedAt",
                        9.5f,
                        RdcColor.FAINT,
                    ),
                )
            }
            if (!device.online && device.lastSeen.isNotBlank()) {
                addView(note("最近在线：${lastSeenLabel(device.lastSeen)}", 9.5f, RdcColor.FAINT))
            }
            setOnClickListener {
                RdcLog.info("Devices", "card_clicked", "demo=${device.demo} current=${device.current}")
                when {
                    device.demo -> toast("示例设备，不会发起真实连接")
                    device.current -> toast("这是本机，不能连接自己")
                    device.online -> useDeviceCode(device.code)
                    else -> toast("设备不在线，无法连接")
                }
            }
            attachCopyMenu(
                this,
                listOf(
                    "设备名称" to device.name,
                    "设备标识" to device.code,
                    "受信来源" to if (device.trusted) trustSourceLabel(device.trustSource) else "",
                ),
            )
        }

    private fun miniBadge(text: String, textColor: Int, background: Int): TextView = TextView(this).apply {
        this.text = text
        textSize = RdcText.MICRO
        setTextColor(textColor)
        setPadding(dp(7), dp(2), dp(7), dp(2))
        roundedBackground(background, 999, RdcColor.LINE)
    }

    private fun systemChip(text: String): TextView = TextView(this).apply {
        this.text = text
        textSize = RdcText.MICRO
        setTextColor(RdcColor.MUTED)
        setPadding(dp(7), dp(2), dp(7), dp(2))
        roundedBackground(RdcColor.PANEL_3, 999, RdcColor.LINE)
    }

    private fun statusDot(online: Boolean): View = View(this).apply {
        background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(if (online) RdcColor.OK else RdcColor.FAINT)
        }
        layoutParams = LinearLayout.LayoutParams(dp(8), dp(8))
    }

    /** 空态插画 + 标题 + 说明 + 下一步按钮. */
    private fun emptyStateCard(
        iconRes: Int,
        title: String,
        body: String,
        primary: String,
        onPrimary: () -> Unit,
        secondary: String? = null,
        onSecondary: (() -> Unit)? = null,
    ): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        gravity = Gravity.CENTER_HORIZONTAL
        setPadding(dp(18), dp(22), dp(18), dp(18))
        roundedBackground(RdcColor.PANEL, 14, RdcColor.LINE)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(12) }

        addView(iconTile(iconRes, 64, RdcColor.FAINT, RdcColor.PANEL_3))
        addView(
            labelView(title, 14f, RdcColor.TEXT, Gravity.CENTER).apply {
                setPadding(0, dp(12), 0, 0)
            },
        )
        addView(
            note(body, 10f, RdcColor.FAINT).apply {
                gravity = Gravity.CENTER
                setPadding(dp(8), dp(6), dp(8), 0)
            },
        )
        addView(
            actionView(primary, 12f, Color.WHITE, RdcColor.ACCENT, 11, heightDp = 42, pressedBackground = RdcColor.ACCENT_PRESS)
                .apply {
                    setOnClickListener { onPrimary() }
                    layoutParams = LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        dp(42),
                    ).apply { topMargin = dp(16) }
                },
        )
        if (secondary != null && onSecondary != null) {
            addView(
                actionView(
                    secondary,
                    11.5f,
                    RdcColor.TEXT,
                    RdcColor.PANEL_3,
                    11,
                    RdcColor.LINE_STRONG,
                    40,
                    RdcColor.PRESS,
                ).apply {
                    setOnClickListener { onSecondary() }
                    layoutParams = LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        dp(40),
                    ).apply { topMargin = dp(8) }
                },
            )
        }
    }

    private fun iconTile(iconRes: Int, sizeDp: Int, tint: Int, background: Int): FrameLayout =
        FrameLayout(this).apply {
            setBackground(roundedDrawable(background, if (sizeDp >= 56) 999 else 11))
            layoutParams = LinearLayout.LayoutParams(dp(sizeDp), dp(sizeDp))
            addView(
                iconView(iconRes, (sizeDp * 0.5f).toInt(), tint),
                FrameLayout.LayoutParams(dp((sizeDp * 0.5f).toInt()), dp((sizeDp * 0.5f).toInt())).apply {
                    gravity = Gravity.CENTER
                },
            )
        }

    private fun iconView(iconRes: Int, sizeDp: Int, tint: Int): ImageView =
        ImageView(this).apply {
            setImageResource(iconRes)
            setColorFilter(tint)
            layoutParams = LinearLayout.LayoutParams(dp(sizeDp), dp(sizeDp))
        }

    private fun legendItem(text: String, dotColor: Int): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(
                View(this@MainActivity).apply {
                    background = GradientDrawable().apply {
                        shape = GradientDrawable.OVAL
                        setColor(dotColor)
                    }
                    layoutParams = LinearLayout.LayoutParams(dp(7), dp(7))
                },
            )
            addView(
                labelView(text, 10f, RdcColor.MUTED).apply {
                    setPadding(dp(5), 0, 0, 0)
                },
            )
        }

    private fun legendParams(startMargin: Int) = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.WRAP_CONTENT,
        ViewGroup.LayoutParams.WRAP_CONTENT,
    ).apply { leftMargin = dp(startMargin) }

    // ---------------------------------------------------------------- 消息页

    private fun buildMessagesPage(): ScrollView {
        val column = pageColumn()
        val head = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        val headTexts = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        headTexts.addView(
            TextView(this).apply {
                text = getString(R.string.tab_messages)
                textSize = RdcText.METRIC
                setTextColor(RdcColor.TEXT)
            },
        )
        headTexts.addView(note(getString(R.string.messages_sub), 10f, RdcColor.MUTED))
        head.addView(headTexts)
        head.addView(
            labelView(getString(R.string.messages_mark_all), 10.5f, RdcColor.ACCENT).apply {
                setPadding(dp(8), dp(6), 0, dp(6))
                isClickable = true
                isFocusable = true
                pressableText(this, RdcColor.ACCENT, RdcColor.ACCENT_STRONG)
                setOnClickListener { markAllRead() }
            },
        )
        column.addView(head)

        messageSummary = note("", 9.5f, RdcColor.FAINT).apply { setPadding(0, dp(10), 0, 0) }
        column.addView(messageSummary)

        messageGate = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        column.addView(messageGate)

        messageChips = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(10), 0, 0)
        }
        val filters = listOf(
            FILTER_ALL to "全部",
            FILTER_SESSION to "会话",
            FILTER_BINDING to "绑定",
            FILTER_APPROVAL to "审批",
            FILTER_GRANT to "授权",
            FILTER_ACCOUNT to "账号",
        )
        for ((value, label) in filters) {
            messageChips.addView(
                filterChip(label, value == messageFilter) { setMessageFilter(value) },
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ).apply { rightMargin = dp(6) },
            )
        }
        column.addView(messageChips)

        messageList = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(10), 0, 0)
        }
        column.addView(messageList)

        messageEmpty = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        column.addView(messageEmpty)
        refreshMessages()
        return pageScroll(column)
    }

    private fun setMessageFilter(value: Int) {
        messageFilter = value
        for (index in 0 until messageChips.childCount) {
            val child = messageChips.getChildAt(index)
            val selected = index == filterIndex(value)
            (child as? TextView)?.let {
                it.setTextColor(if (selected) RdcColor.ACCENT_STRONG else RdcColor.MUTED)
                it.pressableBackground(
                    if (selected) RdcColor.ACCENT_SOFT else RdcColor.PANEL_2,
                    RdcColor.PRESS,
                    999,
                    if (selected) RdcColor.ACCENT else RdcColor.LINE_STRONG,
                )
                it.isSelected = selected
                it.contentDescription = describeChip(it.text.toString(), selected)
            }
        }
        refreshMessages()
        RdcLog.info("Messages", "filter_changed", "filter=$value")
    }

    private fun filterIndex(value: Int): Int = when (value) {
        FILTER_SESSION -> 1
        FILTER_BINDING -> 2
        FILTER_APPROVAL -> 3
        FILTER_GRANT -> 4
        FILTER_ACCOUNT -> 5
        else -> 0
    }

    /** A4: 消息与审计走账号中心 /v1; 未读以服务端 unread 为准, 不展示本地水位. */
    private fun refreshMessages() {
        if (!::messageList.isInitialized) {
            return
        }
        messageList.removeAllViews()
        messageEmpty.removeAllViews()
        messageEmpty.visibility = View.GONE
        messageGate.removeAllViews()
        val session = RdcAccountService.session(this)
        val remote = session?.remote == true
        messageSummary.visibility = if (remote) View.VISIBLE else View.GONE
        messageChips.visibility = if (remote) View.VISIBLE else View.GONE
        if (!remote) {
            messageGate.addView(
                emptyStateCard(
                    iconRes = R.drawable.ic_tab_messages,
                    title = "登录后查看消息",
                    body = "会话与账号审计事件按账号同步；登录后未读数以服务端未读数为准。",
                    primary = "去登录",
                    onPrimary = { openAccount() },
                    secondary = "用连接码连接",
                    onSecondary = { selectTab(TAB_CONNECT) },
                ),
            )
            refreshBadge()
            return
        }
        messageSummary.text = if (messageLoading && remoteMessages.isEmpty()) {
            "正在读取消息与审计事件…"
        } else {
            "共 ${messageUnread} 条未读 · ${messageTotal} 条消息 · 已读至 ${readUntilLabel()}"
        }
        if (messageError != null && remoteMessages.isEmpty()) {
            messageEmpty.visibility = View.VISIBLE
            messageEmpty.addView(
                emptyStateCard(
                    iconRes = R.drawable.ic_tab_messages,
                    title = "消息加载失败",
                    body = messageError ?: "",
                    primary = "重试",
                    onPrimary = { loadMessages(force = true) },
                    secondary = "检查登录状态",
                    onSecondary = { openAccount() },
                ),
            )
            refreshBadge()
            return
        }
        val items = remoteMessages.filter { message ->
            messageFilter == FILTER_ALL || message.category == filterCategory(messageFilter)
        }
        if (items.isEmpty()) {
            messageEmpty.visibility = View.VISIBLE
            messageEmpty.addView(
                emptyStateCard(
                    iconRes = R.drawable.ic_tab_messages,
                    title = if (messageFilter == FILTER_ALL) "暂无消息" else "该分类暂无消息",
                    body = "会话与账号审计事件会出现在这里；未读与已读水位以服务端为准。",
                    primary = if (messageFilter == FILTER_ALL) "去连接页" else "查看全部",
                    onPrimary = {
                        if (messageFilter == FILTER_ALL) {
                            selectTab(TAB_CONNECT)
                        } else {
                            setMessageFilter(FILTER_ALL)
                        }
                    },
                ),
            )
            refreshBadge()
            return
        }
        var lastDay = ""
        for (item in items) {
            val day = messageDayLabel(item.ts)
            if (day != lastDay) {
                lastDay = day
                messageList.addView(dayHeader(day))
            }
            messageList.addView(messageRow(item))
        }
        refreshBadge()
    }

    private fun readUntilLabel(): String =
        if (messageReadUntil.isBlank()) "—" else messageReadUntil.take(16).replace('T', ' ')

    private fun filterCategory(filter: Int): String = when (filter) {
        FILTER_SESSION -> "session"
        FILTER_BINDING -> "binding"
        FILTER_APPROVAL -> "approval"
        FILTER_GRANT -> "grant"
        FILTER_ACCOUNT -> "account"
        else -> ""
    }

    private fun categoryLabel(message: AccountMessage): String {
        if (message.categoryLabel.isNotBlank()) {
            return message.categoryLabel
        }
        return when (message.category) {
            "session" -> "会话"
            "binding" -> "绑定"
            "approval" -> "审批"
            "grant" -> "授权"
            "account" -> "账号"
            "system" -> "系统"
            else -> "其他"
        }
    }

    private fun messageDayLabel(ts: String): String {
        val day = ts.take(10)
        if (day.length < 10) {
            return "更早"
        }
        val format = SimpleDateFormat("yyyy-MM-dd", Locale.US)
        val today = format.format(Date())
        val calendar = java.util.Calendar.getInstance()
        calendar.add(java.util.Calendar.DAY_OF_YEAR, -1)
        val yesterday = format.format(calendar.time)
        return when (day) {
            today -> "今天"
            yesterday -> "昨天"
            else -> day
        }
    }

    private fun dayHeader(label: String): TextView = labelView(label, 10f, RdcColor.FAINT).apply {
        setPadding(dp(2), dp(10), 0, dp(2))
    }

    private fun messageRow(message: AccountMessage): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(12), dp(10), dp(12), dp(10))
        pressableBackground(RdcColor.PANEL, RdcColor.PRESS, 11, RdcColor.LINE)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(6) }

        val head = LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        head.addView(miniBadge(categoryLabel(message), RdcColor.ACCENT_STRONG, RdcColor.ACCENT_SOFT))
        head.addView(
            labelView(
                message.ts.drop(11).take(5).ifBlank { message.ts },
                9f,
                RdcColor.FAINT,
                Gravity.END,
            ).apply {
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            },
        )
        addView(head)
        addView(
            labelView(message.title.ifBlank { "会话事件" }, 12f, RdcColor.TEXT).apply {
                setPadding(0, dp(6), 0, 0)
            },
        )
        val meta = listOf(message.deviceName, message.actor).filter { it.isNotBlank() }.joinToString(" · ")
        if (meta.isNotBlank()) {
            addView(note(meta, 9.5f, RdcColor.FAINT))
        }
        val detail = listOf(resultLabel(message.result), message.reason)
            .filter { it.isNotBlank() }
            .joinToString(" · ")
        if (detail.isNotBlank()) {
            addView(note(detail, 9.5f, RdcColor.MUTED).apply { setPadding(0, dp(4), 0, 0) })
        }
        attachCopyMenu(
            this,
            listOf(
                "消息标题" to message.title,
                "设备名称" to message.deviceName,
                "设备标识" to message.deviceId,
                "失败原因" to message.reason,
            ),
        )
    }

    /** A4: 拉取账号级消息; 未读与已读水位直接采用服务端返回值. */
    private fun loadMessages(force: Boolean = false) {
        if (!::messageList.isInitialized) {
            return
        }
        val session = RdcAccountService.session(this)
        if (session?.remote != true) {
            remoteMessages = emptyList()
            messageUnread = 0
            messageTotal = 0
            messageReadUntil = ""
            messageError = null
            messagesLoadedFor = null
            messageLoading = false
            refreshMessages()
            return
        }
        val signature = "${session.accountId}|${session.deviceId}"
        if (!force && messagesLoadedFor == signature) {
            return
        }
        if (messageLoading) {
            return
        }
        messageLoading = true
        messageError = null
        refreshMessages()
        val application = applicationContext
        Thread({
            val outcome = RdcAccountService.messages(application)
            runOnUiThread {
                messageLoading = false
                if (outcome.ok) {
                    remoteMessages = outcome.items
                    messageUnread = outcome.unread
                    messageTotal = outcome.total
                    messageReadUntil = outcome.readUntil
                    messagesLoadedFor = signature
                    messageError = null
                } else {
                    messageError = outcome.message
                }
                refreshMessages()
                refreshAccountSurfaces()
            }
        }, "messages-sync").start()
    }

    private fun markAllRead() {
        val session = RdcAccountService.session(this)
        if (session?.remote != true) {
            openAccount()
            return
        }
        if (messageUnread == 0) {
            toast("当前没有未读消息")
            return
        }
        val application = applicationContext
        Thread({
            val outcome = RdcAccountService.markAllRead(application)
            runOnUiThread {
                toast(outcome.message)
                loadMessages(force = true)
                refreshAccountSurfaces()
            }
        }, "messages-read").start()
    }

    /** 底部导航未读红点: 只消费服务端 unread; 未登录或刷新前保持隐藏. */
    private fun refreshBadge() {
        if (!::tabCells.isInitialized) {
            return
        }
        val remote = RdcAccountService.session(this)?.remote == true
        val unread = if (remote) messageUnread else 0
        tabCells[TAB_MESSAGES].dot.visibility = if (unread > 0) View.VISIBLE else View.GONE
        tabCells[TAB_MESSAGES].root.contentDescription = describeTab(TAB_MESSAGES, currentTab == TAB_MESSAGES)
        profileUnreadBadge?.let {
            it.text = if (unread > 99) "99+" else unread.toString()
            it.visibility = if (unread > 0) View.VISIBLE else View.GONE
        }
    }

    /** 底部导航的无障碍描述: 名称 + 选中态 + 服务端未读数. */
    private fun describeTab(index: Int, selected: Boolean): String {
        val parts = mutableListOf(tabCells[index].label.text.toString())
        if (selected) {
            parts.add(getString(R.string.a11y_selected))
        }
        if (index == TAB_MESSAGES && messageUnread > 0) {
            parts.add("${messageUnread} 条未读")
        }
        return parts.joinToString("，")
    }

    // ---------------------------------------------------------------- 我的页

    private fun buildProfilePage(): ScrollView {
        val column = pageColumn()
        column.addView(pageHeader(getString(R.string.tab_profile), getString(R.string.profile_sub)))

        val session = RdcAccountService.session(this)
        profileAccountSignature = session?.accountId ?: GUEST_ACCOUNT

        val account = card()
        val accountHead = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        accountHead.addView(
            FrameLayout(this).apply {
                background = roundedDrawable(RdcColor.ACCENT_SOFT, 999)
                layoutParams = LinearLayout.LayoutParams(dp(48), dp(48))
                addView(
                    labelView(
                        session?.name?.take(1)?.uppercase(Locale.US) ?: "未",
                        18f,
                        RdcColor.ACCENT_STRONG,
                        Gravity.CENTER,
                    ),
                    FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT,
                    ),
                )
            },
        )
        val accountTexts = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), 0, dp(8), 0)
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        accountTexts.addView(labelView(session?.name ?: "未登录", 14f, RdcColor.TEXT))
        accountTexts.addView(
            note(
                when {
                    session?.remote == true -> "账号中心 · ${shortDeviceId(session.accountId)}"
                    session != null -> "本地账号 · 不跨设备同步"
                    else -> "登录后可同步设备、受信与消息"
                },
                9.5f,
                RdcColor.FAINT,
            ),
        )
        accountHead.addView(accountTexts)
        accountHead.addView(
            miniBadge(
                if (session != null) "已登录" else "未登录",
                if (session != null) RdcColor.OK else RdcColor.MUTED,
                RdcColor.PANEL_3,
            ),
        )
        account.addView(accountHead)

        if (session != null && session.remote) {
            val idRow = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(0, dp(10), 0, 0)
            }
            idRow.addView(
                labelView(session.accountId, 10.5f, RdcColor.FAINT).apply {
                    layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                },
            )
            idRow.addView(
                labelView("复制账号编号", 10f, RdcColor.ACCENT).apply {
                    setPadding(dp(6), dp(4), 0, dp(4))
                    isClickable = true
                    isFocusable = true
                    pressableText(this, RdcColor.ACCENT, RdcColor.ACCENT_STRONG)
                    setOnClickListener { copyToClipboard(session.accountId, "账号编号") }
                },
            )
            account.addView(idRow)
            // 账号与设备信息合并到「我的」页, 登录后不再单独占一屏
            account.addView(profileInfoRow("设备编号", session.deviceId))
            account.addView(profileInfoRow("平台", "安卓 ${Build.VERSION.RELEASE}"))
            account.addView(profileInfoRow("受信状态", if (session.trusted) "已受信" else "未受信"))
            account.addView(
                profileInfoRow(
                    "登录时间",
                    java.text.SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.CHINA)
                        .format(java.util.Date(session.loginAt)),
                ),
            )
            attachCopyMenu(
                account,
                listOf(
                    "账号名称" to session.name,
                    "账号编号" to session.accountId,
                    "设备编号" to session.deviceId,
                ),
            )
        } else if (session == null) {
            account.addView(
                actionView(
                    "登录 / 注册",
                    12f,
                    Color.WHITE,
                    RdcColor.ACCENT,
                    10,
                    heightDp = 40,
                    pressedBackground = RdcColor.ACCENT_PRESS,
                ).apply {
                    setOnClickListener { openAccount() }
                    layoutParams = LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        dp(40),
                    ).apply { topMargin = dp(12) }
                },
            )
        }
        column.addView(account)

        val stats = card()
        val statsRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        profileStatBound = addProfileStat(statsRow, "已绑定", "0")
        profileStatTrusted = addProfileStat(statsRow, "受信", "0")
        profileStatOnline = addProfileStat(statsRow, "在线", "0")
        stats.addView(statsRow)
        column.addView(stats)
        updateProfileStats()

        column.addView(sectionRow("快捷入口", ""))
        val entries = listCard()
        entries.addView(
            settingsRow(
                R.drawable.ic_tab_devices,
                "设备与信任",
                "同账号设备、在线状态与受信来源",
            ) { selectTab(TAB_DEVICES) },
        )
        entries.addView(rowDivider())
        profileUnreadBadge = miniBadge("0", Color.WHITE, RdcColor.DANGER).apply {
            visibility = View.GONE
        }
        entries.addView(
            settingsRow(
                R.drawable.ic_tab_messages,
                "消息中心",
                "会话与账号审计 · 未读以服务端为准",
                trailing = profileUnreadBadge,
            ) { selectTab(TAB_MESSAGES) },
        )
        entries.addView(rowDivider())
        entries.addView(
            settingsRow(R.drawable.ic_lucide_settings, "设置", "画质、网络、安全、通知与外观") {
                showSettingsPanel()
            },
        )
        column.addView(entries)

        if (session != null) {
            column.addView(sectionRow("登录状态", ""))
            val status = listCard()
            status.addView(
                settingsRow(
                    R.drawable.ic_lucide_shield,
                    if (session.remote) "账号中心登录" else "本地账号登录",
                    if (session.remote) {
                        "设备令牌有效期至 ${formatAccountTime(session.expiresAt)}"
                    } else {
                        "登录于 ${formatAccountTime(session.loginAt)}"
                    },
                    trailing = miniBadge("正常", RdcColor.OK, RdcColor.PANEL_3),
                ) { openAccount() },
            )
            status.addView(rowDivider())
            status.addView(
                settingsRow(
                    R.drawable.ic_lucide_logout,
                    "退出登录",
                    if (session.remote) "账号中心 · 退出即吊销本机设备令牌" else "本地账号 · 退出后需重新登录",
                ) { confirmLogout() },
            )
            column.addView(status)
        }

        column.addView(
            note(getString(R.string.profile_footnote), 9.5f, RdcColor.FAINT).apply {
                gravity = Gravity.CENTER
                setPadding(0, dp(16), 0, 0)
            },
        )
        return pageScroll(column)
    }

    /** 「我的」页的账号/设备信息行: 左标题右值, 值可长按或点按复制. */
    private fun profileInfoRow(label: String, value: String): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        setPadding(0, dp(8), 0, 0)
        addView(
            labelView(label, 10f, RdcColor.MUTED).apply {
                layoutParams = LinearLayout.LayoutParams(dp(72), ViewGroup.LayoutParams.WRAP_CONTENT)
            },
        )
        addView(
            labelView(value, 10.5f, RdcColor.TEXT).apply {
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                isClickable = true
                isFocusable = true
                setOnClickListener { copyToClipboard(value, label) }
            },
        )
    }

    private fun addProfileStat(row: LinearLayout, label: String, initial: String): TextView {
        val cell = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        val value = labelView(initial, 18f, RdcColor.TEXT, Gravity.CENTER)
        cell.addView(value)
        cell.addView(labelView(label, 9.5f, RdcColor.MUTED, Gravity.CENTER))
        row.addView(cell)
        return value
    }

    /** 统计只来自账号中心设备列表; 未登录显示 —, 不虚构数字. */
    private fun updateProfileStats() {
        val remote = RdcAccountService.session(this)?.remote == true
        val bound = profileStatBound ?: return
        val trusted = profileStatTrusted ?: return
        val online = profileStatOnline ?: return
        if (!remote) {
            bound.text = "—"
            trusted.text = "—"
            online.text = "—"
            return
        }
        bound.text = remoteDeviceItems.size.toString()
        trusted.text = remoteDeviceItems.count { it.trusted }.toString()
        online.text = remoteDeviceItems.count { it.online }.toString()
    }

    /** 绑定入口: 绑定命令尚未进入安卓命令层, 如实提示, 不伪造成功. */
    private fun openBindPanel() {
        showPanel("绑定设备") { column, dialog ->
            column.addView(
                note(
                    "安卓端绑定接口尚未接入（桌面端绑定命令）。可复制桌面端设备码，回到连接页输入 6 位连接码发起连接。",
                    10.5f,
                    RdcColor.MUTED,
                ),
            )
            column.addView(
                actionView(
                    "去连接页",
                    11.5f,
                    Color.WHITE,
                    RdcColor.ACCENT,
                    10,
                    heightDp = 42,
                    pressedBackground = RdcColor.ACCENT_PRESS,
                ).apply {
                    layoutParams = LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        dp(42),
                    ).apply { topMargin = dp(12) }
                    setOnClickListener {
                        dialog.dismiss()
                        selectTab(TAB_CONNECT)
                    }
                },
            )
        }
    }

    private fun showSettingsPanel() {
        showPanel("设置") { column, dialog ->
            val settings = listCard()
            settings.addView(
                settingsRow(R.drawable.ic_lucide_settings, "画质", "默认画质档：${qualityLabel(AppPrefs.profile(this))}") {
                    dialog.dismiss()
                    showQualityPanel()
                },
            )
            settings.addView(rowDivider())
            settings.addView(
                settingsRow(
                    R.drawable.ic_lucide_wifi,
                    "网络",
                    "中继自动降档：${if (AppPrefs.autoDowngrade(this)) "开启" else "关闭"}",
                ) {
                    dialog.dismiss()
                    showNetworkPanel()
                },
            )
            settings.addView(rowDivider())
            settings.addView(
                settingsRow(
                    R.drawable.ic_lucide_shield,
                    "安全",
                    "剪贴板同步：${if (AppPrefs.clipboardSync(this)) "开启" else "关闭"}",
                ) {
                    dialog.dismiss()
                    showSecurityPanel()
                },
            )
            settings.addView(rowDivider())
            settings.addView(
                settingsRow(R.drawable.ic_lucide_bell, "通知", "消息未读以服务端为准") {
                    dialog.dismiss()
                    showNotifyPanel()
                },
            )
            settings.addView(rowDivider())
            settings.addView(
                switchSettingRow(
                    R.drawable.ic_lucide_moon,
                    "外观",
                    if (AppPrefs.darkTheme(this)) "深色（默认）" else "浅色",
                    AppPrefs.darkTheme(this),
                ) { enabled ->
                    AppPrefs.setDarkTheme(this, enabled)
                    RdcLog.info("Profile", "theme_changed", "dark=$enabled")
                    dialog.dismiss()
                    recreate()
                },
            )
            settings.addView(rowDivider())
            settings.addView(
                switchSettingRow(
                    R.drawable.ic_lucide_info,
                    "示例数据",
                    "仅用于展示信息层次，默认关闭",
                    AppPrefs.demoData(this),
                ) { enabled ->
                    AppPrefs.setDemoData(this, enabled)
                    RdcLog.info("Profile", "demo_data", "enabled=$enabled")
                    dialog.dismiss()
                    selectTab(TAB_PROFILE)
                },
            )
            column.addView(settings)
            column.addView(
                actionView(
                    "关于远程桌面 · 版本 ${versionName()}",
                    11.5f,
                    RdcColor.MUTED,
                    RdcColor.PANEL_2,
                    10,
                    RdcColor.LINE_STRONG,
                    42,
                    RdcColor.PRESS,
                ).apply {
                    layoutParams = LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        dp(42),
                    ).apply { topMargin = dp(10) }
                    setOnClickListener {
                        dialog.dismiss()
                        showAboutPanel()
                    }
                },
            )
        }
    }

    /** 设置列表容器: 无内边距, 行自带 14dp 内边距与分隔线. */
    private fun listCard(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        roundedBackground(RdcColor.PANEL, 14, RdcColor.LINE)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(8) }
    }

    private fun rowDivider(): View = View(this).apply {
        setBackgroundColor(RdcColor.LINE)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            dp(1),
        ).apply { leftMargin = dp(56) }
    }

    private fun settingsRow(
        iconRes: Int,
        title: String,
        subtitle: String,
        trailing: View? = null,
        onClick: () -> Unit,
    ): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        setPadding(dp(14), dp(12), dp(14), dp(12))
        isClickable = true
        isFocusable = true
        pressableBackground(Color.TRANSPARENT, RdcColor.PANEL_2, 10)
        addView(iconTile(iconRes, 30, RdcColor.MUTED, RdcColor.PANEL_3))
        val texts = LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), 0, dp(8), 0)
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        texts.addView(labelView(title, 12f, RdcColor.TEXT))
        texts.addView(labelView(subtitle, 9.5f, RdcColor.FAINT))
        addView(texts)
        if (trailing != null) {
            addView(trailing)
        }
        addView(iconView(R.drawable.ic_lucide_chevron_right, 16, RdcColor.FAINT))
        setOnClickListener { onClick() }
    }

    private fun switchSettingRow(
        iconRes: Int,
        title: String,
        subtitle: String,
        checked: Boolean,
        onChanged: (Boolean) -> Unit,
    ): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        setPadding(dp(14), dp(12), dp(14), dp(12))
        addView(iconTile(iconRes, 30, RdcColor.MUTED, RdcColor.PANEL_3))
        val texts = LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), 0, dp(8), 0)
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        texts.addView(labelView(title, 12f, RdcColor.TEXT))
        texts.addView(labelView(subtitle, 9.5f, RdcColor.FAINT))
        addView(texts)
        addView(
            SwitchCompat(this@MainActivity).apply {
                isChecked = checked
                contentDescription = title
                setOnCheckedChangeListener { _, value -> onChanged(value) }
            },
        )
    }

    private fun setDefaultProfile(item: String) {
        AppPrefs.setProfile(this, item)
        RdcLog.info("Profile", "default_profile", "profile=$item")
        toast("默认画质已设为 ${qualityLabel(item)}")
        selectTab(TAB_PROFILE)
    }

    /** 可选项的无障碍描述: 文本 + 选中态. */
    private fun describeChip(text: String, selected: Boolean): String =
        if (selected) "$text，${getString(R.string.a11y_selected)}" else text

    private fun settingHeader(
        title: String,
        noteText: String,
        enabled: Boolean,
        checked: Boolean,
        onChanged: (Boolean) -> Unit,
    ): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            val texts = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            }
            texts.addView(labelView(title, 11.5f, RdcColor.TEXT))
            texts.addView(note(noteText, 9.5f, RdcColor.FAINT))
            addView(texts)
            val toggle = SwitchCompat(this@MainActivity).apply {
                isChecked = checked
                isEnabled = enabled
                alpha = if (enabled) 1f else 0.4f
                // 读屏朗读开关用途与其后的说明, 而不是只读"开关"
                contentDescription = if (enabled) title else "$title，$noteText"
                importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_YES
                setOnCheckedChangeListener { _, value -> onChanged(value) }
            }
            addView(toggle)
        }

    // ---------------------------------------------------------------- 页面公共件

    private fun pageColumn(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(RdcMetrics.PAGE_PADDING, dp(14), RdcMetrics.PAGE_PADDING, dp(28))
        setBackgroundColor(RdcColor.BG)
    }

    private fun pageScroll(column: LinearLayout): ScrollView =
        ScrollView(this).apply {
            isFillViewport = true
            setBackgroundColor(RdcColor.BG)
            addView(column)
        }

    private fun pageHeader(title: String, subtitle: String): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.TOP
            val texts = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            }
            texts.addView(
                TextView(this@MainActivity).apply {
                    text = title
                    textSize = RdcText.METRIC
                    setTextColor(RdcColor.TEXT)
                },
            )
            texts.addView(note(subtitle, 10f, RdcColor.MUTED))
            addView(texts)
            addView(roleBadge())
        }

    private fun roleBadge(): TextView = TextView(this).apply {
        text = getString(R.string.role_badge)
        textSize = RdcText.MICRO
        setTextColor(RdcColor.MUTED)
        setPadding(dp(8), dp(4), dp(8), dp(4))
        roundedBackground(RdcColor.PANEL_2, 999, RdcColor.LINE)
    }

    private fun sectionRow(title: String, right: String): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(18), 0, dp(6))
            addView(
                labelView(title, 11f, RdcColor.MUTED).apply {
                    layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                },
            )
            if (right.isNotEmpty()) {
                addView(note(right, 9.5f, RdcColor.FAINT))
            }
        }

    private fun card(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(12), dp(10), dp(12), dp(12))
        roundedBackground(RdcColor.PANEL, 14, RdcColor.LINE)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(10) }
    }

    /**
     * 卡片内的状态块: 等宽、虚线描边、内容水平居中.
     *
     * 与原型 `.auth-gate` / `.empty` 同一口径: 图标、标题、说明、按钮都居中,
     * 用于受信设备门禁/空态/失败态与最近连接空态.
     */
    private fun stateBox(
        iconRes: Int,
        title: String,
        description: String,
        actionText: String,
        onAction: () -> Unit,
    ): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        gravity = RdcAlign.CARD_STATE
        setPadding(dp(16), dp(20), dp(16), dp(18))
        background = GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            setColor(RdcColor.PANEL_2)
            cornerRadius = dp(14).toFloat()
            setStroke(dp(1), RdcColor.LINE_STRONG, dp(5).toFloat(), dp(4).toFloat())
        }
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(6) }

        addView(
            iconTile(iconRes, 42, RdcColor.MUTED, RdcColor.PANEL_3).apply {
                layoutParams = LinearLayout.LayoutParams(dp(42), dp(42)).apply {
                    gravity = RdcAlign.CARD_STATE
                }
            },
        )
        addView(
            labelView(title, 13f, RdcColor.TEXT, RdcAlign.CARD_STATE_TEXT).apply {
                setPadding(0, dp(10), 0, 0)
            },
        )
        if (description.isNotEmpty()) {
            addView(
                labelView(description, 10.5f, RdcColor.MUTED, RdcAlign.CARD_STATE_TEXT).apply {
                    setPadding(dp(2), dp(5), dp(2), 0)
                },
            )
        }
        addView(
            actionView(
                actionText,
                11f,
                RdcColor.ACCENT_STRONG,
                RdcColor.ACCENT_SOFT,
                10,
                RdcColor.ACCENT,
                36,
                RdcColor.PANEL_3,
            ).apply {
                minWidth = dp(96)
                setPadding(dp(18), 0, dp(18), 0)
                setOnClickListener { onAction() }
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    dp(36),
                ).apply {
                    gravity = RdcAlign.CARD_STATE
                    topMargin = dp(12)
                }
            },
        )
    }

    private fun fieldLabel(text: String): TextView = labelView(text, 10.5f, RdcColor.MUTED).apply {
        setPadding(0, dp(13), 0, dp(6))
    }

    private fun fieldParams() = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        dp(45),
    )

    /** 输入框 + 行内「复制」按钮, 与原型 input-with-copy 一致. */
    private fun inputWithCopy(input: EditText, value: () -> String): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            roundedBackground(RdcColor.INPUT, 11, RdcColor.LINE_STRONG)
            layoutParams = fieldParams()
            input.background = null
            input.setPadding(dp(12), 0, 0, 0)
            addView(
                input,
                LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f),
            )
            addView(
                labelView("复制", 10.5f, RdcColor.ACCENT).apply {
                    setPadding(dp(10), 0, dp(12), 0)
                    isClickable = true
                    isFocusable = true
                    pressableText(this, RdcColor.ACCENT, RdcColor.ACCENT_STRONG)
                    setOnClickListener {
                        val current = value().trim()
                        if (current.isEmpty()) {
                            toast("暂无可复制内容")
                        } else {
                            copyToClipboard(current, "内容")
                        }
                    }
                },
            )
        }

    private fun link(text: String, onClick: () -> Unit): TextView =
        labelView(text, 11.5f, RdcColor.ACCENT, Gravity.CENTER).apply {
            setPadding(0, dp(12), 0, 0)
            isClickable = true
            isFocusable = true
            pressableText(this, RdcColor.ACCENT, RdcColor.ACCENT_STRONG)
            setOnClickListener { onClick() }
        }

    private fun note(text: String, sizeSp: Float, color: Int): TextView =
        labelView(text, sizeSp, color).apply { setPadding(0, dp(6), 0, 0) }

    // ---------------------------------------------------------------- 最近连接与跳转

    /** 最近连接来自命令返回值; 无真实记录时给空态与下一步指引, 不虚构会话. */
    private fun refreshRecent() {
        if (!::recentList.isInitialized) {
            return
        }
        recentList.removeAllViews()
        val result = ControlContract.listSessions()
        val demo = AppPrefs.demoData(this)

        if (demo) {
            recentList.addView(
                recentRow("演示电脑 A", "示例 · 刚刚", "示例") {
                    toast("示例记录，不会发起真实连接")
                },
            )
        }

        val count = result.data["count"] as? Int ?: 0
        if (result.ok && count > 0) {
            recentList.addView(note("最近会话已加载", 10f, RdcColor.MUTED))
            return
        }
        if (!demo) {
            recentList.addView(
                stateBox(
                    R.drawable.ic_tab_connect,
                    "还没有最近连接",
                    "完成一次连接后，这里会保存最近会话；下次只需点一下即可重连。",
                    "填写上方连接信息",
                ) { peerInput.requestFocus() },
            )
        }
        RdcLog.debug("Home", "recent_empty", "reason=${result.message}")
    }

    private fun recentRow(name: String, meta: String, demoTag: String?, onClick: () -> Unit): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(12), dp(11), dp(12), dp(11))
            pressableBackground(RdcColor.PANEL_2, RdcColor.PRESS, 12, RdcColor.LINE)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(6) }
            isClickable = true
            isFocusable = true
            addView(iconTile(R.drawable.ic_tab_devices, 34, RdcColor.MUTED, RdcColor.PANEL_3))
            val texts = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(10), 0, dp(8), 0)
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            }
            texts.addView(labelView(name, 12f, RdcColor.TEXT))
            texts.addView(labelView(meta, 9.5f, RdcColor.FAINT))
            addView(texts)
            if (demoTag != null) {
                addView(
                    TextView(this@MainActivity).apply {
                        text = demoTag
                        textSize = RdcText.MICRO
                        setTextColor(RdcColor.WARN)
                        setPadding(dp(7), dp(2), dp(7), dp(2))
                        roundedBackground(RdcColor.PANEL_3, 999, RdcColor.LINE)
                    },
                )
            }
            setOnClickListener { onClick() }
        }

    // ---------------------------------------------------------------- 设置面板

    /** 底部设置面板: 圆角深色容器, 关闭按钮固定在底部. */
    private fun showPanel(title: String, build: (LinearLayout, Dialog) -> Unit): Dialog {
        val dialog = Dialog(this)
        dialog.requestWindowFeature(Window.FEATURE_NO_TITLE)
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(12), dp(20), dp(20))
            roundedBackground(RdcColor.PANEL, 20, RdcColor.LINE)
        }
        panel.addView(
            View(this).apply {
                setBackgroundColor(RdcColor.LINE_STRONG)
                layoutParams = LinearLayout.LayoutParams(dp(36), dp(4)).apply {
                    gravity = Gravity.CENTER_HORIZONTAL
                    bottomMargin = dp(14)
                }
            },
        )
        panel.addView(labelView(title, 16f, RdcColor.TEXT))
        build(panel, dialog)
        panel.addView(
            actionView(
                "关闭",
                12f,
                RdcColor.TEXT,
                RdcColor.PANEL_3,
                10,
                RdcColor.LINE_STRONG,
                40,
                RdcColor.PRESS,
            ).apply {
                setOnClickListener { dialog.dismiss() }
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    dp(40),
                ).apply { topMargin = dp(16) }
            },
        )
        dialog.setContentView(ScrollView(this).apply { addView(panel) })
        dialog.window?.apply {
            setBackgroundDrawable(ColorDrawable(Color.TRANSPARENT))
            setGravity(Gravity.BOTTOM)
            setDimAmount(0.5f)
        }
        dialog.show()
        dialog.window?.setLayout(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        )
        return dialog
    }

    private fun showQualityPanel() {
        showPanel("画质") { column, dialog ->
            column.addView(note("画质档在真实会话建立后生效；当前用于记录默认偏好。", 10f, RdcColor.FAINT))
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                setPadding(0, dp(12), 0, 0)
            }
            for (item in ControlContract.DIRECT_PROFILES) {
                val selected = item == AppPrefs.profile(this)
                row.addView(
                    TextView(this).apply {
                        text = qualityLabel(item)
                        textSize = RdcText.TINY
                        setTextColor(if (selected) RdcColor.ACCENT_STRONG else RdcColor.MUTED)
                        setPadding(dp(10), dp(6), dp(10), dp(6))
                        isClickable = true
                        isFocusable = true
                        pressableBackground(
                            if (selected) RdcColor.ACCENT_SOFT else RdcColor.PANEL_2,
                            RdcColor.PRESS,
                            999,
                            if (selected) RdcColor.ACCENT else RdcColor.LINE_STRONG,
                        )
                        setOnClickListener {
                            setDefaultProfile(item)
                            dialog.dismiss()
                        }
                    },
                    LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.WRAP_CONTENT,
                        ViewGroup.LayoutParams.WRAP_CONTENT,
                    ).apply { rightMargin = dp(6) },
                )
            }
            column.addView(row)
        }
        RdcLog.info("Profile", "panel_quality")
    }

    private fun showNetworkPanel() {
        showPanel("网络") { column, dialog ->
            column.addView(note("局域网直连不经过中继；自动降档只影响公网中继会话。", 10f, RdcColor.FAINT))
            column.addView(
                settingHeader(
                    "中继自动降档",
                    "带宽不足时自动切换更低画质档",
                    enabled = true,
                    checked = AppPrefs.autoDowngrade(this),
                ) { enabled ->
                    AppPrefs.setAutoDowngrade(this, enabled)
                    RdcLog.info("Profile", "auto_downgrade", "enabled=$enabled")
                    selectTab(TAB_PROFILE)
                    dialog.dismiss()
                },
            )
        }
        RdcLog.info("Profile", "panel_network")
    }

    private fun showSecurityPanel() {
        showPanel("安全") { column, dialog ->
            column.addView(
                settingHeader(
                    "剪贴板同步",
                    "会话中双向同步文本剪贴板",
                    enabled = true,
                    checked = AppPrefs.clipboardSync(this),
                ) { enabled ->
                    AppPrefs.setClipboardSync(this, enabled)
                    RdcLog.info("Profile", "clipboard_sync", "enabled=$enabled")
                    selectTab(TAB_PROFILE)
                    dialog.dismiss()
                },
            )
            column.addView(
                note("新设备自动确认：未登录不可配置，当前为关闭（每次都需要连接码）。", 9.5f, RdcColor.FAINT)
                    .apply { setPadding(0, dp(10), 0, 0) },
            )
        }
        RdcLog.info("Profile", "panel_security")
    }

    private fun showNotifyPanel() {
        val unread = if (AppPrefs.noticeRead(this)) 0 else 1
        showPanel("通知") { column, dialog ->
            column.addView(note("未读系统通知：$unread 条。会话消息需登录后同步。", 10f, RdcColor.FAINT))
            column.addView(
                actionView("标记全部已读", 11.5f, RdcColor.TEXT, RdcColor.PANEL_3, 10, RdcColor.LINE_STRONG, 40, RdcColor.PRESS)
                    .apply {
                        setOnClickListener {
                            markAllRead()
                            dialog.dismiss()
                        }
                        layoutParams = LinearLayout.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            dp(40),
                        ).apply { topMargin = dp(14) }
                    },
            )
        }
        RdcLog.info("Profile", "panel_notify")
    }

    private fun showAboutPanel() {
        showPanel("关于远程桌面") { column, _ ->
            column.addView(labelView("版本 ${versionName()}", 12f, RdcColor.TEXT))
            column.addView(note(getString(R.string.profile_role_note), 9.5f, RdcColor.FAINT))
            column.addView(
                settingHeader(
                    "调试日志",
                    "仅本机诊断使用，不上传",
                    enabled = true,
                    checked = DebugSwitch.enabled,
                ) { enabled ->
                    if (DebugSwitch.enabled != enabled) {
                        DebugSwitch.toggle()
                    }
                    RdcLog.info("Profile", "debug_switch", "enabled=$enabled")
                },
            )
            column.addView(
                actionView("权限检查", 11.5f, RdcColor.TEXT, RdcColor.PANEL_3, 10, RdcColor.LINE_STRONG, 40, RdcColor.PRESS)
                    .apply {
                        setOnClickListener {
                            startActivity(Intent(this@MainActivity, PermissionActivity::class.java))
                        }
                        layoutParams = LinearLayout.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            dp(40),
                        ).apply { topMargin = dp(12) }
                    },
            )
            column.addView(
                actionView("导出诊断（脱敏）", 11.5f, RdcColor.TEXT, RdcColor.PANEL_3, 10, RdcColor.LINE_STRONG, 40, RdcColor.PRESS)
                    .apply {
                        setOnClickListener {
                            exportLauncher.launch("诊断-${fileStamp()}.txt")
                            RdcLog.info("Profile", "diagnostics_requested")
                        }
                        layoutParams = LinearLayout.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            dp(40),
                        ).apply { topMargin = dp(8) }
                    },
            )
        }
        RdcLog.info("Profile", "panel_about")
    }

    private fun showUpdatePanel() {
        showPanel("检查更新") { column, _ ->
            column.addView(labelView("当前版本 ${versionName()}", 12f, RdcColor.TEXT))
            column.addView(
                note("更新服务尚未接入，无法在线检查。界面与协议按当前本地版本运行。", 10f, RdcColor.FAINT),
            )
            column.addView(
                actionView("重新检查", 11.5f, RdcColor.ACCENT_STRONG, RdcColor.ACCENT_SOFT, 10, RdcColor.ACCENT, 40, RdcColor.PANEL_3)
                    .apply {
                        setOnClickListener { toast("更新服务未接入，当前为本地最新版本") }
                        layoutParams = LinearLayout.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            dp(40),
                        ).apply { topMargin = dp(14) }
                    },
            )
        }
        RdcLog.info("Profile", "panel_update")
    }

    private fun copyToClipboard(text: String, label: String) {
        val manager = getSystemService(ClipboardManager::class.java)
        manager.setPrimaryClip(ClipData.newPlainText(label, text))
        toast("已复制$label")
    }

    /** 长按菜单: 只列出可复制的非空字段, 文案全中文; 复制后给中文轻提示. */
    private fun attachCopyMenu(view: View, entries: List<Pair<String, String>>) {
        val available = entries.filter { it.second.isNotBlank() }
        if (available.isEmpty()) {
            return
        }
        view.isLongClickable = true
        view.setOnLongClickListener {
            AlertDialog.Builder(this)
                .setTitle("复制内容")
                .setItems(available.map { "复制${it.first}" }.toTypedArray()) { _, index ->
                    copyToClipboard(available[index].second, available[index].first)
                }
                .setNegativeButton("取消", null)
                .show()
            true
        }
    }

    /** 连接: 参数校验 -> 局域网真实链路 -> 进会话窗; 失败时留在本页并给出中文原因. */
    private fun connect() {
        val address = addressInput.text.toString().trim()
        val peer = peerInput.text.toString().trim()
        val code = codeInput.text.toString().trim()

        connectStatus.setTextColor(RdcColor.MUTED)
        connectStatus.text = getString(R.string.home_connecting)
        connectReasonCopy?.visibility = View.GONE
        connectButton.isEnabled = false
        // 每次提交前恢复默认边框, 避免上一次的错误态残留
        addressInput.roundedBackground(RdcColor.INPUT, 11, RdcColor.LINE_STRONG)
        peerInput.roundedBackground(RdcColor.INPUT, 11, RdcColor.LINE_STRONG)
        codeInput.roundedBackground(RdcColor.INPUT, 11, RdcColor.LINE_STRONG)

        ControlContract.startLanConnection(this, address, peer, code) { result ->
            connectButton.isEnabled = true
            when (result) {
                is LanSession.ConnectResult.Ok -> {
                    connectStatus.setTextColor(RdcColor.OK)
                    connectStatus.text = "已连接 · 正在打开会话"
                    connectReasonCopy?.visibility = View.GONE
                    RdcLog.info("Home", "lan_connected", "session=${result.sessionId.take(8)}")
                    openSession(peer, offline = false, realSessionId = result.sessionId)
                }
                is LanSession.ConnectResult.Fail -> {
                    connectStatus.setTextColor(RdcColor.DANGER)
                    connectStatus.text = result.message
                    connectReasonCopy?.visibility = View.VISIBLE
                    RdcLog.warn("Home", "lan_connect_failed", "message=${result.message}")
                }
            }
        }
    }

    /** 校验失败时把对应输入框边框标红并聚焦, 让用户知道改哪里. */
    private fun markFieldError(errorCode: String?) {
        val target = when (errorCode) {
            "DEVICE_NOT_FOUND" -> peerInput
            "ACCESS_CODE_INVALID" -> codeInput
            else -> return
        }
        target.roundedBackground(RdcColor.INPUT, 11, RdcColor.DANGER)
        target.requestFocus()
    }

    private fun openPreview() {
        connectStatus.setTextColor(RdcColor.MUTED)
        connectStatus.text = getString(R.string.home_preview_note)
        openSession(peerInput.text.toString().trim(), offline = true)
    }

    private fun openSession(peer: String, offline: Boolean, realSessionId: String? = null) {
        val profile = AppPrefs.profile(this)
        val target = Intent(this, SessionActivity::class.java).apply {
            putExtra(SessionActivity.EXTRA_SESSION_ID, if (offline) PREVIEW_SESSION_ID else realSessionId.orEmpty())
            putExtra(SessionActivity.EXTRA_PROFILE, profile)
            putExtra(SessionActivity.EXTRA_OFFLINE_PREVIEW, offline)
            putExtra(SessionActivity.EXTRA_PEER_ID, peer)
        }
        startActivity(target)
        RdcLog.info("Home", "open_session", "offline=$offline profile=$profile tab=$currentTab")
    }

    private fun openAccount() {
        startActivity(Intent(this, AccountActivity::class.java))
        RdcLog.info("Home", "open_account")
    }

    // ---------------------------------------------------------------- 账号状态

    private fun accountSignature(): String = RdcAccountService.session(this)?.accountId ?: GUEST_ACCOUNT

    /** 登录态变化后刷新账号相关界面: 「我的」整页重建, 设备页账号卡就地改字. */
    private fun refreshAccountSurfaces() {
        if (!::pages.isInitialized) {
            return
        }
        val signature = accountSignature()
        if (signature != profileAccountSignature) {
            rebuildProfilePage()
            profileAccountSignature = signature
        }
        val session = RdcAccountService.session(this)
        devicesAccountTitle?.text = when {
            session?.remote == true -> "已登录：${session.name}"
            session != null -> "本地账号：${session.name}"
            else -> "登录后查看设备"
        }
        devicesAccountNote?.text = when {
            session == null -> getString(R.string.devices_no_account_note)
            session.remote && remoteDevicesLoading -> "账号中心 · 正在同步设备列表…"
            session.remote && remoteDevicesError != null -> remoteDevicesError
            session.remote -> "账号中心 · 已同步 ${remoteDeviceItems.size} 台设备"
            else -> "本地账号，不跨设备同步；当前无云端设备。"
        }
        devicesAccountAction?.visibility = if (session == null) View.VISIBLE else View.GONE
        refreshConnectTrusted()
        updateProfileStats()
    }

    private fun rebuildProfilePage() {
        val fresh = buildProfilePage()
        contentHost.removeView(pages[TAB_PROFILE])
        contentHost.addView(
            fresh,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT,
            ),
        )
        pages = pages.toMutableList().also { it[TAB_PROFILE] = fresh }
        fresh.visibility = if (currentTab == TAB_PROFILE) View.VISIBLE else View.GONE
        RdcLog.info("Profile", "account_changed", "logged_in=${accountSignature() != GUEST_ACCOUNT}")
    }

    private fun confirmLogout() {
        AlertDialog.Builder(this)
            .setTitle("退出登录")
            .setMessage("退出后将清除本机登录态，需重新输入口令。")
            .setNegativeButton("取消", null)
            .setPositiveButton("退出") { _, _ ->
                val application = applicationContext
                Thread({
                    val outcome = RdcAccountService.logout(application)
                    RdcLog.info("Profile", "logout", "ok=${outcome.ok}")
                    runOnUiThread {
                        toast(outcome.message)
                        refreshAccountSurfaces()
                        loadRemoteDevices(force = true)
                    }
                }, "account-logout").start()
            }
            .show()
    }

    private fun formatAccountTime(epochMs: Long): String =
        SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.CHINA).format(Date(epochMs))

    // ---------------------------------------------------------------- 诊断导出

    private fun buildDiagnostics(): String {
        val sdf = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.CHINA)
        return buildString {
            appendLine("RDC 控制端诊断导出（脱敏）")
            appendLine("生成时间: ${sdf.format(Date())}")
            appendLine("版本: ${versionName()}")
            appendLine("Android: ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})")
            appendLine("ABI: ${Build.SUPPORTED_ABIS.joinToString(",")}")
            appendLine("默认画质档: ${qualityLabel(AppPrefs.profile(this@MainActivity))}")
            appendLine("自动降档: ${AppPrefs.autoDowngrade(this@MainActivity)}")
            appendLine("剪贴板同步: ${AppPrefs.clipboardSync(this@MainActivity)}")
            appendLine("调试日志: ${DebugSwitch.enabled}")
            appendLine("命令层接入: ${ControlContract.WIRED}")
            appendLine("角色: 仅控制端（不采集屏幕）")
            appendLine("说明: 不含设备编号、连接码、令牌与剪贴板内容。")
        }
    }

    private fun writeDiagnostics(uri: Uri) {
        val text = buildDiagnostics()
        try {
            contentResolver.openOutputStream(uri)?.use { out ->
                out.write(text.toByteArray())
            }
            toast(getString(R.string.profile_diagnostics_done))
            RdcLog.info("Profile", "diagnostics_exported", "bytes=${text.toByteArray().size}")
        } catch (error: IOException) {
            toast(getString(R.string.profile_diagnostics_failed))
            RdcLog.error("Profile", "diagnostics_export_failed", "reason=${error.message}")
        }
    }

    private fun versionName(): String {
        val info = packageManager.getPackageInfo(packageName, 0)
        return info.versionName ?: "—"
    }

    private fun fileStamp(): String =
        SimpleDateFormat("yyyyMMdd-HHmmss", Locale.CHINA).format(Date())

    private fun timestamp(): String =
        SimpleDateFormat("MM-dd HH:mm", Locale.CHINA).format(Date())

    private fun toast(text: String) {
        android.widget.Toast.makeText(this, text, android.widget.Toast.LENGTH_SHORT).show()
    }

    /** 底部导航单元格引用. */
    private data class TabCell(
        val root: LinearLayout,
        val icon: ImageView,
        val label: TextView,
        val dot: View,
    )

    companion object {
        private const val KEY_TAB = "current_tab"
        private const val KEY_SCROLL = "scroll_"
        private const val TAB_CONNECT = 0
        private const val TAB_DEVICES = 1
        private const val TAB_MESSAGES = 2
        private const val TAB_PROFILE = 3

        private const val FILTER_ALL = 0
        private const val FILTER_SESSION = 1
        private const val FILTER_BINDING = 2
        private const val FILTER_APPROVAL = 3
        private const val FILTER_GRANT = 4
        private const val FILTER_ACCOUNT = 5

        private const val FILTER_DEVICE_ALL = 0
        private const val FILTER_DEVICE_ONLINE = 1
        private const val FILTER_DEVICE_TRUSTED = 2

        private const val DEVICES_POLL_MS = 15_000L

        private const val PREVIEW_SESSION_ID = "preview"
        private const val ACCESS_CODE_LENGTH = 6
        private const val PEER_ID_MAX_LENGTH = 24
        private const val GUEST_ACCOUNT = "guest"
    }
}
