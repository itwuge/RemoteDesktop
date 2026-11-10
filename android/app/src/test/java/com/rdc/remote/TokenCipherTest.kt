// ==========================================================================================
// 文件        : TokenCipherTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : 令牌本地加密信封的纯 JVM 单测
// 架构定位    : 存储层单测; 用普通 AES 密钥替代 Android Keystore
// 核心功能    : 加解密往返; 每次 IV 随机; 密文不含明文; 篡改被 GCM 拒绝; 摘要稳定
// 职责边界    : 只测 TokenCipher, 不测 Keystore 生命周期
// 运行环境    : JDK 17, JUnit 4
// 依赖关系    : TokenCipher
// ==========================================================================================

package com.rdc.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.util.Base64
import javax.crypto.AEADBadTagException

class TokenCipherTest {

    private val key = TokenCipher.newKey()

    @Test
    fun `seal and open round trip`() {
        val token = "device-token-0123456789abcdef"

        val sealed = TokenCipher.seal(key, token)

        assertEquals(token, TokenCipher.open(key, sealed))
    }

    @Test
    fun `sealed text never contains the plaintext token`() {
        val token = "plain-token-must-not-leak"

        val sealed = TokenCipher.seal(key, token)
        val raw = String(Base64.getDecoder().decode(sealed), Charsets.ISO_8859_1)

        assertFalse(raw.contains(token))
        assertFalse(sealed.contains(token))
    }

    @Test
    fun `same token seals to different ciphertext each time`() {
        val token = "same-token"

        assertNotEquals(TokenCipher.seal(key, token), TokenCipher.seal(key, token))
    }

    @Test
    fun `tampered ciphertext is rejected`() {
        val sealed = TokenCipher.seal(key, "token")
        val raw = Base64.getDecoder().decode(sealed)
        raw[raw.size - 1] = (raw[raw.size - 1].toInt() xor 0x01).toByte()

        try {
            TokenCipher.open(key, Base64.getEncoder().encodeToString(raw))
            fail("篡改后的密文必须被拒绝")
        } catch (error: AEADBadTagException) {
            assertTrue(error.message.orEmpty().isNotEmpty())
        }
    }

    @Test
    fun `wrong key cannot open`() {
        val sealed = TokenCipher.seal(key, "token")
        val other = TokenCipher.newKey()

        try {
            TokenCipher.open(other, sealed)
            fail("错误密钥必须被拒绝")
        } catch (error: AEADBadTagException) {
            assertTrue(error.message.orEmpty().isNotEmpty())
        }
    }

    @Test
    fun `sha256 digest is stable and does not expose token`() {
        val first = TokenCipher.sha256Hex("token")
        val second = TokenCipher.sha256Hex("token")

        assertEquals(first, second)
        assertEquals(64, first.length)
        assertFalse(first.contains("token"))
    }
}
