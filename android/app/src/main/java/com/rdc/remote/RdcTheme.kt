// ==========================================================================================
// 文件        : RdcTheme.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 控制端界面的设计令牌与通用控件样式, 深色为默认并可切浅色
// 架构定位    : 界面层样式常量, 不含业务逻辑
// 核心功能    : 从 res/values/colors.xml 与 dimens.xml 读取颜色/字号/间距/圆角/描边/控件高
// 职责边界    : 不读业务状态、不发命令、不持有界面状态; 代码内不写十六进制颜色
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35)
// 依赖关系    : RdcApp 启动时绑定资源; 被 MainActivity、SessionActivity、AccountActivity 等使用
// ==========================================================================================

package com.rdc.remote

import android.content.Context
import android.content.res.ColorStateList
import android.content.res.Resources
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.StateListDrawable
import android.view.Gravity
import android.view.View
import android.widget.TextView
import kotlin.math.roundToInt

/** 应用启动时绑定资源, 之后所有令牌都按当前主题从资源文件取值. */
internal object RdcColor {

    /** 当前是否深色; 由 Activity 在构建界面前设置. */
    @Volatile
    var dark: Boolean = true

    private var bound: Resources? = null

    fun bind(context: Context) {
        bound = context.applicationContext.resources
    }

    fun resources(): Resources = bound ?: error("RdcColor 未绑定资源, 请确认 RdcApp 已在清单注册")

    private fun pick(darkId: Int, lightId: Int): Int =
        resources().getColor(if (dark) darkId else lightId, null)

    val BG: Int get() = pick(R.color.rdc_bg_dark, R.color.rdc_bg_light)
    val PANEL: Int get() = pick(R.color.rdc_panel_dark, R.color.rdc_panel_light)
    val PANEL_2: Int get() = pick(R.color.rdc_panel2_dark, R.color.rdc_panel2_light)
    val PANEL_3: Int get() = pick(R.color.rdc_panel3_dark, R.color.rdc_panel3_light)
    val CARD: Int get() = pick(R.color.rdc_card_dark, R.color.rdc_card_light)
    val INPUT: Int get() = pick(R.color.rdc_input_dark, R.color.rdc_input_light)
    val HOVER: Int get() = pick(R.color.rdc_hover_dark, R.color.rdc_hover_light)
    val LINE: Int get() = pick(R.color.rdc_line_dark, R.color.rdc_line_light)
    val LINE_STRONG: Int get() = pick(R.color.rdc_line_strong_dark, R.color.rdc_line_strong_light)
    val TEXT: Int get() = pick(R.color.rdc_text_dark, R.color.rdc_text_light)
    val MUTED: Int get() = pick(R.color.rdc_muted_dark, R.color.rdc_muted_light)
    val FAINT: Int get() = pick(R.color.rdc_faint_dark, R.color.rdc_faint_light)
    val ACCENT: Int get() = pick(R.color.rdc_accent_dark, R.color.rdc_accent_light)
    val ACCENT_HOVER: Int get() = pick(R.color.rdc_accent_hover_dark, R.color.rdc_accent_hover_light)
    val ACCENT_STRONG: Int get() = pick(R.color.rdc_accent_strong_dark, R.color.rdc_accent_strong_light)
    val ACCENT_SOFT: Int get() = pick(R.color.rdc_accent_soft_dark, R.color.rdc_accent_soft_light)
    val OK: Int get() = pick(R.color.rdc_ok_dark, R.color.rdc_ok_light)
    val OK_SOFT: Int get() = pick(R.color.rdc_ok_soft_dark, R.color.rdc_ok_soft_light)
    val OK_BORDER: Int get() = pick(R.color.rdc_ok_border_dark, R.color.rdc_ok_border_light)
    val WARN: Int get() = pick(R.color.rdc_warn_dark, R.color.rdc_warn_light)
    val WARN_SOFT: Int get() = pick(R.color.rdc_warn_soft_dark, R.color.rdc_warn_soft_light)
    val WARN_BORDER: Int get() = pick(R.color.rdc_warn_border_dark, R.color.rdc_warn_border_light)
    val DANGER: Int get() = pick(R.color.rdc_danger_dark, R.color.rdc_danger_light)
    val DANGER_SOFT: Int get() = pick(R.color.rdc_danger_soft_dark, R.color.rdc_danger_soft_light)
    val DANGER_BORDER: Int get() = pick(R.color.rdc_danger_border_dark, R.color.rdc_danger_border_light)
    val BRAND_FROM: Int get() = pick(R.color.rdc_brand_from_dark, R.color.rdc_brand_from_light)
    val BRAND_TO: Int get() = pick(R.color.rdc_brand_to_dark, R.color.rdc_brand_to_light)
    val MEDIA_TEXT: Int get() = pick(R.color.rdc_media_text_dark, R.color.rdc_media_text_light)
    val MEDIA_TEXT_STRONG: Int get() = pick(R.color.rdc_media_text_strong_dark, R.color.rdc_media_text_strong_light)
    val MEDIA_FILL: Int get() = pick(R.color.rdc_media_fill_dark, R.color.rdc_media_fill_light)
    val MEDIA_FILL_STRONG: Int get() = pick(R.color.rdc_media_fill_strong_dark, R.color.rdc_media_fill_strong_light)
    val MEDIA_LINE: Int get() = pick(R.color.rdc_media_line_dark, R.color.rdc_media_line_light)
    val SURFACE: Int get() = pick(R.color.rdc_surface_dark, R.color.rdc_surface_light)
    val OVERLAY: Int get() = pick(R.color.rdc_overlay_dark, R.color.rdc_overlay_light)
    val PRESS: Int get() = pick(R.color.rdc_hover_dark, R.color.rdc_hover_light)
    val ACCENT_PRESS: Int get() = pick(R.color.rdc_accent_hover_dark, R.color.rdc_accent_hover_light)
}

