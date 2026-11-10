// ==========================================================================================
// 文件        : RdcAccountService.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 账号入口统一门面: 优先跨端账号中心, 未配置地址时退回本机账号
// 架构定位    : 界面唯一依赖的账号数据层; 界面不直接触碰 HTTP、令牌与身份
// 核心功能    : 登录/注册/登出/设备列表; 登录态持久化; 令牌吊销与过期后的本地清理
// 职责边界    : 不写界面文案以外的业务逻辑、不保存明文令牌、不改契约
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35)
// 依赖关系    : 组合 RemoteAccountClient、AndroidAccountTransport、AndroidSessionStore、RdcAccount
// ==========================================================================================

package com.rdc.remote

import android.content.Context
import android.os.Build

internal object RdcAccountService {

    /** 账号中心地址来自构建配置; 为空表示只用本机账号. */
    private val endpoint: String = BuildConfig.ACCOUNT_ENDPOINT

    /** 设备列表结果; ok 为 false 时 items 为空. */
    data class DeviceListOutcome(
        val ok: Boolean,
        val message: String,
        val errorCode: String? = null,
        val items: List<AccountDeviceInfo> = emptyList(),
    )

    /** A4: 消息与审计结果; 未读与已读水位均以服务端为准. */
    data class MessagesOutcome(
        val ok: Boolean,
        val message: String,
        val errorCode: String? = null,
        val items: List<AccountMessage> = emptyList(),
        val unread: Int = 0,
        val total: Int = 0,
        val readUntil: String = "",
    )

    /** 当前登录态; 账号中心优先, 本机账号兜底. */
    fun session(context: Context): RdcAccount.Session? {
        val store = AndroidSessionStore(context)
        val remote = store.load()
        if (remote != null) {
            if (remote.isValid()) {
                return RdcAccount.Session(
                    accountId = remote.accountId,
                    name = remote.accountName,
                    loginAt = remote.loginAt,
                    expiresAt = remote.deviceExpiresAt,
                    remote = true,
                    deviceId = remote.deviceId,
                    trusted = remote.trusted,
                )
            }
            store.clear()
            RdcLog.warn(TAG, "remote_session_expired", "account=${remote.accountId}")
        }
        return RdcAccount.session(context)
    }

    fun register(context: Context, name: String, password: String): RdcAccount.Outcome {
        RdcAccount.validate(name, password)?.let { return RdcAccount.Outcome(false, it) }
        if (endpoint.isBlank()) {
            return RdcAccount.register(context, name, password)
        }
        return try {
            val result = client(context).register(name, password)
            RdcLog.info(TAG, "register_ok", "account_id=${result.accountId}")
            RdcAccount.Outcome(
                true,
                "账号已创建",
                data = mapOf(
                    "account_id" to result.accountId,
                    "account_name" to result.accountName,
                    "created_at" to result.createdAt,
                    "remote" to true,
                ),
            )
        } catch (error: RemoteRejected) {
            RdcAccount.Outcome(false, error.message ?: "注册失败", error.code)
        } catch (error: Exception) {
            failure("register", error)
        }
    }

    fun login(context: Context, name: String, password: String): RdcAccount.Outcome {
        RdcAccount.validate(name, password)?.let { return RdcAccount.Outcome(false, it) }
        if (endpoint.isBlank()) {
            return RdcAccount.login(context, name, password)
        }
        val store = AndroidSessionStore(context)
        return try {
            val result = client(context).login(name, password, deviceName())
            store.save(
                RemoteSession(
                    accountId = result.accountId,
                    accountName = result.accountName,
                    deviceId = result.deviceId,
                    deviceToken = result.deviceToken,
                    sessionToken = result.sessionToken,
                    deviceExpiresAt = result.deviceExpiresAt,
                    sessionExpiresAt = result.sessionExpiresAt,
                    loginAt = System.currentTimeMillis(),
                    trusted = result.trusted,
                ),
            )
            RdcAccount.clear(context)
            RdcLog.info(TAG, "login_ok", "account_id=${result.accountId} trusted=${result.trusted}")
            RdcAccount.Outcome(
                true,
                "登录成功",
                data = mapOf(
                    "account_id" to result.accountId,
                    "account_name" to result.accountName,
                    "device_id" to result.deviceId,
                    "trusted" to result.trusted,
                    "remote" to true,
                ),
            )
        } catch (error: RemoteRejected) {
            RdcAccount.Outcome(false, error.message ?: "登录失败", error.code)
        } catch (error: Exception) {
            failure("login", error)
        }
    }

