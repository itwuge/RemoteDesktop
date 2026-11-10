// ==========================================================================================
// 文件        : RdcCrypto.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 控制端本地密码学原语: 规范 JSON、Ed25519 身份、X25519 协商、HKDF 与 ChaCha20-Poly1305
// 架构定位    : 与 Python 参考实现 src/rdc/crypto 与 poc/common/crypto 逐项对齐的移植层
// 核心功能    : device_id 由公钥 SHA-256 的 Base32 前 20 位生成; 七字段包络签名; AEAD 帧加解密
// 职责边界    : 不建连、不发包、不写业务状态; 只做原语与编解码
// 运行环境    : Android 13 (API 33) 起平台提供 Ed25519/X25519; 低于此版本给出明确中文错误
// 依赖关系    : 被 LanSession 与 ControlContract 调用
// ==========================================================================================

package com.rdc.remote

import android.content.Context
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.SecureRandom
import java.security.Signature
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.SecretKeySpec

/** 标准 Base64, 与 Python base64.b64encode 一致, 不带换行. */
internal fun b64(raw: ByteArray): String = Base64.getEncoder().encodeToString(raw)

/** 标准 Base64 解码; 非法输入抛 IllegalArgumentException. */
internal fun b64d(text: String): ByteArray = Base64.getDecoder().decode(text)

/**
 * 规范 JSON: UTF-8、键名按字典序、无多余空白、不转义非必要字符.
 * 与 Python json.dumps(ensure_ascii=False, sort_keys=True, separators=(",", ":")) 对齐.
 */
internal object CanonicalJson {

    fun bytes(value: Any?): ByteArray = encode(value).toByteArray(Charsets.UTF_8)

    fun encode(value: Any?): String = StringBuilder().also { write(it, value) }.toString()

    private fun write(sb: StringBuilder, value: Any?) {
        when (value) {
            null -> sb.append("null")
            is String -> writeString(sb, value)
            is Boolean -> sb.append(if (value) "true" else "false")
            is Byte, is Short, is Int, is Long -> sb.append(value.toString())
            is Float, is Double -> sb.append(value.toString())
            is Map<*, *> -> {
                sb.append('{')
                val entries = value.entries
                    .map { it.key.toString() to it.value }
                    .sortedBy { it.first }
                entries.forEachIndexed { index, entry ->
                    if (index > 0) {
                        sb.append(',')
                    }
                    writeString(sb, entry.first)
                    sb.append(':')
                    write(sb, entry.second)
                }
                sb.append('}')
            }
            is Iterable<*> -> {
                sb.append('[')
                var first = true
                for (item in value) {
                    if (!first) {
                        sb.append(',')
                    }
                    first = false
                    write(sb, item)
                }
                sb.append(']')
            }
            is Array<*> -> write(sb, value.asList())
            else -> writeString(sb, value.toString())
        }
    }

    private fun writeString(sb: StringBuilder, text: String) {
        sb.append('"')
        for (char in text) {
            when (char) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                '\b' -> sb.append("\\b")
                '\u000C' -> sb.append("\\f")
                else -> {
                    if (char.code < 0x20) {
                        sb.append("\\u")
                        sb.append(char.code.toString(16).padStart(4, '0'))
                    } else {
                        sb.append(char)
                    }
                }
            }
        }
        sb.append('"')
    }
}

/**
 * 指定密码学提供方.
 *
 * 部分系统(如 MIUI)会优先把 Ed25519/X25519 解析到 AndroidKeyStore,
 * 该实现必须显式初始化才能生成密钥, 否则抛 IllegalStateException: Not initialized.
 * 这里优先走随包的 BouncyCastle, 其次 Conscrypt(AndroidOpenSSL), 都不可用才报不支持.
 */
internal object RdcProviders {

    private val CANDIDATES = arrayOf("BC", "AndroidOpenSSL")

    /** 部分 ROM 的 Conscrypt 不带 Ed25519; 注册随包的 BouncyCastle 作为兜底. */
    private val ready: Boolean by lazy {
        try {
            // Android 自带的 "BC" 是裁剪版且不含 Ed25519, 版本较旧时必须替换成随包版本
            val existing = java.security.Security.getProvider("BC")
            if (existing == null || existing.version < 1.70) {
                java.security.Security.removeProvider("BC")
                java.security.Security.insertProviderAt(
                    org.bouncycastle.jce.provider.BouncyCastleProvider(),
                    1,
                )
            }
            val installed = java.security.Security.getProvider("BC")
            val supported = installed?.getService("KeyPairGenerator", "Ed25519") != null
            RdcLog.info(
                "Crypto",
                "provider_ready",
                "bc=${installed?.version} ed25519=$supported",
            )
            supported
        } catch (error: Throwable) {
            RdcLog.error("Crypto", "provider_failed", error.toString())
            false
        }
    }

