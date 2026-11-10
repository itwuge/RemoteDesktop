// ==========================================================================================
// 文件        : AccountSessionStore.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 账号中心登录态本地存储; 令牌只以 AES-GCM 密文落盘, 密钥由 Android Keystore 托管
// 架构定位    : 本地持久层; 与桌面端账号中心的"只存摘要/密文"约束一致
// 核心功能    : TokenCipher 纯 JVM 加解密; AndroidSessionStore 负责偏好读写与密钥生命周期
// 职责边界    : 不发起网络、不校验口令; 解密失败按"无登录态"处理并清理损坏数据
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35); TokenCipher 可在 JVM 单测
// 依赖关系    : 被 RdcAccountService 调用
// ==========================================================================================

package com.rdc.remote

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** 账号中心登录态; 令牌字段只在内存与密文中出现, 不进入界面与日志. */
internal data class RemoteSession(
    val accountId: String,
    val accountName: String,
    val deviceId: String,
    val deviceToken: String,
    val sessionToken: String,
    val deviceExpiresAt: Long,
    val sessionExpiresAt: Long,
    val loginAt: Long,
    val trusted: Boolean,
) {
    fun isValid(now: Long = System.currentTimeMillis()): Boolean = deviceExpiresAt > now
}

internal interface SessionStore {
    fun load(): RemoteSession?
    fun save(session: RemoteSession)
    fun clear()
}

/** AES-256-GCM 信封: 12 字节随机 IV + 密文(含 16 字节 tag), 整体 Base64. */
internal object TokenCipher {

    private const val IV_BYTES = 12
    private const val TAG_BITS = 128

    fun newKey(): SecretKey {
        val generator = KeyGenerator.getInstance("AES")
        generator.init(256)
        return generator.generateKey()
    }

    fun seal(key: SecretKey, plaintext: String): String {
        val sealed: ByteArray
        val iv: ByteArray
        if (isKeystoreKey(key)) {
            // Keystore 密钥默认禁止调用方提供 IV, 由系统生成后从 Cipher 取回
            val cipher = cipherFor(key)
            cipher.init(Cipher.ENCRYPT_MODE, key)
            iv = cipher.iv
            sealed = cipher.doFinal(plaintext.toByteArray(Charsets.UTF_8))
        } else {
            iv = ByteArray(IV_BYTES).also { SecureRandom().nextBytes(it) }
            val cipher = cipherFor(key)
            cipher.init(Cipher.ENCRYPT_MODE, key, GCMParameterSpec(TAG_BITS, iv))
            sealed = cipher.doFinal(plaintext.toByteArray(Charsets.UTF_8))
        }
        return Base64.getEncoder().encodeToString(iv + sealed)
    }

    fun open(key: SecretKey, sealedText: String): String {
        val raw = Base64.getDecoder().decode(sealedText)
        require(raw.size > IV_BYTES) { "密文长度不足" }
        val cipher = cipherFor(key)
        cipher.init(
            Cipher.DECRYPT_MODE,
            key,
            GCMParameterSpec(TAG_BITS, raw.copyOfRange(0, IV_BYTES)),
        )
        val plaintext = cipher.doFinal(raw, IV_BYTES, raw.size - IV_BYTES)
        return String(plaintext, Charsets.UTF_8)
    }

    /**
     * Keystore 里的密钥不可导出, 必须交给 AndroidKeyStore 的 BC 适配 provider,
     * 走默认 provider 会拿到空 key 字节并抛 NullPointerException.
     */
    private fun cipherFor(key: SecretKey): Cipher =
        if (isKeystoreKey(key)) {
            Cipher.getInstance("AES/GCM/NoPadding", "AndroidKeyStoreBCWorkaround")
        } else {
            Cipher.getInstance("AES/GCM/NoPadding")
        }

    /** 不可导出即视为 Keystore 托管密钥. */
    private fun isKeystoreKey(key: SecretKey): Boolean =
        runCatching { key.encoded }.getOrNull() == null

    fun sha256Hex(text: String): String =
        MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
}

internal class AndroidSessionStore(context: Context) : SessionStore {

    private val appContext = context.applicationContext
    private val prefs = appContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    override fun load(): RemoteSession? {
        val sealedDevice = prefs.getString(KEY_DEVICE_TOKEN, null) ?: return null
        val sealedSession = prefs.getString(KEY_SESSION_TOKEN, null) ?: return null
        return try {
            val key = secretKey()
            RemoteSession(
                accountId = prefs.getString(KEY_ACCOUNT_ID, "") ?: "",
                accountName = prefs.getString(KEY_ACCOUNT_NAME, "") ?: "",
                deviceId = prefs.getString(KEY_DEVICE_ID, "") ?: "",
                deviceToken = TokenCipher.open(key, sealedDevice),
                sessionToken = TokenCipher.open(key, sealedSession),
                deviceExpiresAt = prefs.getLong(KEY_DEVICE_EXPIRES, 0L),
                sessionExpiresAt = prefs.getLong(KEY_SESSION_EXPIRES, 0L),
                loginAt = prefs.getLong(KEY_LOGIN_AT, 0L),
                trusted = prefs.getBoolean(KEY_TRUSTED, false),
            )
        } catch (error: Exception) {
            RdcLog.warn(TAG, "session_load_failed", error.javaClass.simpleName)
            clear()
            null
        }
    }

    override fun save(session: RemoteSession) {
        val key = secretKey()
        prefs.edit()
            .putString(KEY_ACCOUNT_ID, session.accountId)
            .putString(KEY_ACCOUNT_NAME, session.accountName)
            .putString(KEY_DEVICE_ID, session.deviceId)
            .putString(KEY_DEVICE_TOKEN, TokenCipher.seal(key, session.deviceToken))
            .putString(KEY_SESSION_TOKEN, TokenCipher.seal(key, session.sessionToken))
            .putLong(KEY_DEVICE_EXPIRES, session.deviceExpiresAt)
            .putLong(KEY_SESSION_EXPIRES, session.sessionExpiresAt)
            .putLong(KEY_LOGIN_AT, session.loginAt)
            .putBoolean(KEY_TRUSTED, session.trusted)
            .apply()
        // 便于排查令牌是否已被替换: 只记录摘要, 原文不落盘也不进日志
        RdcLog.info(TAG, "session_saved", "device_token_sha256=${TokenCipher.sha256Hex(session.deviceToken)}")
    }

    override fun clear() {
        prefs.edit().clear().apply()
    }

    /** 密钥常驻 Android Keystore, 不可导出; 应用卸载时随 Keystore 一并清理. */
    private fun secretKey(): SecretKey {
        synchronized(LOCK) {
            val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
            val existing = keyStore.getKey(KEY_ALIAS, null) as? SecretKey
            if (existing != null) {
                return existing
            }
            val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
            generator.init(
                KeyGenParameterSpec.Builder(
                    KEY_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setKeySize(256)
                    .build(),
            )
            return generator.generateKey()
        }
    }

    private companion object {
        const val TAG = "AccountStore"
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val KEY_ALIAS = "rdc-account-vault-v1"
        const val PREFS = "rdc_account_remote"
        const val KEY_ACCOUNT_ID = "account_id"
        const val KEY_ACCOUNT_NAME = "account_name"
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_DEVICE_TOKEN = "device_token_cipher"
        const val KEY_SESSION_TOKEN = "session_token_cipher"
        const val KEY_DEVICE_EXPIRES = "device_expires_at"
        const val KEY_SESSION_EXPIRES = "session_expires_at"
        const val KEY_LOGIN_AT = "login_at"
        const val KEY_TRUSTED = "trusted"
        val LOCK = Any()
    }
}
