// ==========================================================================================
// 文件        : RemoteAccountClient.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 跨端账号中心 /v1 客户端: 注册、登录、设备列表、登出与令牌失效判定
// 架构定位    : 数据层; 与桌面端 src/rdc/accounts/remote.py 逐字段对齐
// 核心功能    : 设备挑战 + Ed25519 声明签名登录; 统一包络解析; 写操作幂等键; 中文错误映射
// 职责边界    : 不读写本地存储、不建界面、不碰业务状态; 传输与设备身份均从构造注入
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35); JVM 单测用注入桩
// 依赖关系    : 被 RdcAccountService 调用; 传输实现见 AndroidAccountTransport
// ==========================================================================================

package com.rdc.remote

import org.json.JSONArray
import org.json.JSONObject
import java.security.SecureRandom
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/** 账号中心 HTTP 传输抽象; 生产实现走 TLS 1.3, 单测用注入桩. */
internal interface AccountTransport {
    fun request(method: String, path: String, headers: Map<String, String>, body: ByteArray): AccountHttpResponse
}

internal data class AccountHttpResponse(val status: Int, val body: String)

/** 账号中心明确拒绝; code 来自服务端包络, 未知时给 STORAGE_UNAVAILABLE. */
internal class RemoteRejected(val status: Int, val code: String, message: String) : Exception(message)

/** 账号中心不可达; message 已是可直接展示的中文. */
internal class RemoteUnavailable(message: String, cause: Throwable? = null) : Exception(message, cause)

/** 账号中心返回格式无法识别. */
internal class RemoteBadResponse(message: String) : Exception(message)

/** 设备身份端口; 生产由 RdcIdentity 提供, 单测注入固定值. */
internal interface AccountIdentity {
    val deviceId: String
    val publicKeyB64: String
    fun sign(payload: Map<String, String>): String
}

/** 用本机 Ed25519 身份实现账号中心声明签名. */
internal class RdcAccountIdentity(private val identity: RdcIdentity) : AccountIdentity {
    override val deviceId: String = identity.deviceId
    override val publicKeyB64: String = b64(identity.publicKey)
    override fun sign(payload: Map<String, String>): String =
        b64(identity.sign(CanonicalJson.bytes(payload)))
}

internal data class AccountLoginResult(
    val accountId: String,
    val accountName: String,
    val deviceId: String,
    val deviceToken: String,
    val sessionToken: String,
    val deviceExpiresAt: Long,
    val sessionExpiresAt: Long,
    val trusted: Boolean,
)

internal data class AccountRegisterResult(
    val accountId: String,
    val accountName: String,
    val createdAt: String,
)

internal data class AccountDeviceInfo(
    val deviceId: String,
    val displayName: String,
    val platform: String,
    val online: Boolean,
    val presenceState: String,
    val lastSeenAt: String,
    val trusted: Boolean,
    val current: Boolean,
    /** 受信来源: device(设备自身) / owner(账号所有者); 未受信为空. */
    val trustSource: String = "",
    /** 授信时间 ISO 8601; 未受信为空. */
    val trustedAt: String = "",
    /** 最近连接时间 ISO 8601; 从未连接为空. */
    val lastConnectedAt: String = "",
)

internal data class AccountLogoutResult(val revokedTokens: Int, val kickedConnections: Int)

/** A4: 消息与审计项, 字段与桌面 list_messages 一致. */
internal data class AccountMessage(
    val messageId: String,
    val ts: String,
    val category: String,
    val categoryLabel: String,
    val title: String,
    val event: String,
    val deviceId: String,
    val deviceName: String,
    val actor: String,
    val result: String,
    val reason: String,
)

internal data class AccountMessagePage(
    val items: List<AccountMessage>,
    val unread: Int,
    val total: Int,
    val readUntil: String,
)

internal data class AccountReadResult(val readUntil: String, val cleared: Int)

private const val MESSAGE_LIMIT = 200

/** JSON null 经 optString 会变成字符串 "null"; 统一折成空串, 避免界面露出英文 null. */
private fun JSONObject.textOrEmpty(key: String): String =
    optString(key).takeIf { it != "null" } ?: ""

