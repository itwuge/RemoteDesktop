// ==========================================================================================
// 文件        : H264AnnexBTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : 媒体解码路径的编码级检查: Annex B 参数集提取
// 架构定位    : 媒体适配层纯函数单测; 不拉起 MediaCodec, 不依赖设备
// 核心功能    : 3/4 字节起始码; SPS/PPS 提取; 非参数集 NAL 忽略; 截断输入不崩溃
// 职责边界    : 只测码流切分与 NAL 类型识别, 不验证真实硬解出图
// 运行环境    : JDK 17, JUnit 4
// 依赖关系    : H264AnnexB
// ==========================================================================================

package com.rdc.remote

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class H264AnnexBTest {

    private val sps = byteArrayOf(0x67, 0x42, 0x00, 0x1e, 0x95.toByte(), 0xa8.toByte())
    private val pps = byteArrayOf(0x68, 0xce.toByte(), 0x3c.toByte(), 0x80.toByte())
    private val idr = byteArrayOf(0x65, 0x88.toByte(), 0x84.toByte(), 0x00, 0x33)

    @Test
    fun `extracts sps and pps with four byte start codes`() {
        val data = start4() + sps + start4() + pps + start4() + idr

        val params = H264AnnexB.extractParamSets(data)

        assertArrayEquals(start4() + sps, params.sps)
        assertArrayEquals(start4() + pps, params.pps)
    }

    @Test
    fun `extracts sps and pps with three byte start codes`() {
        val data = start3() + sps + start3() + pps

        val params = H264AnnexB.extractParamSets(data)

        assertArrayEquals(start3() + sps, params.sps)
        assertArrayEquals(start3() + pps, params.pps)
    }

    @Test
    fun `mixed start code lengths keep the last nal intact`() {
        val data = start4() + sps + start3() + pps + start3() + idr

        val params = H264AnnexB.extractParamSets(data)

        assertArrayEquals(start4() + sps, params.sps)
        assertArrayEquals(start3() + pps, params.pps)
    }

    @Test
    fun `sps only frame has null pps`() {
        val params = H264AnnexB.extractParamSets(start3() + sps)

        assertNotNull(params.sps)
        assertNull(params.pps)
    }

    @Test
    fun `frame without start code yields empty result`() {
        val params = H264AnnexB.extractParamSets(byteArrayOf(0x01, 0x02, 0x03, 0x04))

        assertNull(params.sps)
        assertNull(params.pps)
    }

    @Test
    fun `truncated start code does not crash`() {
        val params = H264AnnexB.extractParamSets(byteArrayOf(0x00, 0x00, 0x00))

        assertNull(params.sps)
        assertNull(params.pps)
    }

    @Test
    fun `empty payload is safe`() {
        val params = H264AnnexB.extractParamSets(ByteArray(0))

        assertNull(params.sps)
        assertNull(params.pps)
    }

    @Test
    fun `nal with only start code is ignored`() {
        val data = start4() + start4() + sps

        val params = H264AnnexB.extractParamSets(data)

        assertArrayEquals(start4() + sps, params.sps)
        assertNull(params.pps)
    }

    private fun start3(): ByteArray = byteArrayOf(0x00, 0x00, 0x01)

    private fun start4(): ByteArray = byteArrayOf(0x00, 0x00, 0x00, 0x01)
}
