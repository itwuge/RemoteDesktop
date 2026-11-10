package com.rdc.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 全屏倍率规则测试.
 *
 * 倍率 1.0 = 原始像素(1 远端像素 = 1 屏幕像素); 上下限随视口与远端尺寸实时重算.
 * 界面固定竖屏, 默认档 = 适应屏幕; 手机竖屏视口按 1080×2400 计.
 */
class ViewportMathTest {

    private val portraitView = 1080f to 2400f
    private val landscapeView = 2400f to 1080f

    private fun viewport(
        remoteWidth: Float,
        remoteHeight: Float,
        scale: Float = 0f,
        panX: Float = 0f,
        panY: Float = 0f,
        landscape: Boolean = true,
    ): Viewport {
        val view = if (landscape) landscapeView else portraitView
        return Viewport(
            viewWidth = view.first,
            viewHeight = view.second,
            sourceWidth = remoteWidth,
            sourceHeight = remoteHeight,
            scale = scale,
            panX = panX,
            panY = panY,
        )
    }

    // ------------------------------------------------------------ 默认 1.0 倍(原始像素)

    @Test
    fun defaultIsNativePixelFor720p() {
        val state = ViewportMath.clamp(viewport(1280f, 720f))
        assertEquals(1f, state.scale, 0.0001f)
        assertEquals(1280f, ViewportMath.contentWidth(state), 0.0001f)
        assertEquals(720f, ViewportMath.contentHeight(state), 0.0001f)
    }

    @Test
    fun defaultIsNativePixelFor1080p() {
        val state = ViewportMath.clamp(viewport(1920f, 1080f))
        assertEquals(1f, state.scale, 0.0001f)
        assertEquals("1.0 倍", String.format(java.util.Locale.US, "%.1f 倍", ViewportMath.multiplier(state)))
    }

    @Test
    fun defaultShrinks4kToFit() {
        val state = ViewportMath.clamp(viewport(3840f, 2160f))
        assertEquals(0.5f, state.scale, 0.0001f)
        // 适应屏幕下 4K 整个桌面完整可见(按高度贴合), 无需平移
        assertEquals(state.viewHeight, ViewportMath.contentHeight(state), 0.01f)
        assertEquals(1920f, ViewportMath.contentWidth(state), 0.01f)
        assertFalse(ViewportMath.canPanToTopLeft(state))
    }

    @Test
    fun portraitPhoneUsesFitDefault() {
        // 固定竖屏: 默认即适应屏幕, 1080p 桌面完整可见
        val state = ViewportMath.clamp(viewport(1920f, 1080f, landscape = false))
        assertEquals(0.5625f, state.scale, 0.0001f)
        assertEquals(0f, ViewportMath.contentLeft(state), 0.0001f)
        assertEquals(state.viewWidth, ViewportMath.contentWidth(state), 0.01f)
    }

    // ------------------------------------------------------------ 上下限

    @Test
    fun lowerBoundEqualsFitScale() {
        val state = viewport(1920f, 1080f, landscape = false)
        val fit = ViewportMath.fitScale(state)
        val shrunk = ViewportMath.zoomAt(state, 0.01f, 540f, 1200f)
        assertEquals(fit, shrunk.scale, 0.0001f)
        assertEquals(0.5625f, fit, 0.0001f)
    }

    @Test
    fun upperBoundIsFourTimesNative() {
        val state = viewport(1920f, 1080f)
        val enlarged = ViewportMath.zoomAt(state, 99f, 1200f, 540f)
        assertEquals(4f, enlarged.scale, 0.0001f)
    }