/** 字号令牌, 单位 sp, 与 docs/design/ui-v1 的 typography.scale 同值. */
internal object RdcText {
    private fun sp(id: Int): Float {
        val resources = RdcColor.resources()
        return resources.getDimension(id) / resources.displayMetrics.scaledDensity
    }

    val MICRO: Float get() = sp(R.dimen.rdc_text_micro)               // 9sp
    val TINY: Float get() = sp(R.dimen.rdc_text_tiny)                 // 10sp
    val CAPTION: Float get() = sp(R.dimen.rdc_text_caption)           // 10.5sp
    val META: Float get() = sp(R.dimen.rdc_text_meta)                 // 11sp
    val BODY_SMALL: Float get() = sp(R.dimen.rdc_text_body_small)     // 11.5sp
    val BODY: Float get() = sp(R.dimen.rdc_text_body)                 // 12sp
    val BODY_STRONG: Float get() = sp(R.dimen.rdc_text_body_strong)   // 12.5sp
    val SUBHEAD: Float get() = sp(R.dimen.rdc_text_subhead)           // 13sp
    val HEADLINE: Float get() = sp(R.dimen.rdc_text_headline)         // 15sp
    val TITLE: Float get() = sp(R.dimen.rdc_text_title)               // 17sp
    val PAGE_TITLE: Float get() = sp(R.dimen.rdc_text_page_title)     // 19sp
    val HERO: Float get() = sp(R.dimen.rdc_text_hero)                 // 20sp
    val METRIC: Float get() = sp(R.dimen.rdc_text_metric)             // 22sp
    val CODE: Float get() = sp(R.dimen.rdc_text_code)                 // 28sp

    /** 把调用点请求的字号对齐到最近的令牌档位, 避免出现令牌外的字号. */
    fun tokenOf(sizeSp: Float): Float = when {
        sizeSp <= 9f -> MICRO
        sizeSp <= 10f -> TINY
        sizeSp <= 10.5f -> CAPTION
        sizeSp <= 11f -> META
        sizeSp <= 11.5f -> BODY_SMALL
        sizeSp <= 12f -> BODY
        sizeSp <= 12.5f -> BODY_STRONG
        sizeSp <= 13.5f -> SUBHEAD
        sizeSp <= 15.5f -> HEADLINE
        sizeSp <= 17.5f -> TITLE
        sizeSp <= 19.5f -> PAGE_TITLE
        sizeSp <= 21f -> HERO
        sizeSp <= 25f -> METRIC
        else -> CODE
    }
}

/**
 * 横向对齐基准: 与 ui-v2 原型连接页一致, 供各页沿用, 不在每段布局里各自设定.
 *
 * 基准来源 `docs/design/ui-v2/变更记录.md` r5:
 * 页面标题与说明左对齐; 卡片内状态块(门禁 / 空态 / 失败态)内容居中.
 */
internal object RdcAlign {

    /** 页面标题与说明: 左对齐. */
    const val PAGE_HEADER: Int = Gravity.START

    /** 卡片内状态块整体: 内容水平居中. */
    const val CARD_STATE: Int = Gravity.CENTER_HORIZONTAL