    fun keyPair(algorithm: String): KeyPairGenerator {
        ensureProvider()
        for (name in CANDIDATES) {
            try {
                return KeyPairGenerator.getInstance(algorithm, name)
            } catch (_: Exception) {
            }
        }
        throw java.security.NoSuchAlgorithmException(algorithm)
    }

    fun signature(algorithm: String): Signature {
        ensureProvider()
        for (name in CANDIDATES) {
            try {
                return Signature.getInstance(algorithm, name)
            } catch (_: Exception) {
            }
        }
        throw java.security.NoSuchAlgorithmException(algorithm)
    }

    fun keyFactory(algorithm: String): KeyFactory {
        ensureProvider()
        for (name in CANDIDATES) {
            try {
                return KeyFactory.getInstance(algorithm, name)
            } catch (_: Exception) {
            }
        }
        throw java.security.NoSuchAlgorithmException(algorithm)
    }

    fun keyAgreement(algorithm: String): KeyAgreement {
        ensureProvider()
        for (name in CANDIDATES) {
            try {
                return KeyAgreement.getInstance(algorithm, name)
            } catch (_: Exception) {
            }
        }
        throw java.security.NoSuchAlgorithmException(algorithm)
    }

    fun cipher(algorithm: String): Cipher {
        ensureProvider()
        for (name in CANDIDATES) {
            try {
                return Cipher.getInstance(algorithm, name)
            } catch (_: Exception) {
            }
        }
        return Cipher.getInstance(algorithm)
    }

    private fun ensureProvider() {
        // 触发 lazy 初始化
        ready
    }
}

/** 设备身份: Ed25519 私钥常驻本机私有偏好, device_id 由公钥摘要生成. */
internal class RdcIdentity private constructor(
    private val privateKey: PrivateKey,
    val publicKey: ByteArray,
) {

    val deviceId: String = base32(sha256(publicKey)).take(20)

    fun sign(data: ByteArray): ByteArray {
        val signer = RdcProviders.signature("Ed25519")
        signer.initSign(privateKey)
        signer.update(data)
        return signer.sign()
    }

    /** 私钥与公钥是否配对; 存储损坏或提供方不兼容时返回 false. */
    fun selfCheck(): Boolean {
        val probe = "rdc-identity-self-check".toByteArray()
        return try {
            verify(publicKey, probe, sign(probe))
        } catch (_: Exception) {
            false
        }
    }

    companion object {
        private const val PREFS = "rdc_identity"
        private const val KEY_PRIVATE = "ed25519_private_pkcs8"
        private const val KEY_PUBLIC = "ed25519_public"

        private val ED_PUBLIC_PREFIX = byteArrayOf(
            0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00,
        )

        fun load(context: Context): RdcIdentity {
            val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val storedPrivate = prefs.getString(KEY_PRIVATE, null)
            val storedPublic = prefs.getString(KEY_PUBLIC, null)
            if (storedPrivate != null && storedPublic != null) {
                try {
                    val identity = build(hexToBytes(storedPrivate), hexToBytes(storedPublic))
                    if (identity.selfCheck()) {
                        return identity
                    }
                    RdcLog.warn("Crypto", "identity_selfcheck_failed", "重新生成本机身份")
                } catch (error: Exception) {
                    RdcLog.warn("Crypto", "identity_load_failed", error.toString())
                }
            }
            val generator = RdcProviders.keyPair("Ed25519")
            val pair = generator.generateKeyPair()
            val public = rawTail(pair.public.encoded)
            val privateEncoded = pair.private.encoded
            prefs.edit()
                .putString(KEY_PRIVATE, bytesToHex(privateEncoded))
                .putString(KEY_PUBLIC, bytesToHex(public))
                .apply()
            val identity = build(privateEncoded, public)
            RdcLog.info(
                "Crypto",
                "identity_created",
                "device=${identity.deviceId} selfcheck=${identity.selfCheck()}",
            )
            return identity
        }

        private fun build(privateEncoded: ByteArray, public: ByteArray): RdcIdentity {
            val factory = RdcProviders.keyFactory("Ed25519")
            val privateKey = factory.generatePrivate(PKCS8EncodedKeySpec(privateEncoded))
            return RdcIdentity(privateKey, public)
        }

        fun verify(publicKey: ByteArray, data: ByteArray, signature: ByteArray): Boolean {
            return try {
                val factory = RdcProviders.keyFactory("Ed25519")
                val key = factory.generatePublic(X509EncodedKeySpec(ED_PUBLIC_PREFIX + publicKey))
                val verifier = RdcProviders.signature("Ed25519")
                verifier.initVerify(key)
                verifier.update(data)
                verifier.verify(signature)
            } catch (error: Exception) {
                false
            }
        }

        fun deviceIdFromPublicKey(publicKey: ByteArray): String = base32(sha256(publicKey)).take(20)

        private fun rawTail(encoded: ByteArray): ByteArray = encoded.copyOfRange(encoded.size - 32, encoded.size)
    }
}