internal class RemoteAccountClient(
    private val endpoint: String,
    private val identity: AccountIdentity,
    private val transport: AccountTransport,
    private val devicePlatform: String = "android",
) {

    fun register(account: String, password: String): AccountRegisterResult {
        val data = request("POST", "/v1/accounts/register", JSONObject().apply {
            put("account", account)
            put("password", password)
        })
        return AccountRegisterResult(
            accountId = data.optString("account_id"),
            accountName = data.optString("account_name"),
            createdAt = data.optString("created_at"),
        )
    }

    fun login(account: String, password: String, deviceName: String): AccountLoginResult {
        val challenge = request("POST", "/v1/devices/challenges", JSONObject().apply {
            put("device_id", identity.deviceId)
            put("device_public_key", identity.publicKeyB64)
        })
        val nonce = challenge.optString("nonce")
        if (nonce.isBlank()) {
            throw RemoteRejected(502, CODE_STORAGE_UNAVAILABLE, "账号中心未返回设备挑战")
        }
        val signature = identity.sign(
            linkedMapOf("account" to account, "device_id" to identity.deviceId, "nonce" to nonce),
        )
        val data = request("POST", "/v1/accounts/login", JSONObject().apply {
            put("account", account)
            put("password", password)
            put("device_id", identity.deviceId)
            put("device_name", deviceName.ifBlank { identity.deviceId })
            put("device_platform", devicePlatform)
            put("device_public_key", identity.publicKeyB64)
            put("nonce", nonce)
            put("signature", signature)
        })
        val deviceToken = data.optString("device_token")
        val sessionToken = data.optString("session_token")
        if (deviceToken.isBlank() || sessionToken.isBlank()) {
            throw RemoteBadResponse("账号中心登录响应缺少令牌")
        }
        val now = System.currentTimeMillis()
        return AccountLoginResult(
            accountId = data.optString("account_id"),
            accountName = data.optString("account_name"),
            deviceId = data.optString("device_id").ifBlank { identity.deviceId },
            deviceToken = deviceToken,
            sessionToken = sessionToken,
            deviceExpiresAt = parseIsoMillis(data.optString("device_expires_at")) ?: (now + DEVICE_TOKEN_TTL_MS),
            sessionExpiresAt = parseIsoMillis(data.optString("expires_at")) ?: (now + SESSION_TOKEN_TTL_MS),
            trusted = data.optBoolean("trusted", false),
        )
    }

    fun logout(deviceToken: String): AccountLogoutResult {
        val data = request("POST", "/v1/accounts/logout", JSONObject(), token = deviceToken)
        return AccountLogoutResult(
            revokedTokens = data.optInt("revoked_tokens", 0),
            kickedConnections = data.optInt("kicked_connections", 0),
        )
    }

    fun me(deviceToken: String): AccountDeviceInfo? {
        val data = request("GET", "/v1/accounts/me", token = deviceToken)
        val deviceId = data.textOrEmpty("device_id")
        if (deviceId.isBlank()) {
            return null
        }
        return AccountDeviceInfo(
            deviceId = deviceId,
            displayName = deviceId,
            platform = data.textOrEmpty("platform").ifBlank { "unknown" },
            online = true,
            presenceState = "online",
            lastSeenAt = "",
            trusted = data.optBoolean("trusted", false),
            current = true,
            trustSource = data.textOrEmpty("trust_source"),
            trustedAt = data.textOrEmpty("trusted_at"),
        )
    }

    fun devices(deviceToken: String): List<AccountDeviceInfo> {
        val data = request("GET", "/v1/devices", token = deviceToken)
        val items = data.optJSONArray("items") ?: return emptyList()
        val result = ArrayList<AccountDeviceInfo>(items.length())
        for (index in 0 until items.length()) {
            val item = items.optJSONObject(index) ?: continue
            val deviceId = item.optString("device_id")
            if (deviceId.isBlank()) {
                continue
            }
            result.add(
                AccountDeviceInfo(
                    deviceId = deviceId,
                    displayName = item.textOrEmpty("display_name").ifBlank { deviceId },
                    platform = item.textOrEmpty("platform").ifBlank { "unknown" },
                    online = when (item.textOrEmpty("presence_state")) {
                        "online" -> true
                        "offline" -> false
                        else -> item.optBoolean("online", false)
                    },
                    presenceState = item.textOrEmpty("presence_state").ifBlank { "offline" },
                    lastSeenAt = item.textOrEmpty("last_seen_at"),
                    trusted = item.optBoolean("trusted", false),
                    current = item.optBoolean("current", false),
                    trustSource = item.textOrEmpty("trust_source"),
                    trustedAt = item.textOrEmpty("trusted_at"),
                    lastConnectedAt = item.textOrEmpty("last_connected_at"),
                ),
            )
        }
        return result
    }

    /** 判断错误码是否表示登录态失效, 供调用方清理本地令牌. */
    fun isSessionInvalid(code: String): Boolean = code in SESSION_INVALID_CODES

    /**
     * A4: 拉取消息与审计, 对应 `GET /v1/messages`.
     * device_id 可空; limit 默认 200; 未读与已读水位均以服务端返回为准.
     */
    fun messages(deviceToken: String, deviceId: String?, limit: Int = MESSAGE_LIMIT): AccountMessagePage {
        val query = StringBuilder("/v1/messages?limit=").append(limit)
        if (!deviceId.isNullOrBlank()) {
            query.append("&device_id=").append(deviceId)
        }
        val data = request("GET", query.toString(), token = deviceToken)
        val items = data.optJSONArray("items") ?: JSONArray()
        val parsed = ArrayList<AccountMessage>(items.length())
        for (index in 0 until items.length()) {
            val item = items.optJSONObject(index) ?: continue
            val detail = item.optJSONObject("detail")
            parsed.add(
                AccountMessage(
                    messageId = item.textOrEmpty("message_id"),
                    ts = item.textOrEmpty("ts"),
                    category = item.textOrEmpty("category"),
                    categoryLabel = item.textOrEmpty("category_label"),
                    title = item.textOrEmpty("title"),
                    event = item.textOrEmpty("event"),
                    deviceId = item.textOrEmpty("device_id"),
                    deviceName = item.textOrEmpty("device_name"),
                    actor = item.textOrEmpty("actor"),
                    result = item.textOrEmpty("result"),
                    reason = detail?.textOrEmpty("reason").orEmpty()
                        .ifBlank { detail?.textOrEmpty("end_reason").orEmpty() },
                ),
            )
        }
        return AccountMessagePage(
            items = parsed,
            unread = data.optInt("unread", 0),
            total = data.optInt("total", parsed.size),
            readUntil = data.optString("read_until"),
        )
    }

    /** A4: 标记全部已读, 对应 `POST /v1/messages/read`; 幂等键由请求层统一附加. */
    fun markMessagesRead(deviceToken: String): AccountReadResult {
        val data = request("POST", "/v1/messages/read", JSONObject(), token = deviceToken)
        return AccountReadResult(
            readUntil = data.optString("read_until"),
            cleared = data.optInt("cleared", 0),
        )
    }

    private fun request(
        method: String,
        path: String,
        body: JSONObject? = null,
        token: String? = null,
    ): JSONObject {
        val headers = linkedMapOf(
            "Content-Type" to "application/json; charset=utf-8",
            "Accept" to "application/json",
        )
        if (!token.isNullOrBlank()) {
            headers["Authorization"] = "Bearer $token"
        }
        val rawBody = if (method == "POST") {
            headers["Idempotency-Key"] = idempotencyKey()
            (body ?: JSONObject()).toString().toByteArray(Charsets.UTF_8)
        } else {
            ByteArray(0)
        }
        val response = try {
            transport.request(method, path, headers, rawBody)
        } catch (error: RemoteRejected) {
            throw error
        } catch (error: RemoteBadResponse) {
            throw error
        } catch (error: Exception) {
            throw RemoteUnavailable(UNREACHABLE_MESSAGE, error)
        }
        return parseEnvelope(response)
    }

    private fun parseEnvelope(response: AccountHttpResponse): JSONObject {
        val root = try {
            JSONObject(response.body)
        } catch (_: Exception) {
            throw RemoteBadResponse("账号中心返回内容无法解析")
        }
        val code = root.optString("code")
        if (response.status >= 400) {
            throw RemoteRejected(
                response.status,
                code.ifBlank { CODE_STORAGE_UNAVAILABLE },
                root.optString("message").ifBlank { "账号中心请求失败" },
            )
        }
        if (code.isNotBlank() && code != "0") {
            throw RemoteRejected(
                response.status,
                code,
                root.optString("message").ifBlank { "账号中心请求失败" },
            )
        }
        return root.optJSONObject("data") ?: JSONObject()
    }

    private fun idempotencyKey(): String {
        val raw = ByteArray(16)
        SecureRandom().nextBytes(raw)
        return raw.joinToString("") { "%02x".format(it) }
    }

    companion object {
        /** 与桌面端默认账号中心一致; 走中继同端口的 TLS 监听. */
        const val DEFAULT_ENDPOINT = "https://relay.wuge.fun:8760"

        const val CODE_ACCOUNT_EXISTS = "ACCOUNT_EXISTS"
        const val CODE_CREDENTIAL_INVALID = "CREDENTIAL_INVALID"
        const val CODE_TOKEN_REVOKED = "TOKEN_REVOKED"
        const val CODE_TOKEN_EXPIRED = "TOKEN_EXPIRED"
        const val CODE_TOKEN_INVALID = "TOKEN_INVALID"
        const val CODE_DEVICE_BOUND_OTHER = "DEVICE_BOUND_OTHER"
        const val CODE_CHALLENGE_INVALID = "CHALLENGE_INVALID"
        const val CODE_STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"

        const val UNREACHABLE_MESSAGE = "账号中心不可达, 请检查网络或稍后重试"
        const val SESSION_EXPIRED_MESSAGE = "登录状态已过期, 请重新登录"
        const val SESSION_REVOKED_MESSAGE = "登录状态已失效, 请重新登录"

        private val SESSION_INVALID_CODES = setOf(CODE_TOKEN_REVOKED, CODE_TOKEN_EXPIRED, CODE_TOKEN_INVALID)
        private const val DEVICE_TOKEN_TTL_MS = 30L * 24 * 3600 * 1000
        private const val SESSION_TOKEN_TTL_MS = 15L * 60 * 1000

        /** 账号中心 ISO 8601 带偏移时间转 epoch 毫秒; 解析失败返回 null, 由调用方用默认 TTL. */
        fun parseIsoMillis(text: String): Long? {
            if (text.isBlank()) {
                return null
            }
            return try {
                OffsetDateTime.parse(text).toInstant().toEpochMilli()
            } catch (_: DateTimeParseException) {
                null
            }
        }
    }
}