    /** 卡片内状态块的标题与说明文字: 居中. */
    const val CARD_STATE_TEXT: Int = Gravity.CENTER
}

/** 尺寸令牌, 全部以像素返回; 数值定义在 res/values/dimens.xml. */
internal object RdcMetrics {
    private fun px(id: Int): Int = RdcColor.resources().getDimensionPixelSize(id)

    val HAIRLINE: Int get() = px(R.dimen.rdc_border_hairline)
    val BUTTON: Int get() = px(R.dimen.rdc_control_button)
    val BUTTON_SMALL: Int get() = px(R.dimen.rdc_control_button_small)
    val ICON_BUTTON: Int get() = px(R.dimen.rdc_control_icon)
    val CHIP: Int get() = px(R.dimen.rdc_control_chip)
    val SEGMENT: Int get() = px(R.dimen.rdc_control_segment)
    val NAV_TAB_BAR: Int get() = px(R.dimen.rdc_nav_tab_bar)
    val PAGE_PADDING: Int get() = px(R.dimen.rdc_nav_page_padding)
    val ICON_INLINE: Int get() = px(R.dimen.rdc_icon_inline)
    val ICON_BODY: Int get() = px(R.dimen.rdc_icon_body)
    val ICON_NAV: Int get() = px(R.dimen.rdc_icon_nav)
    val ICON_TAB: Int get() = px(R.dimen.rdc_icon_tab)
}

/** dp 令牌解析: 间距梯级与固定尺寸都取自 dimens.xml. */
internal fun Context.dp(value: Int): Int {
    val id = spacingDimen(value)
    return if (id != 0) {
        resources.getDimensionPixelSize(id)
    } else {
        (value * resources.displayMetrics.density).toInt()
    }
}

private fun spacingDimen(value: Int): Int = when (value) {
    0 -> R.dimen.rdc_space_0
    1 -> R.dimen.rdc_size_1
    2 -> R.dimen.rdc_space_2
    3 -> R.dimen.rdc_space_3
    4 -> R.dimen.rdc_space_4
    5 -> R.dimen.rdc_space_5
    6 -> R.dimen.rdc_space_6
    7 -> R.dimen.rdc_space_7
    8 -> R.dimen.rdc_space_8
    9 -> R.dimen.rdc_space_9
    10 -> R.dimen.rdc_space_10
    11 -> R.dimen.rdc_space_11
    12 -> R.dimen.rdc_space_12
    13 -> R.dimen.rdc_space_13
    14 -> R.dimen.rdc_space_14
    16 -> R.dimen.rdc_space_16
    18 -> R.dimen.rdc_space_18
    20 -> R.dimen.rdc_space_20
    22 -> R.dimen.rdc_space_22
    24 -> R.dimen.rdc_space_24
    26 -> R.dimen.rdc_space_26
    28 -> R.dimen.rdc_size_28
    30 -> R.dimen.rdc_size_30
    32 -> R.dimen.rdc_size_32
    34 -> R.dimen.rdc_size_34
    36 -> R.dimen.rdc_size_36
    38 -> R.dimen.rdc_size_38
    39 -> R.dimen.rdc_size_39
    40 -> R.dimen.rdc_size_40
    42 -> R.dimen.rdc_size_42
    44 -> R.dimen.rdc_size_44
    45 -> R.dimen.rdc_size_45
    48 -> R.dimen.rdc_size_48
    56 -> R.dimen.rdc_size_56
    58 -> R.dimen.rdc_size_58
    60 -> R.dimen.rdc_size_60
    64 -> R.dimen.rdc_size_64
    72 -> R.dimen.rdc_size_72
    88 -> R.dimen.rdc_size_88
    96 -> R.dimen.rdc_size_96
    else -> 0
}

/** 圆角令牌解析: 旧调用点传入的档位统一折算到 radius 令牌. */
private fun Context.resolveRadiusDp(value: Int): Int {
    val id = when (value) {
        999 -> R.dimen.rdc_radius_pill
        20, 18 -> R.dimen.rdc_radius_hero
        16, 14 -> R.dimen.rdc_radius_panel
        13, 12 -> R.dimen.rdc_radius_card
        11, 10 -> R.dimen.rdc_radius_control
        9 -> R.dimen.rdc_radius_chip
        8, 7 -> R.dimen.rdc_radius_md
        6 -> R.dimen.rdc_radius_sm
        5 -> R.dimen.rdc_radius_xs
        else -> 0
    }
    return if (id == 0) value else (resources.getDimension(id) / resources.displayMetrics.density).roundToInt()
}

