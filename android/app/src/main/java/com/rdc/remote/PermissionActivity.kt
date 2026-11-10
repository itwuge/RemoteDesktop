// ==========================================================================================
// 文件        : PermissionActivity.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 首次使用权限引导页, 说明 3 项系统授权与「仅控制端」边界
// 架构定位    : Android 界面说明页, 不申请权限、不调命令
// 核心功能    : 展示安装未知应用、通知、网络与前台服务说明; 返回连接页
// 职责边界    : 不申请 MediaProjection、不做屏幕采集、不做无障碍注入
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

class PermissionActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(14), dp(20), dp(28))
            setBackgroundColor(RdcColor.BG)
        }

        column.addView(
            labelView(getString(R.string.permission_back), 11.5f, RdcColor.ACCENT).apply {
                isClickable = true
                isFocusable = true
                setPadding(0, dp(6), 0, dp(6))
                setOnClickListener { finish() }
            },
        )
        column.addView(
            TextView(this).apply {
                text = getString(R.string.permission_title)
                textSize = 22f
                setTextColor(RdcColor.TEXT)
                setPadding(0, dp(14), 0, dp(4))
            },
        )
        column.addView(
            labelView(getString(R.string.permission_sub), 11.5f, RdcColor.MUTED).apply {
                setPadding(0, 0, 0, dp(16))
            },
        )

        column.addView(step(1, R.string.permission_step_install, R.string.permission_step_install_note))
        column.addView(step(2, R.string.permission_step_notify, R.string.permission_step_notify_note))
        column.addView(step(3, R.string.permission_step_network, R.string.permission_step_network_note))

        column.addView(
            labelView(getString(R.string.permission_control_only), 10.5f, RdcColor.WARN).apply {
                setPadding(dp(12), dp(12), dp(12), dp(12))
                roundedBackground(RdcColor.PANEL_2, 11, RdcColor.LINE)
                val params = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                )
                params.topMargin = dp(16)
                layoutParams = params
            },
        )

        column.addView(
            actionView(
                getString(R.string.permission_done),
                13.5f,
                android.graphics.Color.WHITE,
                RdcColor.ACCENT,
                12,
                heightDp = 47,
            ).apply {
                setOnClickListener { finish() }
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ).apply { topMargin = dp(18) }
            },
        )

        column.addView(
            labelView(getString(R.string.permission_footnote), 9.5f, RdcColor.FAINT, Gravity.CENTER).apply {
                setPadding(0, dp(16), 0, 0)
            },
        )

        setContentView(ScrollView(this).apply { setBackgroundColor(RdcColor.BG) }.also { it.addView(column) })
        RdcLog.info("Permission", "page_ready", "control_only=true media_projection=false")
    }

    private fun step(index: Int, titleRes: Int, noteRes: Int): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(dp(12), dp(12), dp(12), dp(12))
            roundedBackground(RdcColor.PANEL, 11, RdcColor.LINE)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(8) }

            addView(
                TextView(this@PermissionActivity).apply {
                    text = getString(R.string.permission_step_index, index)
                    textSize = 11f
                    setTextColor(RdcColor.ACCENT)
                    gravity = Gravity.CENTER
                    roundedBackground(RdcColor.ACCENT_SOFT, 999)
                    layoutParams = LinearLayout.LayoutParams(dp(22), dp(22))
                },
            )

            val body = LinearLayout(this@PermissionActivity).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(10), 0, 0, 0)
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            }
            body.addView(labelView(getString(titleRes), 12f, RdcColor.TEXT))
            body.addView(
                labelView(getString(noteRes), 10f, RdcColor.FAINT).apply {
                    setPadding(0, dp(3), 0, 0)
                },
            )
            addView(body)
        }
}
