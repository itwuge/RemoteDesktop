// ==========================================================================================
// 文件        : AeadChannelTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : AEAD 通道的 64 位滑动窗口、重放拒绝与前缀绑定单测
// 架构定位    : 协议层单测; 使用固定密钥, 不建网络连接
// 核心功能    : 往返解密; 重复计数器拒绝; 落后窗口断链; 发送方前缀变化拒绝
// 职责边界    : 只测 AeadChannel, 不验证服务端行为
// 运行环境    : JDK 17 + BouncyCastle 1.78.1, JUnit 4
// 依赖关系    : RdcCrypto.AeadChannel
// ==========================================================================================

package com.rdc.remote

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Test

class AeadChannelTest {

    private val keyA = ByteArray(32) { (it + 1).toByte() }
    private val keyB = ByteArray(32) { (it + 101).toByte() }

    @Test
    fun `encrypt decrypt round trips in order`() {
        val sender = AeadChannel(keyA, keyB)
        val receiver = AeadChannel(keyB, keyA)

        repeat(5) { index ->
            val plaintext = "帧-$index".toByteArray()
            assertArrayEquals(plaintext, receiver.decrypt(sender.encrypt(plaintext)))
        }
    }

    @Test
    fun `replayed ciphertext is rejected`() {
        val sender = AeadChannel(keyA, keyB)
        val receiver = AeadChannel(keyB, keyA)
        val ciphertext = sender.encrypt("once".toByteArray())

        receiver.decrypt(ciphertext)
        assertFailure("重放") { receiver.decrypt(ciphertext) }
    }

    @Test
    fun `counter older than 64 frame window breaks the link`() {
        val sender = AeadChannel(keyA, keyB)
        val receiver = AeadChannel(keyB, keyA)
        val frames = ArrayList<ByteArray>()
        repeat(70) { frames.add(sender.encrypt("f$it".toByteArray())) }

        // 先收第 70 帧, 最高计数器前移; 再收第 1 帧, 落后 69 帧超过 64 位窗口
        assertEquals("f69", String(receiver.decrypt(frames[69])))
        assertFailure("窗口") { receiver.decrypt(frames[0]) }
    }

    @Test
    fun `out of order frames inside window are accepted`() {
        val sender = AeadChannel(keyA, keyB)
        val receiver = AeadChannel(keyB, keyA)
        val frames = ArrayList<ByteArray>()
        repeat(10) { frames.add(sender.encrypt("f$it".toByteArray())) }

        assertEquals("f9", String(receiver.decrypt(frames[9])))
        assertEquals("f7", String(receiver.decrypt(frames[7])))
        assertEquals("f8", String(receiver.decrypt(frames[8])))
        assertFailure("重放") { receiver.decrypt(frames[8]) }
    }

    @Test
    fun `sender prefix change is rejected`() {
        val senderOne = AeadChannel(keyA, keyB)
        val senderTwo = AeadChannel(keyA, keyB)
        val receiver = AeadChannel(keyB, keyA)
        receiver.decrypt(senderOne.encrypt("one".toByteArray()))

        assertFailure("前缀") { receiver.decrypt(senderTwo.encrypt("two".toByteArray())) }
    }

    private fun assertFailure(keyword: String, block: () -> Unit) {
        try {
            block()
            fail("应当拒绝: $keyword")
        } catch (error: IllegalArgumentException) {
            val message = error.message.orEmpty()
            if (!message.contains(keyword)) {
                throw AssertionError("拒绝原因不含「$keyword」: $message", error)
            }
        }
    }
}