/** 控件高度令牌解析: 按钮/小按钮/图标按钮统一取 controlHeight 令牌. */
private fun controlHeightDp(value: Int): Int = when (value) {
    42, 40, 39 -> RdcMetrics.BUTTON
    30 -> RdcMetrics.BUTTON_SMALL
    28 -> RdcMetrics.ICON_BUTTON
    34 -> RdcColor.resources().getDimensionPixelSize(R.dimen.rdc_control_icon_large)
    else -> -1
}

/** 圆角纯色 drawable. */
internal fun Context.roundedDrawable(color: Int, radiusDp: Int, strokeColor: Int? = null): GradientDrawable =
    GradientDrawable().apply {
        shape = GradientDrawable.RECTANGLE
        setColor(color)
        cornerRadius = dp(resolveRadiusDp(radiusDp)).toFloat()
        if (strokeColor != null) {
            setStroke(dp(1), strokeColor)
        }
    }

/** 给视图设置圆角纯色背景, 可选描边. */
internal fun View.roundedBackground(color: Int, radiusDp: Int, strokeColor: Int? = null) {
    background = context.roundedDrawable(color, radiusDp, strokeColor)
}

/** 给视图设置按下态背景: 普通态 normal, 按下态 pressed. */
internal fun View.pressableBackground(
    normal: Int,
    pressed: Int,
    radiusDp: Int,
    strokeColor: Int? = null,
) {
    background = StateListDrawable().apply {
        addState(intArrayOf(android.R.attr.state_pressed), context.roundedDrawable(pressed, radiusDp, strokeColor))
        addState(intArrayOf(), context.roundedDrawable(normal, radiusDp, strokeColor))
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
    textSize = RdcText.tokenOf(sizeSp)
    setTextColor(color)
    this.gravity = gravity
    // 长文案按字符自动换行, 不省略、不截断
    maxLines = Int.MAX_VALUE
    ellipsize = null
    breakStrategy = android.graphics.text.LineBreaker.BREAK_STRATEGY_SIMPLE
    hyphenationFrequency = android.text.Layout.HYPHENATION_FREQUENCY_NONE
    includeFontPadding = false
    setLineSpacing(0f, 1.18f)
}

/** 可点击的胶囊/主按钮, 以 TextView 承载, 自带按下态. */
internal fun Context.actionView(
    text: String,
    sizeSp: Float,
    textColor: Int,
    background: Int,
    radiusDp: Int,
    strokeColor: Int? = null,
    heightDp: Int = 40,
    pressedBackground: Int = RdcColor.PRESS,
): TextView = TextView(this).apply {
    this.text = text
    textSize = RdcText.tokenOf(sizeSp)
    setTextColor(textColor)
    gravity = Gravity.CENTER
    isClickable = true
    isFocusable = true
    // 按钮文案过长时换行, 不溢出到按钮外
    maxLines = 2
    ellipsize = null
    breakStrategy = android.graphics.text.LineBreaker.BREAK_STRATEGY_SIMPLE
    hyphenationFrequency = android.text.Layout.HYPHENATION_FREQUENCY_NONE
    includeFontPadding = false
    pressableBackground(background, pressedBackground, radiusDp, strokeColor)
    val height = controlHeightDp(heightDp).takeIf { it > 0 } ?: dp(heightDp)
    layoutParams = android.view.ViewGroup.LayoutParams(
        android.view.ViewGroup.LayoutParams.MATCH_PARENT,
        height,
    )
}

/** 文本按下态颜色: 普通 accent, 按下 accentStrong. */
internal fun View.pressableText(view: TextView, normal: Int, pressed: Int) {
    view.setTextColor(
        ColorStateList(
            arrayOf(
                intArrayOf(android.R.attr.state_pressed),
                intArrayOf(),
            ),
            intArrayOf(pressed, normal),
        ),
    )
}

/** 画质档位的中文显示名; 命令层值保持原样, 只做界面映射. */
internal fun qualityLabel(value: String): String = when (value) {
    "auto" -> "自动"
    "4K60" -> "超清 60 帧"
    "1440p60" -> "高清 60 帧"
    "1080p60" -> "全高清 60 帧"
    "1080p30" -> "全高清 30 帧"
    else -> "自动"
}

/** 通路的首个字段只取中文显示名, 不把点对点/中继的英文标识露给用户. */
internal fun transportLabel(value: String?): String = when (value?.lowercase()) {
    "p2p", "direct", "lan" -> "点对点直连"
    "relay" -> "中继转发"
    else -> if (value.isNullOrBlank()) "—" else "未知通路"
}