    fun logout(context: Context): RdcAccount.Outcome {
        val store = AndroidSessionStore(context)
        val session = store.load()
        if (session == null) {
            store.clear()
            return RdcAccount.logout(context)
        }
        if (!session.isValid()) {
            store.clear()
            RdcAccount.clear(context)
            return RdcAccount.Outcome(true, "登录状态已过期, 已清理本地登录态", "TOKEN_EXPIRED")
        }
        return try {
            val result = client(context).logout(session.deviceToken)
            store.clear()
            RdcAccount.clear(context)
            RdcLog.info(TAG, "logout_ok", "account_id=${session.accountId}")
            RdcAccount.Outcome(
                true,
                "已登出, 吊销 ${result.revokedTokens} 个令牌, 断开 ${result.kickedConnections} 个会话",
                data = mapOf(
                    "account_id" to session.accountId,
                    "revoked_tokens" to result.revokedTokens,
                    "kicked_sessions" to result.kickedConnections,
                ),
            )
        } catch (error: RemoteRejected) {
            if (isSessionInvalid(error.code)) {
                store.clear()
                RdcAccount.clear(context)
                RdcAccount.Outcome(true, "登录状态已失效, 已清理本地登录态", error.code)
            } else {
                RdcAccount.Outcome(false, error.message ?: "登出失败", error.code)
            }
        } catch (error: Exception) {
            failure("logout", error)
        }
    }

    /** 拉取账号下设备; 登录态被吊销时清空本地令牌并返回中文原因. */
    fun devices(context: Context): DeviceListOutcome {
        val store = AndroidSessionStore(context)
        val session = store.load()
            ?: return DeviceListOutcome(false, "需要先登录账号")
        if (!session.isValid()) {
            store.clear()
            RdcAccount.clear(context)
            return DeviceListOutcome(false, RemoteAccountClient.SESSION_EXPIRED_MESSAGE, "TOKEN_EXPIRED")
        }
        return try {
            val items = client(context).devices(session.deviceToken)
            DeviceListOutcome(true, "共 ${items.size} 台设备", items = items)
        } catch (error: RemoteRejected) {
            if (isSessionInvalid(error.code)) {
                store.clear()
                RdcAccount.clear(context)
                DeviceListOutcome(false, RemoteAccountClient.SESSION_REVOKED_MESSAGE, error.code)
            } else {
                DeviceListOutcome(false, error.message ?: "设备列表获取失败", error.code)
            }
        } catch (error: Exception) {
            RdcLog.warn(TAG, "devices_failed", error.javaClass.simpleName)
            DeviceListOutcome(false, RemoteAccountClient.UNREACHABLE_MESSAGE, "SIGNALING_UNREACHABLE")
        }
    }

