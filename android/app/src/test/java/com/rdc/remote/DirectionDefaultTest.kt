package com.rdc.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

/**
 * 固定竖屏下的默认倍率: 默认档 = 适应屏幕(整个远端桌面完整可见, 远端小于屏幕时不放大).
 * 与 rdc 安卓端的 SessionActivity.currentViewport 取值规则一一对应.
 */
class DirectionDefaultTest {

    private fun viewport(w: Float, h: Float, sw: Float, sh: Float, scale: Float = 0f, panX: Float = 0f, panY: Float = 0f) =
        Viewport(viewWidth = w, viewHeight = h, sourceWidth = sw, sourceHeight = sh, scale = scale, panX = panX, panY = panY)

    private fun defaultOf(state: Viewport) = ViewportMath.defaultScale(state)

    @Test
    fun portraitDefaultIsFitFor1080pDesktop() {
        val state = viewport(1080f, 2400f, 1920f, 1080f)
        assertEquals(0.5625f, defaultOf(state), 1e-4f)
        assertEquals(defaultOf(state), ViewportMath.fitScale(state), 1e-4f)
    }

    @Test
    fun portraitDefaultIsFitFor4kDesktop() {
        val state = viewport(1080f, 2400f, 3840f, 2160f)
        assertEquals(0.28125f, defaultOf(state), 1e-4f)
    }

    @Test
    fun desktopFittingNativelyDefaultsToOne() {
        // 宽视口下 1080p 远端刚好放得下, 适应屏幕 = 1.0 倍
        val state = viewport(2400f, 1080f, 1920f, 1080f)
        assertEquals(1f, defaultOf(state), 1e-4f)
    }

    @Test
    fun wideDefaultIsStillFitNotNativePixel() {
        // 4K 远端在宽视口下也要缩小到适应, 不再按 1.0 倍原始像素显示
        val state = viewport(2400f, 1080f, 3840f, 2160f)
        assertEquals(0.5f, defaultOf(state), 1e-4f)
        assertEquals(ViewportMath.fitScale(state), defaultOf(state), 1e-4f)
    }

    @Test
    fun portraitDefaultForSmallRemoteIsOneNotEnlarged() {
        val state = viewport(1080f, 2400f, 640f, 480f)
        assertEquals(1f, defaultOf(state), 1e-4f)
        assertEquals(1080f / 640f, ViewportMath.fitScale(state), 1e-4f)
    }

    @Test
    fun portraitRemoteInPortraitViewFitsNatively() {
        // 远端为竖屏 1080×1920: 竖屏视口 1080×2289 下按 1.0 倍完整显示, 不放大、不裁切
        val state = viewport(1080f, 2289f, 1080f, 1920f)
        assertEquals(1f, defaultOf(state), 1e-4f)
        val fitted = ViewportMath.clamp(state)
        assertEquals(1080f, ViewportMath.contentWidth(fitted), 0.5f)
        assertEquals(1920f, ViewportMath.contentHeight(fitted), 0.5f)
        assertEquals(0f, ViewportMath.contentLeft(fitted), 0.5f)
        assertFalse(ViewportMath.canPanToTopLeft(fitted))
    }

    @Test
    fun rotationKeepsScaleAndPan() {
        // 界面固定竖屏: 旋转手机不改变视口尺寸, 已放大的倍率与偏移必须原样保留
        val zoomed = ViewportMath.zoomAt(viewport(1080f, 2400f, 1920f, 1080f), 2f, 540f, 1200f)
        val panned = ViewportMath.panBy(zoomed, -120f, -80f)
        val afterRotation = ViewportMath.clamp(
            viewport(
                1080f,
                2400f,
                1920f,
                1080f,
                scale = panned.scale,
                panX = panned.panX,
                panY = panned.panY,
            ),
        )
        assertEquals(panned.scale, afterRotation.scale, 1e-4f)
        assertEquals(panned.panX, afterRotation.panX, 1e-4f)
        assertEquals(panned.panY, afterRotation.panY, 1e-4f)
    }

    @Test
    fun rotationKeepsFourTimesUpperBound() {
        val maxed = ViewportMath.zoomAt(viewport(1080f, 2400f, 1920f, 1080f), 99f, 540f, 1200f)
        assertEquals(4f, maxed.scale, 1e-4f)
        val afterRotation = ViewportMath.clamp(maxed.copy())
        assertEquals(4f, afterRotation.scale, 1e-4f)
    }

    @Test
    fun resetGoesToDefaultFitFor4k() {
        val zoomed = viewport(1080f, 2400f, 3840f, 2160f, scale = 3f, panX = -500f, panY = -200f)
        val reset = ViewportMath.reset(zoomed)
        assertEquals(0.28125f, reset.scale, 1e-4f)
        assertEquals(0f, reset.panX, 1e-4f)
    }

    @Test
    fun resetGoesToPortraitFit() {
        val zoomed = viewport(1080f, 2400f, 1920f, 1080f, scale = 2.5f)
        val reset = ViewportMath.reset(zoomed)
        assertEquals(0.5625f, reset.scale, 1e-4f)
        assertEquals(0f, reset.panX, 1e-4f)
    }
}
