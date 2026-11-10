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

import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.text.InputFilter
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
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
    private lateinit var connectStatus: TextView
    private lateinit var connectButton: TextView
    private lateinit var recentList: LinearLayout

    private lateinit var messageList: LinearLayout
    private lateinit var messageEmpty: TextView
    private lateinit var messageChips: LinearLayout
    private var messageFilter = FILTER_ALL

    private lateinit var profileChips: LinearLayout

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
        root.addView(nav, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(58)))
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
        refreshRecent()
        refreshMessages()
        refreshBadge()
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
    }

    // ---------------------------------------------------------------- 连接页

    private fun buildConnectPage(): ScrollView {
        val column = pageColumn()
        column.addView(buildBrandRow())
        column.addView(
            TextView(this).apply {
                text = getString(R.string.home_title)
                textSize = 25f
                setTextColor(RdcColor.TEXT)
                setPadding(0, dp(16), 0, dp(4))
            },
        )
        column.addView(note(getString(R.string.home_sub), 11.5f, RdcColor.MUTED))
        column.addView(buildLocalIdCard())

        column.addView(fieldLabel(getString(R.string.home_peer_label)))
        peerInput = EditText(this).apply {
            hint = getString(R.string.home_peer_hint)
            setTextColor(RdcColor.TEXT)
            setHintTextColor(RdcColor.FAINT)
            textSize = 12.5f
            typeface = Typeface.MONOSPACE
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            filters = arrayOf(InputFilter.LengthFilter(PEER_ID_MAX_LENGTH))
            setSingleLine()
            setPadding(dp(12), 0, dp(12), 0)
            roundedBackground(RdcColor.INPUT, 11, RdcColor.LINE_STRONG)
        }
        column.addView(peerInput, fieldParams())

        column.addView(fieldLabel(getString(R.string.home_code_label)))
        codeInput = EditText(this).apply {
            hint = getString(R.string.home_code_hint)
            setTextColor(RdcColor.TEXT)
            setHintTextColor(RdcColor.FAINT)
            textSize = 12.5f
            typeface = Typeface.MONOSPACE
            inputType = InputType.TYPE_CLASS_NUMBER
            filters = arrayOf(InputFilter.LengthFilter(ACCESS_CODE_LENGTH))
            setSingleLine()
            setPadding(dp(12), 0, dp(12), 0)
            roundedBackground(RdcColor.INPUT, 11, RdcColor.LINE_STRONG)
        }
        column.addView(codeInput, fieldParams())

        connectButton = actionView(
            getString(R.string.home_connect),
            13.5f,
            Color.WHITE,
            RdcColor.ACCENT,
            12,
            heightDp = 47,
        ).apply {
            setOnClickListener { connect() }
        }
        column.addView(
            connectButton,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(6) },
        )

        column.addView(link(getString(R.string.home_perm_entry)) {
            startActivity(Intent(this, PermissionActivity::class.java))
            RdcLog.info("Home", "open_permissions")
        })
        column.addView(link(getString(R.string.home_account_entry)) { openAccount() })

        column.addView(buildRecent())
        column.addView(link(getString(R.string.home_preview_entry)) { openPreview() })

        connectStatus = labelView(getString(R.string.home_status_idle), 12.5f, RdcColor.MUTED).apply {
            setPadding(0, dp(10), 0, 0)
            // 连接结果变化时让读屏主动播报
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        }
        column.addView(connectStatus)
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
                textSize = 14f
                setTextColor(Color.WHITE)
                gravity = Gravity.CENTER
                background = GradientDrawable().apply {
                    shape = GradientDrawable.RECTANGLE
                    cornerRadius = dp(8).toFloat()
                    colors = intArrayOf(0xFF5B97FF.toInt(), 0xFF2F63D8.toInt())
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

    private fun buildLocalIdCard(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(12), dp(10), dp(12), dp(12))
        roundedBackground(RdcColor.PANEL_2, 11, RdcColor.LINE)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(16) }

        addView(labelView(getString(R.string.home_local_id_label), 10.5f, RdcColor.MUTED))
        addView(
            TextView(this@MainActivity).apply {
                text = ControlContract.localDeviceId()
                textSize = 15f
                typeface = Typeface.MONOSPACE
                setTextColor(RdcColor.TEXT)
                setPadding(0, dp(4), 0, dp(2))
            },
        )
        addView(note(getString(R.string.home_local_id_note), 9.5f, RdcColor.FAINT))
    }

    private fun buildRecent(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        )

        val head = LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(20), 0, dp(6))
        }
        head.addView(
            labelView(getString(R.string.home_recent), 11f, RdcColor.MUTED).apply {
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            },
        )
        head.addView(note(getString(R.string.home_recent_contract), 9.5f, RdcColor.FAINT))
        addView(head)

        recentList = LinearLayout(this@MainActivity).apply { orientation = LinearLayout.VERTICAL }
        addView(recentList)
    }

    // ---------------------------------------------------------------- 设备页

    private fun buildDevicesPage(): ScrollView {
        val column = pageColumn()
        column.addView(pageHeader(getString(R.string.tab_devices), getString(R.string.devices_sub)))

        val accountCard = card()
        accountCard.addView(
            labelView(getString(R.string.devices_no_account), 12f, RdcColor.TEXT),
        )
        accountCard.addView(note(getString(R.string.devices_no_account_note), 10f, RdcColor.FAINT))
        accountCard.addView(
            actionView(
                getString(R.string.devices_go_profile),
                11.5f,
                RdcColor.TEXT,
                RdcColor.PANEL_3,
                9,
                RdcColor.LINE_STRONG,
                36,
            ).apply {
                setOnClickListener { selectTab(TAB_PROFILE) }
            },
        )
        column.addView(accountCard)

        column.addView(sectionRow(getString(R.string.devices_list_title), getString(R.string.devices_list_contract)))

        val empty = card()
        empty.addView(labelView(getString(R.string.devices_empty), 12f, RdcColor.TEXT))
        empty.addView(note(getString(R.string.devices_empty_note), 10f, RdcColor.FAINT))
        column.addView(empty)

        val legend = card()
        legend.addView(labelView(getString(R.string.devices_legend_title), 10.5f, RdcColor.MUTED))
        val legendRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(8), 0, 0)
        }
        legendRow.addView(legendItem(getString(R.string.devices_state_online), RdcColor.OK), legendParams(0))
        legendRow.addView(legendItem(getString(R.string.devices_state_offline), RdcColor.MUTED), legendParams(10))
        legendRow.addView(legendItem(getString(R.string.devices_state_unknown), RdcColor.FAINT), legendParams(10))
        legend.addView(legendRow)
        column.addView(legend)

        val autoConfirm = card()
        autoConfirm.addView(
            settingHeader(
                getString(R.string.devices_auto_confirm),
                getString(R.string.devices_auto_confirm_note),
                enabled = false,
                checked = false,
                onChanged = { RdcLog.info("Devices", "auto_confirm_changed") },
            ),
        )
        autoConfirm.addView(
            note(getString(R.string.devices_auto_confirm_locked), 9.5f, RdcColor.FAINT).apply {
                setPadding(0, dp(6), 0, 0)
            },
        )
        column.addView(autoConfirm)
        return pageScroll(column)
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
        column.addView(pageHeader(getString(R.string.tab_messages), getString(R.string.messages_sub)))

        val head = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(10), 0, 0)
        }
        head.addView(
            labelView(getString(R.string.messages_mark_all), 10.5f, RdcColor.ACCENT).apply {
                isClickable = true
                isFocusable = true
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                setOnClickListener { markAllRead() }
            },
        )
        column.addView(head)

        messageChips = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(10), 0, 0)
        }
        val filters = listOf(
            FILTER_ALL to R.string.messages_filter_all,
            FILTER_REQUEST to R.string.messages_filter_request,
            FILTER_AUTH to R.string.messages_filter_auth,
            FILTER_SYSTEM to R.string.messages_filter_system,
        )
        for ((value, labelRes) in filters) {
            val chip = TextView(this).apply {
                text = getString(labelRes)
                textSize = 10f
                setTextColor(if (value == messageFilter) RdcColor.ACCENT_STRONG else RdcColor.MUTED)
                setPadding(dp(9), dp(5), dp(9), dp(5))
                isClickable = true
                isFocusable = true
                isSelected = value == messageFilter
                contentDescription = describeChip(getString(labelRes), value == messageFilter)
                roundedBackground(
                    if (value == messageFilter) RdcColor.ACCENT_SOFT else RdcColor.PANEL_2,
                    999,
                    if (value == messageFilter) RdcColor.ACCENT else RdcColor.LINE_STRONG,
                )
                setOnClickListener { setMessageFilter(value) }
            }
            messageChips.addView(
                chip,
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

        messageEmpty = note(getString(R.string.messages_empty_all), 10f, RdcColor.FAINT).apply {
            setPadding(dp(12), dp(14), dp(12), dp(14))
            roundedBackground(RdcColor.PANEL_2, 11, RdcColor.LINE)
        }
        column.addView(messageEmpty)
        return pageScroll(column)
    }

    private fun setMessageFilter(value: Int) {
        messageFilter = value
        for (index in 0 until messageChips.childCount) {
            val child = messageChips.getChildAt(index)
            val selected = index == filterIndex(value)
            (child as? TextView)?.let {
                it.setTextColor(if (selected) RdcColor.ACCENT_STRONG else RdcColor.MUTED)
                it.roundedBackground(
                    if (selected) RdcColor.ACCENT_SOFT else RdcColor.PANEL_2,
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
        FILTER_REQUEST -> 1
        FILTER_AUTH -> 2
        FILTER_SYSTEM -> 3
        else -> 0
    }

    /** 消息只显示内置系统通知与空态, 不虚构申请或授权记录. */
    private fun refreshMessages() {
        messageList.removeAllViews()
        val unread = !AppPrefs.noticeRead(this)
        val showNotice = messageFilter == FILTER_ALL || messageFilter == FILTER_SYSTEM
        if (showNotice) {
            val card = card()
            val head = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
            }
            head.addView(
                labelView(getString(R.string.messages_notice_title), 12f, RdcColor.TEXT).apply {
                    layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                },
            )
            if (unread) {
                head.addView(
                    TextView(this).apply {
                        text = getString(R.string.messages_unread)
                        textSize = 8.5f
                        setTextColor(Color.WHITE)
                        setPadding(dp(7), dp(2), dp(7), dp(2))
                        roundedBackground(RdcColor.DANGER, 999)
                    },
                )
            }
            card.addView(head)
            card.addView(note(getString(R.string.messages_notice_body), 10f, RdcColor.FAINT))
            card.addView(note(timestamp(), 9f, RdcColor.FAINT))
            card.isClickable = true
            card.setOnClickListener { markNoticeRead() }
            messageList.addView(card)
        }
        messageEmpty.visibility = if (showNotice) View.GONE else View.VISIBLE
        messageEmpty.text = getString(
            when (messageFilter) {
                FILTER_REQUEST -> R.string.messages_empty_request
                FILTER_AUTH -> R.string.messages_empty_auth
                else -> R.string.messages_empty_all
            },
        )
        refreshBadge()
    }

    private fun markNoticeRead() {
        if (AppPrefs.noticeRead(this)) {
            return
        }
        AppPrefs.setNoticeRead(this, true)
        refreshMessages()
        RdcLog.info("Messages", "notice_read")
    }

    private fun markAllRead() {
        AppPrefs.setNoticeRead(this, true)
        refreshMessages()
        RdcLog.info("Messages", "mark_all_read")
    }

    /** 底部导航未读红点: 只统计内置系统通知. */
    private fun refreshBadge() {
        if (!::tabCells.isInitialized) {
            return
        }
        val unread = if (AppPrefs.noticeRead(this)) 0 else 1
        tabCells[TAB_MESSAGES].dot.visibility = if (unread > 0) View.VISIBLE else View.GONE
        tabCells[TAB_MESSAGES].root.contentDescription = describeTab(TAB_MESSAGES, currentTab == TAB_MESSAGES)
    }

    /** 底部导航的无障碍描述: 名称 + 选中态 + 未读提示. */
    private fun describeTab(index: Int, selected: Boolean): String {
        val parts = mutableListOf(tabCells[index].label.text.toString())
        if (selected) {
            parts.add(getString(R.string.a11y_selected))
        }
        if (index == TAB_MESSAGES && !AppPrefs.noticeRead(this)) {
            parts.add(getString(R.string.a11y_unread))
        }
        return parts.joinToString("，")
    }

    // ---------------------------------------------------------------- 我的页

    private fun buildProfilePage(): ScrollView {
        val column = pageColumn()
        column.addView(pageHeader(getString(R.string.tab_profile), getString(R.string.profile_sub)))

        val account = card()
        account.addView(labelView(getString(R.string.profile_account_state), 12f, RdcColor.TEXT))
        account.addView(note(getString(R.string.profile_account_note), 10f, RdcColor.FAINT))
        account.addView(
            actionView(
                getString(R.string.profile_account_entry),
                11.5f,
                RdcColor.TEXT,
                RdcColor.PANEL_3,
                9,
                RdcColor.LINE_STRONG,
                36,
            ).apply { setOnClickListener { openAccount() } },
        )
        column.addView(account)

        column.addView(sectionRow(getString(R.string.profile_section_quality), ""))

        val qualityCard = card()
        qualityCard.addView(labelView(getString(R.string.profile_quality_default), 11.5f, RdcColor.TEXT))
        qualityCard.addView(note(getString(R.string.profile_quality_note), 9.5f, RdcColor.FAINT))
        profileChips = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(8), 0, 0)
        }
        for (item in ControlContract.DIRECT_PROFILES) {
            val chip = TextView(this).apply {
                text = item
                textSize = 10f
                setPadding(dp(9), dp(5), dp(9), dp(5))
                isClickable = true
                isFocusable = true
                setOnClickListener { setDefaultProfile(item) }
            }
            profileChips.addView(
                chip,
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ).apply { rightMargin = dp(6) },
            )
        }
        qualityCard.addView(profileChips)
        column.addView(qualityCard)
        refreshProfileChips()

        val downgrade = card()
        downgrade.addView(
            settingHeader(
                getString(R.string.profile_downgrade),
                getString(R.string.profile_downgrade_note),
                enabled = true,
                checked = AppPrefs.autoDowngrade(this),
            ) { enabled -> AppPrefs.setAutoDowngrade(this, enabled) },
        )
        column.addView(downgrade)

        val clipboard = card()
        clipboard.addView(
            settingHeader(
                getString(R.string.profile_clipboard),
                getString(R.string.profile_clipboard_note),
                enabled = true,
                checked = AppPrefs.clipboardSync(this),
            ) { enabled -> AppPrefs.setClipboardSync(this, enabled) },
        )
        column.addView(clipboard)

        column.addView(sectionRow(getString(R.string.profile_section_diagnostics), ""))

        val diagnostics = card()
        diagnostics.addView(labelView(getString(R.string.profile_diagnostics_export), 11.5f, RdcColor.TEXT))
        diagnostics.addView(note(getString(R.string.profile_diagnostics_note), 9.5f, RdcColor.FAINT))
        diagnostics.addView(
            actionView(
                getString(R.string.profile_diagnostics_action),
                11.5f,
                RdcColor.TEXT,
                RdcColor.PANEL_3,
                9,
                RdcColor.LINE_STRONG,
                36,
            ).apply {
                setOnClickListener {
                    exportLauncher.launch("rdc-diagnostics-${fileStamp()}.txt")
                    RdcLog.info("Profile", "diagnostics_requested")
                }
            },
        )
        column.addView(diagnostics)

        column.addView(sectionRow(getString(R.string.profile_section_about), ""))

        val about = card()
        about.addView(labelView(getString(R.string.profile_version, versionName()), 11.5f, RdcColor.TEXT))
        about.addView(note(getString(R.string.profile_role_note), 9.5f, RdcColor.FAINT))
        about.addView(
            settingHeader(
                getString(R.string.profile_debug_log),
                getString(R.string.profile_debug_log_note),
                enabled = true,
                checked = DebugSwitch.enabled,
            ) { enabled ->
                if (DebugSwitch.enabled != enabled) {
                    DebugSwitch.toggle()
                }
                RdcLog.info("Profile", "debug_switch", "enabled=$enabled")
            },
        )
        about.addView(
            link(getString(R.string.profile_permission_entry)) {
                startActivity(Intent(this, PermissionActivity::class.java))
            },
        )
        column.addView(about)

        column.addView(
            note(getString(R.string.profile_footnote), 9.5f, RdcColor.FAINT).apply {
                gravity = Gravity.CENTER
                setPadding(0, dp(16), 0, 0)
            },
        )
        return pageScroll(column)
    }

    private fun setDefaultProfile(item: String) {
        AppPrefs.setProfile(this, item)
        refreshProfileChips()
        RdcLog.info("Profile", "default_profile", "profile=$item")
    }

    private fun refreshProfileChips() {
        if (!::profileChips.isInitialized) {
            return
        }
        val selectedValue = AppPrefs.profile(this)
        for (index in 0 until profileChips.childCount) {
            val chip = profileChips.getChildAt(index) as? TextView ?: continue
            val selected = chip.text.toString() == selectedValue
            chip.setTextColor(if (selected) RdcColor.ACCENT_STRONG else RdcColor.MUTED)
            chip.roundedBackground(
                if (selected) RdcColor.ACCENT_SOFT else RdcColor.PANEL_2,
                999,
                if (selected) RdcColor.ACCENT else RdcColor.LINE_STRONG,
            )
            chip.isSelected = selected
            chip.contentDescription = describeChip(chip.text.toString(), selected)
        }
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
        setPadding(dp(20), dp(14), dp(20), dp(28))
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
                    textSize = 22f
                    setTextColor(RdcColor.TEXT)
                },
            )
            texts.addView(note(subtitle, 10f, RdcColor.MUTED))
            addView(texts)
            addView(roleBadge())
        }

    private fun roleBadge(): TextView = TextView(this).apply {
        text = getString(R.string.role_badge)
        textSize = 9f
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
        roundedBackground(RdcColor.PANEL, 11, RdcColor.LINE)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(10) }
    }

    private fun fieldLabel(text: String): TextView = labelView(text, 10.5f, RdcColor.MUTED).apply {
        setPadding(0, dp(13), 0, dp(6))
    }

    private fun fieldParams() = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        dp(45),
    )

    private fun link(text: String, onClick: () -> Unit): TextView =
        labelView(text, 11.5f, RdcColor.ACCENT, Gravity.CENTER).apply {
            setPadding(0, dp(12), 0, 0)
            isClickable = true
            isFocusable = true
            setOnClickListener { onClick() }
        }

    private fun note(text: String, sizeSp: Float, color: Int): TextView =
        labelView(text, sizeSp, color).apply { setPadding(0, dp(6), 0, 0) }

    // ---------------------------------------------------------------- 最近连接与跳转

    /** 最近连接来自命令返回值; 未接入时只显示空态, 不虚构会话. */
    private fun refreshRecent() {
        if (!::recentList.isInitialized) {
            return
        }
        recentList.removeAllViews()
        val result = ControlContract.listSessions()
        if (result.ok && result.data["count"] != null) {
            recentList.addView(note(getString(R.string.home_recent_ready), 10f, RdcColor.MUTED))
            return
        }
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(8), 0, dp(8))
        }
        row.addView(labelView(getString(R.string.home_recent_empty), 11.5f, RdcColor.TEXT))
        row.addView(note(getString(R.string.home_recent_empty_note), 9.5f, RdcColor.FAINT))
        recentList.addView(row)
        RdcLog.debug("Home", "recent_empty", "reason=${result.message}")
    }

    /** 连接: 参数校验 -> start_connection -> 进会话窗; 参数不合法时留在本页. */
    private fun connect() {
        val peer = peerInput.text.toString().trim()
        val code = codeInput.text.toString().trim()

        connectStatus.setTextColor(RdcColor.MUTED)
        connectStatus.text = getString(R.string.home_connecting)
        connectButton.isEnabled = false
        // 每次提交前恢复默认边框, 避免上一次的错误态残留
        peerInput.roundedBackground(RdcColor.INPUT, 11, RdcColor.LINE_STRONG)
        codeInput.roundedBackground(RdcColor.INPUT, 11, RdcColor.LINE_STRONG)

        val result = ControlContract.startConnection(peer, code)
        connectButton.isEnabled = true
        connectStatus.setTextColor(if (result.errorCode == null) RdcColor.WARN else RdcColor.DANGER)
        connectStatus.text = result.message
        if (result.errorCode != null) {
            markFieldError(result.errorCode)
            RdcLog.warn("Home", "connect_rejected", "error=${result.errorCode}")
            return
        }
        // 命令层未接入时参数已通过校验, 进入会话窗做界面与输入入口验证, 会话窗内明确标注离线预览
        openSession(peer, offline = !result.ok)
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

    private fun openSession(peer: String, offline: Boolean) {
        val profile = AppPrefs.profile(this)
        val target = Intent(this, SessionActivity::class.java).apply {
            putExtra(SessionActivity.EXTRA_SESSION_ID, if (offline) PREVIEW_SESSION_ID else "session")
            putExtra(SessionActivity.EXTRA_PROFILE, profile)
            putExtra(SessionActivity.EXTRA_OFFLINE_PREVIEW, offline)
            putExtra(SessionActivity.EXTRA_PEER_ID, peer)
        }
        startActivity(target)
        RdcLog.info("Home", "open_session", "offline=$offline profile=$profile tab=$currentTab")
    }

    private fun openAccount() {
        startActivity(
            Intent(this, AccountActivity::class.java).apply {
                putExtra(AccountActivity.EXTRA_ENTRY, getString(R.string.home_account_entry))
            },
        )
        RdcLog.info("Home", "open_account")
    }

    // ---------------------------------------------------------------- 诊断导出

    private fun buildDiagnostics(): String {
        val sdf = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.CHINA)
        return buildString {
            appendLine("RDC 控制端诊断导出（脱敏）")
            appendLine("生成时间: ${sdf.format(Date())}")
            appendLine("版本: ${versionName()}")
            appendLine("Android: ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})")
            appendLine("ABI: ${Build.SUPPORTED_ABIS.joinToString(",")}")
            appendLine("默认画质档: ${AppPrefs.profile(this@MainActivity)}")
            appendLine("自动降档: ${AppPrefs.autoDowngrade(this@MainActivity)}")
            appendLine("剪贴板同步: ${AppPrefs.clipboardSync(this@MainActivity)}")
            appendLine("调试日志: ${DebugSwitch.enabled}")
            appendLine("命令层接入: ${ControlContract.WIRED}")
            appendLine("角色: 仅控制端 (media_projection=false)")
            appendLine("说明: 不含设备 ID、连接码、令牌与剪贴板内容。")
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
        private const val FILTER_REQUEST = 1
        private const val FILTER_AUTH = 2
        private const val FILTER_SYSTEM = 3

        private const val PREVIEW_SESSION_ID = "preview"
        private const val ACCESS_CODE_LENGTH = 6
        private const val PEER_ID_MAX_LENGTH = 24
    }
}
