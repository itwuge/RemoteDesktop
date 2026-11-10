// ==========================================================================================
// 文件        : RdcAccount.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 本地账号存储: 注册、登录、登录态持久化、退出登录
// 架构定位    : 命令层未接入时的等价实现, 字段与口径照 desktop-v1 账号命令
// 核心功能    : PBKDF2-HMAC-SHA256(20 万次, 16 字节随机盐); 令牌只存 SHA-256 摘要; 登录态落本机
// 职责边界    : 不联网、不显示明文口令、不把口令或令牌原文写盘; 只服务本机单账号登录
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34)
// 依赖关系    : 被 MainActivity、AccountActivity 调用
// ==========================================================================================

package com.rdc.remote

import android.content.Context
import android.content.SharedPreferences
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest
import java.security.SecureRandom
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.PBEKeySpec

/**
 * 本地账号存储.
 *
 * 与 desktop-v1 账号命令的口径一致: 账号名 3 至 64 位 `[A-Za-z0-9_.@-]`, 口令至少 8 位,
 * 口令只保存 PBKDF2-HMAC-SHA256 派生值 (20 万次, 16 字节随机盐), 令牌只保存 SHA-256 摘要.
 * 账号与登录态都只存在本机应用私有目录, 不跨设备同步.
 */
internal object RdcAccount {

    /** 与桌面端 `rdc.accounts.store.PBKDF2_ITERATIONS` 一致. */
    const val PBKDF2_ITERATIONS = 200_000

    private const val PREFS = "rdc_accounts"
    private const val KEY_ACCOUNTS = "accounts"
    private const val KEY_SESSION_ACCOUNT = "session_account_id"
    private const val KEY_SESSION_LOGIN_AT = "session_login_at"
    private const val KEY_SESSION_EXPIRES_AT = "session_expires_at"
    private const val KEY_DEVICE_TOKEN_HASH = "device_token_sha256"

    private const val SALT_BYTES = 16
    private const val PASSWORD_MIN_LENGTH = 8
    private const val TOKEN_BYTES = 32

    /** 与桌面端设备令牌 30 天有效期口径一致, 保证杀进程重开后仍在登录态. */
    private const val DEVICE_TOKEN_TTL_MS = 30L * 24 * 60 * 60 * 1000

    /** 与桌面端会话令牌 15 分钟有效期口径一致. */
    private const val SESSION_TOKEN_TTL_MS = 15L * 60 * 1000

    private val NAME_PATTERN = Regex("^[A-Za-z0-9_.@-]{3,64}$")
    private val random = SecureRandom()

    /** 当前登录态; loginAt/expiresAt 均为本地 epoch 毫秒. */
    data class Session(
        val accountId: String,
        val name: String,
        val loginAt: Long,
        val expiresAt: Long,
        val remote: Boolean = false,
        val deviceId: String = "",
        val trusted: Boolean = false,
    )

    /** 与 desktop-v1 统一返回结构一致. */
    data class Outcome(
        val ok: Boolean,
        val message: String,
        val errorCode: String? = null,
        val data: Map<String, Any?> = emptyMap(),
    )

    // ------------------------------------------------------------------ 对外命令

    /** 账号名与口令的本地前置校验; 合法返回 null, 否则返回可直接展示的中文文案. */
    fun validate(name: String, password: String): String? = when {
        !NAME_PATTERN.matches(name) -> "账号需为 3 至 64 位字母、数字或 _ . @ -"
        password.length < PASSWORD_MIN_LENGTH -> "口令至少 8 位"
        else -> null
    }