    @Test
    fun smallRemoteStaysNativeAndCentered() {
        val state = ViewportMath.clamp(viewport(640f, 480f))
        // 适应比例 > 1, 但不自动放大: 原始像素居中
        assertEquals(1f, state.scale, 0.0001f)
        assertEquals(640f, ViewportMath.contentWidth(state), 0.0001f)
        assertEquals(480f, ViewportMath.contentHeight(state), 0.0001f)
        assertEquals(2.25f, ViewportMath.fitScale(state), 0.0001f)
        assertEquals((2400f - 640f) / 2f, ViewportMath.contentLeft(state), 0.0001f)
        assertEquals((1080f - 480f) / 2f, ViewportMath.contentTop(state), 0.0001f)
        assertFalse(ViewportMath.canPanToTopLeft(state))
    }

    @Test
    fun smallRemoteCanStillZoomToFour() {
        val state = ViewportMath.zoomAt(viewport(640f, 480f), 9f, 1200f, 540f)
        assertEquals(4f, state.scale, 0.0001f)
        assertEquals(2560f, ViewportMath.contentWidth(state), 0.0001f)
    }

    // ------------------------------------------------------------ 还原与适应屏幕

    @Test
    fun resetReturnsToNativeAndCentered() {
        val zoomed = ViewportMath.zoomAt(viewport(1920f, 1080f), 2.5f, 600f, 300f)
        val panned = ViewportMath.panBy(zoomed, -800f, -300f)
        val reset = ViewportMath.reset(panned)
        assertEquals(1f, reset.scale, 0.0001f)
        assertEquals((2400f - 1920f) / 2f, ViewportMath.contentLeft(reset), 0.0001f)
        assertEquals((1080f - 1080f) / 2f, ViewportMath.contentTop(reset), 0.0001f)
    }

    @Test
    fun fitToScreenShowsWholeDesktop() {
        val fitted = ViewportMath.fitToScreen(viewport(1920f, 1080f, landscape = false))
        assertEquals(0.5625f, fitted.scale, 0.0001f)
        assertFalse(ViewportMath.canPanToTopLeft(fitted))
        assertEquals(1080f, ViewportMath.contentWidth(fitted), 0.0001f)
        assertEquals(607.5f, ViewportMath.contentHeight(fitted), 0.0001f)
        assertEquals(0f, ViewportMath.contentLeft(fitted), 0.0001f)
        assertEquals((2400f - 607.5f) / 2f, ViewportMath.contentTop(fitted), 0.0001f)
    }

    // ------------------------------------------------------------ 平移

    @Test
    fun panClampsWithoutExposingBackground() {
        val base = ViewportMath.clamp(viewport(1920f, 1080f, scale = 2f))
        val farLeft = ViewportMath.panBy(base, -99999f, 0f)
        assertEquals(2400f - 3840f, ViewportMath.contentLeft(farLeft), 0.0001f)
        val farRight = ViewportMath.panBy(base, 99999f, 0f)
        assertEquals(0f, ViewportMath.contentLeft(farRight), 0.0001f)
        assertTrue(ViewportMath.contentLeft(farLeft) + ViewportMath.contentWidth(farLeft) >= 2400f - 0.01f)
    }

    @Test
    fun panIgnoredWhenPictureSmallerThanViewport() {
        val small = ViewportMath.clamp(viewport(640f, 480f))
        val panned = ViewportMath.panBy(small, 300f, 300f)
        assertEquals((2400f - 640f) / 2f, ViewportMath.contentLeft(panned), 0.0001f)
        assertEquals((1080f - 480f) / 2f, ViewportMath.contentTop(panned), 0.0001f)
    }

    // ------------------------------------------------------------ 坐标换算

    @Test
    fun touchMappingUsesScaleAndOffset() {
        val zoomed = ViewportMath.clamp(viewport(3840f, 2160f, scale = 2f, panX = -1000f, panY = -500f))
        val point = ViewportMath.mapToNormalized(zoomed, 0f, 0f)!!
        // 被控端坐标 = (触摸点 − 画面原点) ÷ 倍率
        assertEquals(500f / 3840f, point.first, 0.0001f)
        assertEquals(250f / 2160f, point.second, 0.0001f)
    }

