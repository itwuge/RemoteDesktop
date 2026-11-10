// ==========================================================================================
// 文件        : ViewportMath.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 远端画面缩放/平移计算: 适应比例、缩放上下限、锚点缩放、边界夹取与触摸反算
// 架构定位    : 界面层纯函数, 不依赖 View / Android 状态, 可被单元测试直接覆盖
// 核心功能    : 适应屏幕、4 倍上限、远端小于屏幕时按原始像素、双指锚点缩放、平移与坐标换算
// 职责边界    : 不发送输入、不改视图; 只产出视口状态、倍率与矩阵数值
// ==========================================================================================

package com.rdc.remote

/**
 * 画面视口.
 *
 * - [scale] 为绝对显示比例(远端像素 -> 视图像素). 0 表示未设置, 按默认比例处理;
 * - [panX]/[panY] 为画面左上角相对视口左上角的偏移, 仅在画面大于视口时生效;
 * - 画面小于视口的方向始终居中, 不会出现拖出黑边或拉伸.
 */
internal data class Viewport(
    val viewWidth: Float = 0f,
    val viewHeight: Float = 0f,
    val sourceWidth: Float = 1f,
    val sourceHeight: Float = 1f,
    val scale: Float = 0f,
    val panX: Float = 0f,
    val panY: Float = 0f,
)

internal object ViewportMath {

    private const val EPSILON = 0.0001f
    private const val MAX_SCALE = 4f

    /** 适应屏幕比例 = min(可视宽 ÷ 远端宽, 可视高 ÷ 远端高). */
    fun fitScale(viewWidth: Float, viewHeight: Float, sourceWidth: Float, sourceHeight: Float): Float {
        if (viewWidth <= 0f || viewHeight <= 0f || sourceWidth <= 0f || sourceHeight <= 0f) {
            return 1f
        }
        return minOf(viewWidth / sourceWidth, viewHeight / sourceHeight)
    }

    fun fitScale(state: Viewport): Float =
        fitScale(state.viewWidth, state.viewHeight, state.sourceWidth, state.sourceHeight)

    /**
     * 缩放下限 = 适应屏幕比例; 远端小于屏幕时适应比例大于 1, 压为 1(原始像素不放大).
     * 随视口与远端尺寸实时重算, 不写死.
     */
    fun minScale(state: Viewport): Float = minOf(fitScale(state), 1f)

    /** 缩放上限 = 4.0 倍原始像素. */
    fun maxScale(state: Viewport): Float = maxOf(MAX_SCALE, minScale(state))

    /** 默认倍率 = 适应屏幕(整个远端桌面完整可见, 远端小于屏幕时不放大). */
    fun defaultScale(state: Viewport): Float = minScale(state)

    private fun resolveScale(state: Viewport): Float {
        if (state.viewWidth <= 0f || state.viewHeight <= 0f) {
            return 1f
        }
        val scale = if (state.scale <= 0f) defaultScale(state) else state.scale
        return scale.coerceIn(minScale(state), maxScale(state))
    }

    /** 把比例与平移夹到合法范围: 内容边缘不允许离开视口. */
    fun clamp(state: Viewport): Viewport {
        if (state.viewWidth <= 0f || state.viewHeight <= 0f) {
            return state
        }
        if (state.scale <= 0f) {
            // 未设置比例: 默认适应屏幕并居中
            return centered(state.copy(scale = defaultScale(state)))
        }
        val scale = resolveScale(state)
        val width = state.sourceWidth * scale
        val height = state.sourceHeight * scale
        val panX = if (width > state.viewWidth) {
            state.panX.coerceIn(state.viewWidth - width, 0f)
        } else {
            0f
        }
        val panY = if (height > state.viewHeight) {
            state.panY.coerceIn(state.viewHeight - height, 0f)
        } else {
            0f
        }
        return state.copy(scale = scale, panX = panX, panY = panY)
    }

    fun contentWidth(state: Viewport): Float = state.sourceWidth * resolveScale(state)

    fun contentHeight(state: Viewport): Float = state.sourceHeight * resolveScale(state)

    /** 内容左上角: 超出视口时由平移量决定, 否则居中. */
    fun contentLeft(state: Viewport): Float {
        val width = contentWidth(state)
        if (width <= state.viewWidth) {
            return (state.viewWidth - width) / 2f
        }
        return state.panX.coerceIn(state.viewWidth - width, 0f)
    }

    fun contentTop(state: Viewport): Float {
        val height = contentHeight(state)
        if (height <= state.viewHeight) {
            return (state.viewHeight - height) / 2f
        }
        return state.panY.coerceIn(state.viewHeight - height, 0f)
    }