/** 一次性 X25519 临时密钥对. */
internal class X25519Pair private constructor(
    val privateKey: PrivateKey,
    val publicKey: ByteArray,
) {
    companion object {
        private val X_PUBLIC_PREFIX = byteArrayOf(
            0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x6e, 0x03, 0x21, 0x00,
        )
        private val X_PRIVATE_PREFIX = byteArrayOf(
            0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x6e,
            0x04, 0x22, 0x04, 0x20,
        )

        fun generate(): X25519Pair {
            val pair = RdcProviders.keyPair("X25519").generateKeyPair()
            return X25519Pair(pair.private, pair.public.encoded.copyOfRange(pair.public.encoded.size - 32, pair.public.encoded.size))
        }

        fun publicKey(raw: ByteArray) = RdcProviders.keyFactory("X25519")
            .generatePublic(X509EncodedKeySpec(X_PUBLIC_PREFIX + raw))

        fun privateKey(raw: ByteArray) = RdcProviders.keyFactory("X25519")
            .generatePrivate(PKCS8EncodedKeySpec(X_PRIVATE_PREFIX + raw))
    }
}

/** X25519 共享密钥计算. */
internal fun x25519Shared(privateKey: PrivateKey, peerRaw: ByteArray): ByteArray {
    val agreement = RdcProviders.keyAgreement("X25519")
    agreement.init(privateKey)
    agreement.doPhase(X25519Pair.publicKey(peerRaw), true)
    return agreement.generateSecret()
}

/** HKDF-SHA256, 与 Python cryptography HKDF(length=64, info=b"rdc-poc-v1") 一致. */
internal fun hkdfSha256(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int): ByteArray {
    val mac = Mac.getInstance("HmacSHA256")
    val effectiveSalt = if (salt.isEmpty()) ByteArray(32) else salt
    mac.init(SecretKeySpec(effectiveSalt, "HmacSHA256"))
    val prk = mac.doFinal(ikm)
    val output = ByteArray(length)
    var block = ByteArray(0)
    var offset = 0
    var counter = 1
    while (offset < length) {
        mac.init(SecretKeySpec(prk, "HmacSHA256"))
        mac.update(block)
        mac.update(info)
        mac.update(counter.toByte())
        block = mac.doFinal()
        val take = minOf(block.size, length - offset)
        System.arraycopy(block, 0, output, offset, take)
        offset += take
        counter++
    }
    return output
}

/**
 * 双向 AEAD 通道: 4 字节随机前缀 + 8 字节大端计数器组成 12 字节 nonce,
 * 密文为 ChaCha20-Poly1305 输出 (含 16 字节 tag).
 */
internal class AeadChannel(private val sendKey: ByteArray, private val recvKey: ByteArray) {

    private val sendPrefix = ByteArray(4).also { SecureRandom().nextBytes(it) }
    private var sendCounter = 0L
    private var recvPrefix: ByteArray? = null

    /** 接收侧 64 位滑动窗口: 记录已成功认证的计数器, 与 poc/common/crypto.py 一致. */
    private var recvHighest = -1L
    private var recvBitmap = 0L