    @Test
    fun touchAtCenterAfterPanHitsCenterOfDesktop() {
        val state = ViewportMath.centered(viewport(1920f, 1080f, scale = 2f))
        val point = ViewportMath.mapToNormalized(state, 1200f, 540f)!!
        assertEquals(0.5f, point.first, 0.0001f)
        assertEquals(0.5f, point.second, 0.0001f)
    }

    @Test
    fun pinchAnchorDoesNotDrift() {
        val before = ViewportMath.clamp(viewport(1920f, 1080f))
        val focusX = 1200f
        val focusY = 540f
        val anchorBefore = ViewportMath.mapToNormalized(before, focusX, focusY)!!
        val after = ViewportMath.zoomAt(before, 2f, focusX, focusY)
        val anchorAfter = ViewportMath.mapToNormalized(after, focusX, focusY)!!
        assertEquals(anchorBefore.first, anchorAfter.first, 0.0001f)
        assertEquals(anchorBefore.second, anchorAfter.second, 0.0001f)
    }

    @Test
    fun touchOutsidePictureIsClampedToEdge() {
        val small = ViewportMath.clamp(viewport(640f, 480f))
        val corner = ViewportMath.mapToNormalized(small, 0f, 0f)!!
        assertEquals(0f, corner.first, 0.0001f)
        assertEquals(0f, corner.second, 0.0001f)
        val beyond = ViewportMath.mapToNormalized(small, 2399f, 1079f)!!
        assertEquals(1f, beyond.first, 0.0001f)
        assertEquals(1f, beyond.second, 0.0001f)
    }

    // ------------------------------------------------------------ 旋转不重算(固定竖屏)

    @Test
    fun rotationKeepsScaleAndOffset() {
        // 界面固定竖屏: 旋转手机不改变视口, 已缩放/平移的倍率与偏移原样保留
        val zoomed = ViewportMath.zoomAt(viewport(1920f, 1080f, landscape = false), 2f, 540f, 1200f)
        val panned = ViewportMath.panBy(zoomed, -150f, -90f)
        val after = ViewportMath.clamp(
            viewport(
                1920f,
                1080f,
                scale = panned.scale,
                panX = panned.panX,
                panY = panned.panY,
                landscape = false,
            ),
        )
        assertEquals(panned.scale, after.scale, 0.0001f)
        assertEquals(panned.panX, after.panX, 0.0001f)
        assertEquals(panned.panY, after.panY, 0.0001f)

        // 4.0 倍上限在旋转后仍保持
        val maxed = ViewportMath.zoomAt(viewport(1920f, 1080f, landscape = false), 99f, 540f, 1200f)
        assertEquals(4f, maxed.scale, 0.0001f)
    }

    // ------------------------------------------------------------ 矩阵

    @Test
    fun matrixMapsToContentRectWithoutDistortion() {
        val state = ViewportMath.clamp(viewport(3840f, 2160f, scale = 2f, panX = -1000f, panY = -500f))
        val values = ViewportMath.matrixValues(state)
        assertEquals(ViewportMath.contentLeft(state), values[2], 0.0001f)
        assertEquals(ViewportMath.contentTop(state), values[5], 0.0001f)
        val drawnWidth = values[0] * state.viewWidth
        val drawnHeight = values[4] * state.viewHeight
        assertEquals(ViewportMath.contentWidth(state), drawnWidth, 0.01f)
        assertEquals(ViewportMath.contentHeight(state), drawnHeight, 0.01f)
        assertEquals(3840f / 2160f, drawnWidth / drawnHeight, 0.0001f)
    }

    @Test
    fun invalidViewportProducesNoTouchPoint() {
        assertNull(ViewportMath.mapToNormalized(viewport(1920f, 1080f).copy(viewWidth = 0f), 100f, 100f))
        assertNull(ViewportMath.mapToNormalized(viewport(1920f, 1080f).copy(viewHeight = 0f), 100f, 100f))
        assertNotNull(ViewportMath.mapToNormalized(viewport(1920f, 1080f), 500f, 500f))
    }
}