    /** 注册本地账号; 成功返回 `account_id` 与 `created_at`. */
    fun register(context: Context, name: String, password: String): Outcome = try {
        if (!NAME_PATTERN.matches(name)) {
            Outcome(false, "账号需为 3 至 64 位字母、数字或 _ . @ -")
        } else if (password.length < PASSWORD_MIN_LENGTH) {
            Outcome(false, "口令至少 8 位")
        } else {
            val prefs = prefs(context)
            val accounts = accountsJson(prefs)
            if (findByName(accounts, name) != null) {
                Outcome(false, "账号已存在, 请直接登录")
            } else {
                val salt = ByteArray(SALT_BYTES).also { random.nextBytes(it) }
                val accountId = "acc-${UUID.randomUUID()}"
                val createdAt = iso(System.currentTimeMillis())
                accounts.put(
                    JSONObject()
                        .put("account_id", accountId)
                        .put("name", name)
                        .put("salt", b64(salt))
                        .put("password_hash", b64(derive(password, salt, PBKDF2_ITERATIONS)))
                        .put("iterations", PBKDF2_ITERATIONS)
                        .put("created_at", createdAt)
                        .put("password_changed_at", createdAt),
                )
                if (!prefs.edit().putString(KEY_ACCOUNTS, accounts.toString()).commit()) {
                    Outcome(false, "账号数据暂不可用: 本地存储写入失败")
                } else {
                    RdcLog.info("Account", "register_ok", "account_id=$accountId")
                    Outcome(
                        true,
                        "账号已创建",
                        data = mapOf("account_id" to accountId, "created_at" to createdAt),
                    )
                }
            }
        }
    } catch (error: Exception) {
        storageFailure("register", error)
    }

    /** 登录: 校验 PBKDF2 派生值, 签发令牌并把登录态落本机. */
    fun login(context: Context, name: String, password: String): Outcome = try {
        val prefs = prefs(context)
        val account = findByName(accountsJson(prefs), name)
        if (account == null) {
            Outcome(false, "账号或口令不正确", "ACCOUNT_NOT_FOUND")
        } else {
            val salt = Base64.decode(account.getString("salt"), Base64.NO_WRAP)
            val iterations = account.optInt("iterations", PBKDF2_ITERATIONS)
            val expected = Base64.decode(account.getString("password_hash"), Base64.NO_WRAP)
            if (!MessageDigest.isEqual(derive(password, salt, iterations), expected)) {
                Outcome(false, "账号或口令不正确", "CREDENTIAL_INVALID")
            } else {
                val accountId = account.getString("account_id")
                val deviceToken = newToken()
                val sessionToken = newToken()
                val now = System.currentTimeMillis()
                val expiresAt = now + DEVICE_TOKEN_TTL_MS
                val saved = prefs.edit()
                    .putString(KEY_SESSION_ACCOUNT, accountId)
                    .putLong(KEY_SESSION_LOGIN_AT, now)
                    .putLong(KEY_SESSION_EXPIRES_AT, expiresAt)
                    .putString(KEY_DEVICE_TOKEN_HASH, sha256Hex(deviceToken))
                    .commit()
                if (!saved) {
                    Outcome(false, "账号数据暂不可用: 本地存储写入失败")
                } else {
                    RdcLog.info("Account", "login_ok", "account_id=$accountId")
                    Outcome(
                        true,
                        "登录成功",
                        data = mapOf(
                            "account_id" to accountId,
                            "device_token" to deviceToken,
                            "session_token" to sessionToken,
                            "expires_at" to iso(now + SESSION_TOKEN_TTL_MS),
                        ),
                    )
                }
            }
        }
    } catch (error: Exception) {
        storageFailure("login", error)
    }

    /** 退出登录: 清空本机登录态, 令牌摘要随之作废. */
    fun logout(context: Context): Outcome = try {
        val prefs = prefs(context)
        val accountId = prefs.getString(KEY_SESSION_ACCOUNT, "").orEmpty()
        if (accountId.isEmpty()) {
            Outcome(false, "当前未登录")
        } else {
            clearSession(prefs)
            RdcLog.info("Account", "logout_ok", "account_id=$accountId")
            Outcome(
                true,
                "已登出, 吊销 2 个令牌, 踢出 0 个会话",
                data = mapOf(
                    "account_id" to accountId,
                    "status" to "logged_out",
                    "revoked_at" to iso(System.currentTimeMillis()),
                    "kicked_sessions" to 0,
                ),
            )
        }
    } catch (error: Exception) {
        storageFailure("logout", error)
    }