    /** 居中: 超出屏幕的方向取中间区域, 小于屏幕的方向保持居中留背景. */
    fun centered(state: Viewport): Viewport {
        val scale = resolveScale(state)
        val width = state.sourceWidth * scale
        val height = state.sourceHeight * scale
        val panX = if (width > state.viewWidth) (state.viewWidth - width) / 2f else 0f
        val panY = if (height > state.viewHeight) (state.viewHeight - height) / 2f else 0f
        return clamp(state.copy(scale = scale, panX = panX, panY = panY))
    }

    /** 双击/「还原画面」: 回到默认档(适应屏幕)并居中. */
    fun reset(state: Viewport): Viewport = centered(state.copy(scale = minScale(state)))

    /** 「适应屏幕」: 缩到能看见整个远端桌面为止(不小于适应比例). */
    fun fitToScreen(state: Viewport): Viewport = centered(state.copy(scale = minScale(state)))

    /** 「回到左上角」: 左上角与视口左上角对齐; 未超出屏幕时退化居中. */
    fun topLeft(state: Viewport): Viewport = clamp(state.copy(panX = 0f, panY = 0f))

    /** 画面是否已超出屏幕: 「回到左上角」只在此时可用. */
    fun canPanToTopLeft(state: Viewport): Boolean =
        contentWidth(state) > state.viewWidth + EPSILON ||
            contentHeight(state) > state.viewHeight + EPSILON

    /** 双指拖动: 平移视口并夹在画面范围内, 不允许露黑边. */
    fun panBy(state: Viewport, deltaX: Float, deltaY: Float): Viewport {
        val clamped = clamp(state)
        return clamp(
            clamped.copy(
                panX = clamped.panX + deltaX,
                panY = clamped.panY + deltaY,
            ),
        )
    }

    /**
     * 以 [focusX]/[focusY] 为锚点缩放到 [targetScale].
     *
     * 先反算锚点处的归一化内容坐标, 再反推新的平移量, 保证双指捏合时锚点不漂移.
     */
    fun zoomAt(state: Viewport, targetScale: Float, focusX: Float, focusY: Float): Viewport {
        val clamped = clamp(state)
        val widthBefore = contentWidth(clamped)
        val heightBefore = contentHeight(clamped)
        if (widthBefore <= EPSILON || heightBefore <= EPSILON) {
            return clamped
        }
        val leftBefore = contentLeft(clamped)
        val topBefore = contentTop(clamped)
        val u = (focusX - leftBefore) / widthBefore
        val v = (focusY - topBefore) / heightBefore
        val scale = targetScale.coerceIn(minScale(clamped), maxScale(clamped))
        val widthAfter = clamped.sourceWidth * scale
        val heightAfter = clamped.sourceHeight * scale
        val leftAfter = focusX - u * widthAfter
        val topAfter = focusY - v * heightAfter
        return clamp(
            clamped.copy(
                scale = scale,
                panX = leftAfter,
                panY = topAfter,
            ),
        )
    }

    /** 当前倍率(1.0 = 原始像素), 用于浮条提示. */
    fun multiplier(state: Viewport): Float = resolveScale(state)

    /**
     * 触摸反算: 返回远端归一化坐标.
     *
     * 落在画面外(居中留边)时钳制到最近的内容边界; 视口尺寸无效时返回 null.
     */
    fun mapToNormalized(state: Viewport, touchX: Float, touchY: Float): Pair<Float, Float>? {
        if (state.viewWidth <= 0f || state.viewHeight <= 0f) {
            return null
        }
        val width = contentWidth(state)
        val height = contentHeight(state)
        if (width <= EPSILON || height <= EPSILON) {
            return null
        }
        val u = ((touchX - contentLeft(state)) / width).coerceIn(0f, 1f)
        val v = ((touchY - contentTop(state)) / height).coerceIn(0f, 1f)
        return u to v
    }

    /**
     * TextureView 变换矩阵 [scaleX, skewX, translateX, skewY, scaleY, translateY, 0, 0, 1].
     *
     * TextureView 默认把纹理拉伸到视口, 这里按内容区缩放并平移.
     */
    fun matrixValues(state: Viewport): FloatArray {
        if (state.viewWidth <= 0f || state.viewHeight <= 0f) {
            return floatArrayOf(1f, 0f, 0f, 0f, 1f, 0f, 0f, 0f, 1f)
        }
        return floatArrayOf(
            contentWidth(state) / state.viewWidth,
            0f,
            contentLeft(state),
            0f,
            contentHeight(state) / state.viewHeight,
            contentTop(state),
            0f,
            0f,
            1f,
        )
    }
}