    /** A4: 拉取消息与审计; device_id 传空表示账号级. */
    fun messages(context: Context, deviceId: String? = null): MessagesOutcome {
        val store = AndroidSessionStore(context)
        val session = store.load()
            ?: return MessagesOutcome(false, "需要先登录账号")
        if (!session.isValid()) {
            store.clear()
            RdcAccount.clear(context)
            return MessagesOutcome(false, RemoteAccountClient.SESSION_EXPIRED_MESSAGE, "TOKEN_EXPIRED")
        }
        return try {
            val page = client(context).messages(session.deviceToken, deviceId)
            MessagesOutcome(
                ok = true,
                message = "共 ${page.total} 条消息",
                items = page.items,
                unread = page.unread,
                total = page.total,
                readUntil = page.readUntil,
            )
        } catch (error: RemoteRejected) {
            if (isSessionInvalid(error.code)) {
                store.clear()
                RdcAccount.clear(context)
                MessagesOutcome(false, RemoteAccountClient.SESSION_REVOKED_MESSAGE, error.code)
            } else {
                MessagesOutcome(false, error.message ?: "消息获取失败", error.code)
            }
        } catch (error: Exception) {
            RdcLog.warn(TAG, "messages_failed", error.javaClass.simpleName)
            MessagesOutcome(false, RemoteAccountClient.UNREACHABLE_MESSAGE, "SIGNALING_UNREACHABLE")
        }
    }

    /** A4: 全部已读; 水位由服务端写回, 本地不保存未读计数. */
    fun markAllRead(context: Context): RdcAccount.Outcome {
        val store = AndroidSessionStore(context)
        val session = store.load()
            ?: return RdcAccount.Outcome(false, "需要先登录账号")
        if (!session.isValid()) {
            store.clear()
            RdcAccount.clear(context)
            return RdcAccount.Outcome(false, RemoteAccountClient.SESSION_EXPIRED_MESSAGE, "TOKEN_EXPIRED")
        }
        return try {
            val result = client(context).markMessagesRead(session.deviceToken)
            RdcAccount.Outcome(
                true,
                if (result.cleared > 0) "已全部标记为已读" else "当前没有未读消息",
                data = mapOf("read_until" to result.readUntil, "cleared" to result.cleared),
            )
        } catch (error: RemoteRejected) {
            if (isSessionInvalid(error.code)) {
                store.clear()
                RdcAccount.clear(context)
                RdcAccount.Outcome(false, RemoteAccountClient.SESSION_REVOKED_MESSAGE, error.code)
            } else {
                RdcAccount.Outcome(false, error.message ?: "标记已读失败", error.code)
            }
        } catch (error: Exception) {
            failure("mark_read", error)
        }
    }

    private fun client(context: Context): RemoteAccountClient = RemoteAccountClient(
        endpoint = endpoint,
        identity = RdcAccountIdentity(RdcIdentity.load(context)),
        transport = AndroidAccountTransport(context, endpoint),
        devicePlatform = "android",
    )

    private fun isSessionInvalid(code: String): Boolean =
        code in setOf(
            RemoteAccountClient.CODE_TOKEN_REVOKED,
            RemoteAccountClient.CODE_TOKEN_EXPIRED,
            RemoteAccountClient.CODE_TOKEN_INVALID,
            RemoteAccountClient.CODE_CREDENTIAL_INVALID,
        )

    private fun failure(event: String, error: Exception): RdcAccount.Outcome {
        val message = when (error) {
            is RemoteUnavailable -> error.message ?: RemoteAccountClient.UNREACHABLE_MESSAGE
            is RemoteBadResponse -> error.message ?: "账号中心返回内容无法解析"
            else -> {
                RdcLog.error(TAG, "${event}_failed", error.javaClass.simpleName)
                RdcLog.warn(
                    TAG,
                    "${event}_stack",
                    error.stackTraceToString().lineSequence().take(4).joinToString(" | "),
                )
                "账号中心请求失败：${error.message ?: error.javaClass.simpleName}"
            }
        }
        RdcLog.warn(TAG, "${event}_failed", error.javaClass.simpleName)
        return RdcAccount.Outcome(false, message, "SIGNALING_UNREACHABLE")
    }

    private fun deviceName(): String {
        val manufacturer = Build.MANUFACTURER.orEmpty().trim()
        val model = Build.MODEL.orEmpty().trim()
        val name = listOf(manufacturer, model).filter { it.isNotEmpty() }.joinToString(" ")
        return name.ifBlank { "Android" }
    }

    private const val TAG = "AccountService"
}
