// ==========================================================================================
// 文件        : AccountActivity.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 账号与设备占位页, 只说明命令层未接入并返回连接页
// 架构定位    : Android 界面层占位页, 不含账号业务逻辑
// 核心功能    : 显示入口名称、占位文案与边界说明; 返回连接页
// 职责边界    : 不做登录、不请求网络、不保存任何凭据、不虚构设备列表
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34)
// 依赖关系    : RdcTheme、RdcLog、strings.xml
// ==========================================================================================

package com.rdc.remote

import android.os.Bundle
import android.view.Gravity
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

class AccountActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val entry = intent.getStringExtra(EXTRA_ENTRY) ?: getString(R.string.home_account_entry)

        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(14), dp(20), dp(28))
            setBackgroundColor(RdcColor.BG)
        }

        column.addView(
            labelView(getString(R.string.account_back), 11.5f, RdcColor.ACCENT).apply {
                isClickable = true
                isFocusable = true
                setPadding(0, dp(6), 0, dp(6))
                setOnClickListener { finish() }
            },
        )
        column.addView(
            TextView(this).apply {
                text = entry
                textSize = 22f
                setTextColor(RdcColor.TEXT)
                setPadding(0, dp(14), 0, dp(4))
            },
        )
        column.addView(
            labelView(getString(R.string.account_placeholder), 11.5f, RdcColor.MUTED).apply {
                setPadding(0, 0, 0, dp(14))
            },
        )

        column.addView(
            labelView(getString(R.string.account_scope), 10.5f, RdcColor.FAINT).apply {
                setPadding(dp(12), dp(12), dp(12), dp(12))
                roundedBackground(RdcColor.PANEL_2, 11, RdcColor.LINE)
            },
        )

        column.addView(
            actionView(
                getString(R.string.account_back_to_connect),
                12.5f,
                RdcColor.TEXT,
                RdcColor.PANEL_3,
                10,
                RdcColor.LINE_STRONG,
            ).apply {
                setOnClickListener { finish() }
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ).apply { topMargin = dp(18) }
            },
        )

        column.addView(
            labelView(getString(R.string.account_footnote), 9.5f, RdcColor.FAINT, Gravity.CENTER).apply {
                setPadding(0, dp(16), 0, 0)
            },
        )

        setContentView(ScrollView(this).apply { setBackgroundColor(RdcColor.BG) }.also { it.addView(column) })
        RdcLog.info("Account", "page_ready", "entry=$entry login_wired=false")
    }

    companion object {
        const val EXTRA_ENTRY = "entry"
    }
}