    fun encrypt(payload: ByteArray): ByteArray = synchronized(this) {
        val counter = ByteArray(8)
        var value = sendCounter
        for (index in 7 downTo 0) {
            counter[index] = (value and 0xff).toByte()
            value = value ushr 8
        }
        val nonce = sendPrefix + counter
        val cipher = RdcProviders.cipher("ChaCha20-Poly1305")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(sendKey, "ChaCha20"), IvParameterSpec(nonce))
        val ciphertext = cipher.doFinal(payload)
        sendCounter++
        sendPrefix + counter + ciphertext
    }

    fun decrypt(data: ByteArray): ByteArray = synchronized(this) {
        if (data.size < 12 + 16) {
            throw IllegalArgumentException("密文长度不足")
        }
        val prefix = data.copyOfRange(0, 4)
        val existing = recvPrefix
        if (existing == null) {
            recvPrefix = prefix
        } else if (!existing.contentEquals(prefix)) {
            throw IllegalArgumentException("nonce 前缀与首包不一致")
        }
        var counter = 0L
        for (index in 4 until 12) {
            counter = (counter shl 8) or (data[index].toLong() and 0xff)
        }
        checkCounter(counter)
        val nonce = data.copyOfRange(0, 12)
        val cipher = RdcProviders.cipher("ChaCha20-Poly1305")
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(recvKey, "ChaCha20"), IvParameterSpec(nonce))
        val plaintext = cipher.doFinal(data, 12, data.size - 12)
        commitCounter(counter)
        plaintext
    }

    /** 认证前先查计数器: 已接收或落后窗口外直接拒绝, 避免重放与乱序攻击. */
    private fun checkCounter(counter: Long) {
        if (recvHighest < 0L) {
            return
        }
        if (counter > recvHighest) {
            return
        }
        val distance = recvHighest - counter
        if (distance >= RECV_REPLAY_WINDOW) {
            throw IllegalArgumentException("计数器落后接收窗口, 已断开")
        }
        if (((recvBitmap ushr distance.toInt()) and 1L) != 0L) {
            throw IllegalArgumentException("检测到重放的密文帧")
        }
    }

    /** 认证通过后登记计数器, 供后续帧去重. */
    private fun commitCounter(counter: Long) {
        if (recvHighest < 0L) {
            recvHighest = counter
            recvBitmap = 1L
            return
        }
        if (counter > recvHighest) {
            val shift = counter - recvHighest
            recvBitmap = if (shift >= RECV_REPLAY_WINDOW) 0L else recvBitmap shl shift.toInt()
            recvBitmap = recvBitmap or 1L
            recvHighest = counter
            return
        }
        recvBitmap = recvBitmap or (1L shl (recvHighest - counter).toInt())
    }
}

private const val RECV_REPLAY_WINDOW = 64L

/** 由 X25519 结果派生会话通道; initiator=true 时发送 key_a. */
internal fun deriveAeadSession(
    shared: ByteArray,
    salt: ByteArray,
    initiator: Boolean,
    context: ByteArray = ByteArray(0),
): AeadChannel {
    val info = if (context.isEmpty()) {
        "rdc-poc-v1".toByteArray(Charsets.UTF_8)
    } else {
        "rdc-poc-v1".toByteArray(Charsets.UTF_8) + "|".toByteArray(Charsets.UTF_8) + context
    }
    val effectiveSalt = if (context.isEmpty()) salt else salt + "|".toByteArray(Charsets.UTF_8) + context
    val material = hkdfSha256(shared, effectiveSalt, info, 64)
    val keyA = material.copyOfRange(0, 32)
    val keyB = material.copyOfRange(32, 64)
    return if (initiator) AeadChannel(keyA, keyB) else AeadChannel(keyB, keyA)
}

private fun sha256(data: ByteArray): ByteArray = MessageDigest.getInstance("SHA-256").digest(data)

/** RFC4648 Base32, 字母表大写, 不带填充; 与 Python b32encode 前 20 位一致. */
internal fun base32(bytes: ByteArray): String {
    val alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    val sb = StringBuilder()
    var buffer = 0
    var bits = 0
    for (byte in bytes) {
        buffer = (buffer shl 8) or (byte.toInt() and 0xff)
        bits += 8
        while (bits >= 5) {
            sb.append(alphabet[(buffer ushr (bits - 5)) and 0x1f])
            bits -= 5
        }
    }
    if (bits > 0) {
        sb.append(alphabet[(buffer shl (5 - bits)) and 0x1f])
    }
    return sb.toString()
}

private fun bytesToHex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it) }

private fun hexToBytes(text: String): ByteArray {
    if (text.length % 2 != 0) {
        return ByteArray(0)
    }
    return ByteArray(text.length / 2) { index ->
        text.substring(index * 2, index * 2 + 2).toInt(16).toByte()
    }
}
