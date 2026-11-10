// ==========================================================================================
// 文件        : RdcTheme.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 控制端界面的颜色令牌与通用控件样式, 与 docs/design/ui-v1 深色主题对齐
// 架构定位    : 界面层样式常量, 不含业务逻辑
// 核心功能    : 深色配色、圆角背景、文本控件构造
// 职责边界    : 不读业务状态、不发命令、不持有界面状态
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34)
// 依赖关系    : 被 MainActivity、SessionActivity、AccountActivity、PermissionActivity 使用
// ==========================================================================================

package com.rdc.remote

import android.content.Context
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.View
import android.widget.TextView

/** 深色主题令牌, 取自 docs/design/ui-v1 的 :root 变量. */
internal object RdcColor {
    val BG = Color.parseColor("#0B0E13")
    val PANEL = Color.parseColor("#121820")
    val PANEL_2 = Color.parseColor("#0F141B")
    val PANEL_3 = Color.parseColor("#1A212B")
    val INPUT = Color.parseColor("#0E131A")
    val LINE = Color.parseColor("#222B37")
    val LINE_STRONG = Color.parseColor("#2E3947")
    val TEXT = Color.parseColor("#E7EDF6")
    val MUTED = Color.parseColor("#8B98AA")
    val FAINT = Color.parseColor("#5E6B7D")
    val ACCENT = Color.parseColor("#4D8DFF")
    val ACCENT_STRONG = Color.parseColor("#9DC0FF")
    val ACCENT_SOFT = Color.parseColor("#244D8DFF")
    val OK = Color.parseColor("#3FC996")
    val WARN = Color.parseColor("#E0A955")
    val DANGER = Color.parseColor("#EA6A6A")
    val SURFACE = Color.parseColor("#091018")
    val OVERLAY = Color.parseColor("#CC080C12")
}

/** dp 转像素, 统一以当前屏幕密度换算. */
internal fun Context.dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

/** 给视图设置圆角纯色背景, 可选描边. */
internal fun View.roundedBackground(color: Int, radiusDp: Int, strokeColor: Int? = null) {
    background = GradientDrawable().apply {
        shape = GradientDrawable.RECTANGLE
        setColor(color)
        cornerRadius = context.dp(radiusDp).toFloat()
        if (strokeColor != null) {
            setStroke(context.dp(1), strokeColor)
        }
    }
}

/** 快捷构造文本视图. */
internal fun Context.labelView(
    text: String,
    sizeSp: Float,
    color: Int,
    gravity: Int = Gravity.START,
): TextView = TextView(this).apply {
    this.text = text
    textSize = sizeSp
    setTextColor(color)
    this.gravity = gravity
}

/** 可点击的胶囊/主按钮, 以 TextView 承载, 避免系统按钮默认内边距破坏设计稿尺寸. */
internal fun Context.actionView(
    text: String,
    sizeSp: Float,
    textColor: Int,
    background: Int,
    radiusDp: Int,
    strokeColor: Int? = null,
    heightDp: Int = 40,
): TextView = TextView(this).apply {
    this.text = text
    textSize = sizeSp
    setTextColor(textColor)
    gravity = Gravity.CENTER
    isClickable = true
    isFocusable = true
    roundedBackground(background, radiusDp, strokeColor)
    layoutParams = android.view.ViewGroup.LayoutParams(
        android.view.ViewGroup.LayoutParams.MATCH_PARENT,
        dp(heightDp),
    )
}
