// ==========================================================================================
// 文件        : AccountActivity.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 账号登录 / 注册页与已登录状态页
// 架构定位    : Android 界面层; 账号请求与存储交给 RdcAccountService, 界面只收集参数并呈现结果
// 核心功能    : 登录、注册、登录态展示、退出登录; 中文错误提示; 不显示口令与令牌原文
// 职责边界    : 不直接发 HTTP、不接触令牌、不调用 Python 命令层、不保存明文口令
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35)
// 依赖关系    : RdcAccountService、RdcTheme、RdcLog、AppPrefs
// ==========================================================================================

package com.rdc.remote

import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputFilter
import android.text.InputType
import android.text.method.PasswordTransformationMethod
import android.view.Gravity
import android.view.ViewGroup
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import java.util.concurrent.Executors

class AccountActivity : AppCompatActivity() {

    private lateinit var column: LinearLayout
    private var statusView: TextView? = null
    private var nameInput: EditText? = null
    private var passwordInput: EditText? = null
    private var submitButton: TextView? = null
    private var modeLoginCell: TextView? = null
    private var modeRegisterCell: TextView? = null

    private var registerMode = false
    private var submitting = false

    /** 口令派生与落盘放到后台线程, 避免阻塞界面. */
    private val worker = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "account-io").apply { isDaemon = true }
    }
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        RdcColor.dark = AppPrefs.darkTheme(this)
        column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(RdcMetrics.PAGE_PADDING, dp(14), RdcMetrics.PAGE_PADDING, dp(28))
            setBackgroundColor(RdcColor.BG)
        }
        setContentView(
            ScrollView(this).apply { setBackgroundColor(RdcColor.BG) }.also { it.addView(column) },
        )
        render()
        RdcLog.info("Account", "page_ready", "logged_in=${RdcAccountService.session(this) != null}")
    }

    override fun onDestroy() {
        worker.shutdownNow()
        super.onDestroy()
    }

    // ------------------------------------------------------------------ 页面骨架

    private fun render() {
        column.removeAllViews()
        statusView = null
        nameInput = null
        passwordInput = null
        submitButton = null
        modeLoginCell = null
        modeRegisterCell = null

        column.addView(
            labelView(getString(R.string.account_back), 11.5f, RdcColor.ACCENT).apply {
                isClickable = true
                isFocusable = true
                setPadding(0, dp(6), 0, dp(6))
                setOnClickListener { finish() }
            },
        )

        val session = RdcAccountService.session(this)
        if (session == null) {
            renderForm()
        } else {
            // 已登录: 账号与设备信息统一在「我的」页展示, 不再单独占一屏
            finish()
        }
    }

    private fun renderForm() {
        column.addView(
            TextView(this).apply {
                text = if (registerMode) "注册账号" else "登录"
                textSize = RdcText.METRIC
                setTextColor(RdcColor.TEXT)
                setPadding(0, dp(14), 0, dp(4))
            },
        )
        column.addView(
            note(
                "账号中心 · 登录后可在设备页查看同一账号下的其他设备。",
                11f,
                RdcColor.MUTED,
            ),
        )

        val modes = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(38),
            ).apply { topMargin = dp(14) }
        }
        modeLoginCell = modeCell("登录") { registerMode = false; applyMode() }
        modeRegisterCell = modeCell("注册") { registerMode = true; applyMode() }
        modes.addView(
            modeLoginCell,
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f).apply {
                rightMargin = dp(4)
            },
        )
        modes.addView(
            modeRegisterCell,
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f).apply {
                leftMargin = dp(4)
            },
        )
        column.addView(modes)

        val form = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            roundedBackground(RdcColor.PANEL, 14, RdcColor.LINE)
            setPadding(dp(14), dp(4), dp(14), dp(14))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(12) }
        }
        form.addView(fieldLabel("用户名"))
        nameInput = EditText(this).apply {
            hint = "3 至 64 位字母、数字或 _ . @ -"
            setTextColor(RdcColor.TEXT)
            setHintTextColor(RdcColor.FAINT)
            textSize = RdcText.BODY_STRONG
            typeface = Typeface.MONOSPACE
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            filters = arrayOf(InputFilter.LengthFilter(64))
            setSingleLine()
            setPadding(dp(12), 0, dp(12), 0)
            roundedBackground(RdcColor.INPUT, 11, RdcColor.LINE_STRONG)
        }
        form.addView(nameInput, fieldParams())

        form.addView(fieldLabel("口令"))
        passwordInput = EditText(this).apply {
            hint = "至少 8 位"
            setTextColor(RdcColor.TEXT)
            setHintTextColor(RdcColor.FAINT)
            textSize = RdcText.BODY_STRONG
            typeface = Typeface.MONOSPACE
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            filters = arrayOf(InputFilter.LengthFilter(64))
            setSingleLine()
            // setSingleLine 会把转换器换成 SingleLineTransformationMethod, 必须显式恢复口令掩码
            transformationMethod = PasswordTransformationMethod.getInstance()
            setPadding(dp(12), 0, dp(12), 0)
            roundedBackground(RdcColor.INPUT, 11, RdcColor.LINE_STRONG)
        }
        form.addView(passwordInput, fieldParams())
        form.addView(
            note(
                if (registerMode) {
                    "账号注册成功后返回 account_id，不会自动登录。"
                } else {
                    "口令只用于本次校验，不保存在本机。"
                },
                9.5f,
                RdcColor.FAINT,
            ).apply { setPadding(0, dp(2), 0, dp(12)) },
        )

        submitButton = actionView(
            if (registerMode) "注册" else "登录",
            12.5f,
            RdcColor.TEXT,
            RdcColor.ACCENT,
            10,
            heightDp = 42,
            pressedBackground = RdcColor.ACCENT_PRESS,
        ).apply {
            setTextColor(android.graphics.Color.WHITE)
            setOnClickListener { submit() }
        }
        form.addView(submitButton)
        column.addView(form)

        statusView = note("", 10.5f, RdcColor.MUTED).apply {
            setPadding(0, dp(12), 0, 0)
        }
        column.addView(statusView)

        column.addView(
            note(
                "账号由跨端账号中心校验；登录状态保存在本机，杀进程重开后仍保持。",
                9.5f,
                RdcColor.FAINT,
            ).apply {
                gravity = Gravity.CENTER
                setPadding(0, dp(16), 0, 0)
            },
        )
        applyMode()
    }

    private fun modeCell(text: String, onClick: () -> Unit): TextView = TextView(this).apply {
        this.text = text
        textSize = RdcText.BODY
        gravity = Gravity.CENTER
        isClickable = true
        isFocusable = true
        pressableBackground(RdcColor.PANEL_3, RdcColor.PRESS, 10)
        setOnClickListener { onClick() }
    }

    /** 只刷新登录/注册的模式样式与按钮文案, 不重建输入框, 避免丢失已输入内容. */
    private fun applyMode() {
        modeLoginCell?.let { cell ->
            val selected = !registerMode
            cell.setTextColor(if (selected) RdcColor.ACCENT_STRONG else RdcColor.MUTED)
            cell.pressableBackground(
                if (selected) RdcColor.ACCENT_SOFT else RdcColor.PANEL_3,
                RdcColor.PRESS,
                10,
                if (selected) RdcColor.ACCENT else RdcColor.LINE,
            )
        }
        modeRegisterCell?.let { cell ->
            val selected = registerMode
            cell.setTextColor(if (selected) RdcColor.ACCENT_STRONG else RdcColor.MUTED)
            cell.pressableBackground(
                if (selected) RdcColor.ACCENT_SOFT else RdcColor.PANEL_3,
                RdcColor.PRESS,
                10,
                if (selected) RdcColor.ACCENT else RdcColor.LINE,
            )
        }
        submitButton?.text = if (registerMode) "注册" else "登录"
    }

    // ------------------------------------------------------------------ 动作

    private fun submit() {
        if (submitting) {
            return
        }
        val name = nameInput?.text?.toString()?.trim().orEmpty()
        val password = passwordInput?.text?.toString().orEmpty()
        if (name.isEmpty()) {
            showStatus("请输入用户名", false)
            return
        }
        if (password.isEmpty()) {
            showStatus("请输入口令", false)
            return
        }

        submitting = true
        submitButton?.isEnabled = false
        showStatus(if (registerMode) "正在创建账号…" else "正在登录…", null)
        val register = registerMode
        val application = applicationContext
        worker.execute {
            val outcome = if (register) {
                RdcAccountService.register(application, name, password)
            } else {
                RdcAccountService.login(application, name, password)
            }
            main.post { onOutcome(outcome, register) }
        }
    }

    private fun onOutcome(outcome: RdcAccount.Outcome, wasRegister: Boolean) {
        submitting = false
        submitButton?.isEnabled = true
        if (!outcome.ok) {
            RdcLog.warn(
                "Account",
                if (wasRegister) "register_failed" else "login_failed",
                "error_code=${outcome.errorCode ?: "none"}",
            )
            showStatus(outcome.message, false)
            return
        }
        if (wasRegister) {
            RdcLog.info("Account", "register_done")
            showStatus("账号已创建，请登录", true)
            registerMode = false
            applyMode()
            passwordInput?.setText("")
            passwordInput?.requestFocus()
            return
        }
        RdcLog.info("Account", "login_done")
        showStatus("登录成功", true)
        main.postDelayed({ finish() }, 350)
    }

    private fun showStatus(text: String, ok: Boolean?) {
        val view = statusView ?: return
        view.text = text
        view.setTextColor(
            when (ok) {
                true -> RdcColor.OK
                false -> RdcColor.DANGER
                null -> RdcColor.MUTED
            },
        )
    }

    // ------------------------------------------------------------------ 小工具

    private fun fieldLabel(text: String): TextView = labelView(text, 10f, RdcColor.MUTED).apply {
        setPadding(0, dp(10), 0, dp(6))
    }

    private fun fieldParams(): LinearLayout.LayoutParams = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        dp(42),
    )

    private fun note(text: String, sizeSp: Float, color: Int): TextView = TextView(this).apply {
        this.text = text
        textSize = sizeSp
        setTextColor(color)
        setLineSpacing(dp(3).toFloat(), 1f)
    }
}