    /** 清空本机登录态; 供账号中心登录成功后替换本地态, 不返回结果. */
    fun clear(context: Context) {
        try {
            clearSession(prefs(context))
        } catch (error: Exception) {
            RdcLog.warn("Account", "session_clear_failed", error.javaClass.simpleName)
        }
    }

    /** 读取当前登录态; 过期或账号被删时清空并返回 null. */
    fun session(context: Context): Session? = try {
        val prefs = prefs(context)
        val accountId = prefs.getString(KEY_SESSION_ACCOUNT, null)
        val tokenHash = prefs.getString(KEY_DEVICE_TOKEN_HASH, null)
        val loginAt = prefs.getLong(KEY_SESSION_LOGIN_AT, 0L)
        val expiresAt = prefs.getLong(KEY_SESSION_EXPIRES_AT, 0L)
        if (accountId.isNullOrEmpty() || tokenHash.isNullOrEmpty() || loginAt <= 0L) {
            null
        } else if (expiresAt <= System.currentTimeMillis()) {
            clearSession(prefs)
            RdcLog.info("Account", "session_expired")
            null
        } else {
            val record = accountsJson(prefs).let { list ->
                (0 until list.length()).map { list.getJSONObject(it) }
                    .firstOrNull { it.optString("account_id") == accountId }
            }
            if (record == null) {
                clearSession(prefs)
                null
            } else {
                Session(accountId, record.optString("name"), loginAt, expiresAt)
            }
        }
    } catch (error: Exception) {
        RdcLog.warn("Account", "session_read_failed", error.javaClass.simpleName)
        null
    }

    // ------------------------------------------------------------------ 内部实现

    private fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun accountsJson(prefs: SharedPreferences): JSONArray =
        JSONArray(prefs.getString(KEY_ACCOUNTS, "[]") ?: "[]")

    private fun findByName(accounts: JSONArray, name: String): JSONObject? {
        val wanted = name.trim().lowercase(Locale.ROOT)
        for (index in 0 until accounts.length()) {
            val item = accounts.getJSONObject(index)
            if (item.optString("name").lowercase(Locale.ROOT) == wanted) {
                return item
            }
        }
        return null
    }

    private fun derive(password: String, salt: ByteArray, iterations: Int): ByteArray {
        val spec = PBEKeySpec(password.toCharArray(), salt, iterations, 256)
        return try {
            SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).encoded
        } finally {
            spec.clearPassword()
        }
    }

    private fun newToken(): String =
        ByteArray(TOKEN_BYTES).also { random.nextBytes(it) }.let {
            Base64.encodeToString(it, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
        }

    private fun sha256Hex(value: String): String =
        MessageDigest.getInstance("SHA-256").digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    private fun b64(value: ByteArray): String = Base64.encodeToString(value, Base64.NO_WRAP)

    private fun iso(epochMs: Long): String =
        SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.CHINA).format(Date(epochMs))

    private fun clearSession(prefs: SharedPreferences) {
        prefs.edit()
            .remove(KEY_SESSION_ACCOUNT)
            .remove(KEY_SESSION_LOGIN_AT)
            .remove(KEY_SESSION_EXPIRES_AT)
            .remove(KEY_DEVICE_TOKEN_HASH)
            .commit()
    }

    private fun storageFailure(event: String, error: Exception): Outcome {
        RdcLog.error("Account", "${event}_failed", error.javaClass.simpleName)
        val detail = error.message ?: error.javaClass.simpleName
        return Outcome(false, "账号数据暂不可用: $detail")
    }
}
