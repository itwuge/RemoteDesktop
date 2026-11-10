// ==========================================================================================
// 文件        : ControlContract.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : Android 控制端与桌面命令契约 desktop-v1 之间的唯一接缝
// 架构定位    : Android 界面适配层与命令层边界; 界面不直接写业务逻辑
// 核心功能    : 以 desktop-v1 的命令函数名与统一返回结构承接调用, 结构化留痕以便 adb 取证
// 职责边界    : 不解码、不编码、不采集、不建连; 不实现降级策略与坐标映射算法
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34), arm64-v8a
// 依赖关系    : 被 MainActivity、SessionActivity 调用; 后续由 Chaquopy Python 核心实现
// ==========================================================================================

package com.rdc.remote

import android.content.Context

/**
 * 统一返回结构, 与 desktop-v1 第二节一致.
 *
 * @param ok 是否成功
 * @param message 用户可见文案, 界面层原样呈现
 * @param errorCode 调用方需要分支判断时给码
 * @param data 成功时的结果数据
 */
internal data class CommandResult(
    val ok: Boolean,
    val message: String,
    val errorCode: String? = null,
    val data: Map<String, Any?> = emptyMap(),
)

/** 连接页地址解析结果: 主机、端口与 TLS 决策. */
internal data class LanEndpoint(
    val host: String,
    val port: Int,
    val useTls: Boolean,
    val tlsServerName: String?,
)

/**
 * 连接地址解析.
 *
 * 支持 IPv4/主机名、带端口的 IPv4/主机名、裸 IPv6、`[IPv6]`、`[IPv6]:端口`;
 * 未写端口时用 8750; `tls://` 前缀表示本次连接强制 TLS. 不再按冒号切两段.
 */
internal fun parseLanEndpoint(raw: String): LanEndpoint? {
    var text = raw.trim()
    if (text.isEmpty()) {
        return null
    }
    var forceTls = false
    if (text.startsWith("tls://", ignoreCase = true)) {
        forceTls = true
        text = text.substring("tls://".length).trim()
        if (text.isEmpty()) {
            return null
        }
    }
    val host: String
    val port: Int
    if (text.startsWith("[")) {
        val close = text.indexOf(']')
        if (close <= 1) {
            return null
        }
        host = text.substring(1, close).trim()
        val rest = text.substring(close + 1).trim()
        port = when {
            rest.isEmpty() -> LanSession.DEFAULT_PORT
            rest.startsWith(":") -> rest.substring(1).trim().toIntOrNull() ?: return null
            else -> return null
        }
    } else {
        when (text.count { it == ':' }) {
            0 -> {
                host = text
                port = LanSession.DEFAULT_PORT
            }
            1 -> {
                val index = text.lastIndexOf(':')
                host = text.substring(0, index).trim()
                val portText = text.substring(index + 1).trim()
                port = if (portText.isEmpty()) {
                    LanSession.DEFAULT_PORT
                } else {
                    portText.toIntOrNull() ?: return null
                }
            }
            else -> {
                // 未加方括号的 IPv6 字面量, 整体作为主机, 端口取默认值
                host = text
                port = LanSession.DEFAULT_PORT
            }
        }
    }
    if (host.isEmpty() || port !in 1..65535) {
        return null
    }
    val useTls = SessionTlsPolicy.enabled(host, forceTls)
    return LanEndpoint(
        host = host,
        port = port,
        useTls = useTls,
        tlsServerName = if (useTls) SessionTlsPolicy.serverName(host) else null,
    )
}

/**
 * 会话传输的 TLS 策略.
 *
 * 默认只对内置中继地址启用 TLS, 可用构建属性 `-PrdcTlsHosts=host1,host2` 覆盖;
 * 连接地址写 `tls://` 前缀时对本次连接强制启用. 证书校验仍只用内置 CA.
 */
internal object SessionTlsPolicy {

    private val configuredHosts: List<String> by lazy {
        BuildConfig.SESSION_TLS_HOSTS
            .split(',')
            .map { it.trim() }
            .filter { it.isNotEmpty() }
    }

    fun enabled(host: String, forced: Boolean): Boolean =
        forced || configuredHosts.any { it.equals(host, ignoreCase = true) }

    /** 证书域名: 主机名原样返回; IP 字面量返回 null, 由 RelayTls 使用默认 SNI. */
    fun serverName(host: String): String? = if (isIpLiteral(host)) null else host

    private fun isIpLiteral(host: String): Boolean =
        host.contains(':') || IPV4.matches(host)

    private val IPV4 = Regex("""^\d{1,3}(\.\d{1,3}){3}$""")
}

/**
 * 命令契约接缝.
 *
 * 现状: 命令层 (Chaquopy 核心) 未接入, [WIRED] 恒为 false;
 * 本对象只做入参口径校验与调用留痕, 便于在模拟器上用 adb logcat 验证
 * 「界面 -> 契约入口」这一段是通的, 不据此宣称任何链路可用.
 */
internal object ControlContract {

    /** 命令层是否已接上: 以局域网真实会话是否在线为准. */
    val WIRED: Boolean
        get() = LanSession.active

    /** 直连可选画质档, 以 desktop-v1 第 7 条为准. */
    val DIRECT_PROFILES = listOf("auto", "4K60", "1440p60", "1080p60", "1080p30")

    /** 本机设备标识; 由本机 Ed25519 公钥摘要生成, 与 Python 参考实现一致. */
    fun localDeviceId(context: Context): String = try {
        RdcIdentity.load(context.applicationContext).deviceId
    } catch (error: Exception) {
        "设备身份不可用"
    }

    /**
     * 发起连接, 对应 desktop-v1 `start_connection(device_id, access_code)`.
     *
     * @param deviceId 对方设备标识
     * @param accessCode 一次性连接码
     */
    fun startConnection(deviceId: String, accessCode: String): CommandResult {
        RdcLog.info(
            "Contract",
            "start_connection",
            "device_id=$deviceId access_code=${maskCode(accessCode)}",
        )
        if (deviceId.isBlank()) {
            return CommandResult(false, "请输入对方设备编号", "DEVICE_NOT_FOUND")
        }
        if (accessCode.length != ACCESS_CODE_LENGTH || accessCode.any { !it.isDigit() }) {
            return CommandResult(false, "连接码应为 $ACCESS_CODE_LENGTH 位数字", "ACCESS_CODE_INVALID")
        }
        if (!WIRED) {
            return CommandResult(false, "命令层未接入: 参数已通过校验, 未发起真实连接")
        }
        return CommandResult(true, "连接任务已创建", data = mapOf("task_id" to "", "total" to 0))
    }

    /**
     * 局域网直连入口: 校验通过后交给真实链路, 不改变契约字段.
     * 结果通过回调回到主线程, 失败时 message 为可直接展示的中文原因.
     */
    fun startLanConnection(
        context: Context,
        address: String,
        deviceId: String,
        accessCode: String,
        callback: (LanSession.ConnectResult) -> Unit,
    ) {
        RdcLog.info(
            "Contract",
            "start_lan_connection",
            "address=$address device_id=$deviceId access_code=${maskCode(accessCode)}",
        )
        val endpoint = parseLanEndpoint(address)
        if (endpoint == null) {
            callback(
                LanSession.ConnectResult.Fail(
                    "地址格式应为 电脑IP:端口；IPv6 可写 [地址]:端口 或直接写地址，例如 192.168.31.80:18750",
                ),
            )
            return
        }
        if (deviceId.isBlank()) {
            callback(LanSession.ConnectResult.Fail("请输入电脑设备码"))
            return
        }
        if (accessCode.length != ACCESS_CODE_LENGTH || accessCode.any { !it.isDigit() }) {
            callback(LanSession.ConnectResult.Fail("连接码应为 $ACCESS_CODE_LENGTH 位数字"))
            return
        }
        LanSession.connect(
            context,
            endpoint.host,
            endpoint.port,
            deviceId,
            accessCode,
            endpoint.useTls,
            endpoint.tlsServerName,
            callback,
        )
    }

    /**
     * 查询连接进度, 对应 desktop-v1 `get_connection_progress(task_id)`.
     */
    fun getConnectionProgress(taskId: String): CommandResult {
        RdcLog.info("Contract", "get_connection_progress", "task_id=$taskId")
        if (!WIRED) {
            return CommandResult(false, "命令层未接入: 无连接任务", "TASK_NOT_FOUND")
        }
        return CommandResult(true, "查询完成")
    }

    /**
     * 最近连接列表, 对应 desktop-v1 `list_sessions()`.
     * 未接入时返回失败与空数据, 界面只显示空态, 不虚构记录.
     */
    fun listSessions(): CommandResult {
        RdcLog.info("Contract", "list_sessions")
        if (!WIRED) {
            return CommandResult(false, "命令层未接入: 无最近连接")
        }
        return CommandResult(true, "查询完成", data = mapOf("count" to 0))
    }

    /**
     * 切换画质档, 对应 desktop-v1 `apply_quality_profile(session_id, profile)`.
     */
    fun applyQualityProfile(sessionId: String, profile: String): CommandResult {
        RdcLog.info("Contract", "apply_quality_profile", "session_id=$sessionId profile=$profile")
        if (profile !in DIRECT_PROFILES) {
            return CommandResult(false, "直连通路不支持该档位, 已恢复 auto", "PROFILE_UNSUPPORTED")
        }
        if (!WIRED) {
            return CommandResult(false, "命令层未接入: 档位已记录, 未下发对端")
        }
        return CommandResult(true, "已切换到 $profile", data = mapOf("quality_profile" to profile))
    }

    /**
     * 取会话指标快照, 对应 desktop-v1 `get_session_metrics(session_id)`.
     * 未接入时返回空数据, 界面显示占位符, 不得编造数值.
     */
    fun getSessionMetrics(sessionId: String): CommandResult {
        RdcLog.debug("Contract", "get_session_metrics", "session_id=$sessionId")
        if (!LanSession.active) {
            return CommandResult(false, "命令层未接入: 无指标")
        }
        // 延迟为 ping/pong 实测; 样本不足 minSamples 时不编造分位, 只报实际样本数
        val latency = LanSession.latencySnapshot()
        val data = linkedMapOf<String, Any?>(
            "fps" to LanSession.fps(),
            "bitrate_kbps" to LanSession.bitrateKbps(),
            "latency_samples" to latency.samples,
        )
        latency.p50Ms?.let { data["p50_ms"] = round1(it) }
        latency.p95Ms?.let { data["p95_ms"] = round1(it) }
        val profile = LanSession.streamProfile
        if (profile.isNotEmpty()) {
            data["quality_profile"] = profile
        }
        return CommandResult(
            true,
            "查询完成",
            data = data,
        )
    }

    /** 与桌面一致: 延迟保留 1 位小数. */
    private fun round1(value: Double): Double = kotlin.math.round(value * 10.0) / 10.0

    /**
     * 取会话状态, 对应 desktop-v1 `get_session_status(session_id)`.
     */
    fun getSessionStatus(sessionId: String): CommandResult {
        RdcLog.debug("Contract", "get_session_status", "session_id=$sessionId")
        if (!LanSession.active) {
            return CommandResult(false, "命令层未接入: 无会话")
        }
        return CommandResult(true, "已连接", data = mapOf("transport_kind" to "局域网直连"))
    }

    /**
     * 停止会话, 对应 desktop-v1 `stop_session(session_id)`.
     */
    fun stopSession(sessionId: String): CommandResult {
        RdcLog.info("Contract", "stop_session", "session_id=$sessionId")
        if (!LanSession.active) {
            return CommandResult(false, "命令层未接入: 本地已关闭会话窗")
        }
        LanSession.stop()
        return CommandResult(true, "会话已停止")
    }

    /**
     * 输入事件入口, 对应 desktop-v1 模块端口 13 `InputInjector.inject(event)`.
     * 控制端负责产生事件并交给通路上行; 这里只做入口留痕与基础校验.
     */
    fun injectInput(event: InputEvent): CommandResult {
        if (event is InputEvent.Pointer && event.action == InputEvent.Pointer.Action.MOVE) {
            // 移动事件频率高, 只记 debug, 避免刷屏影响取证
            RdcLog.debug("Contract", "inject", event.describe())
        } else {
            RdcLog.info("Contract", "inject", event.describe())
        }
        if (!LanSession.active) {
            return CommandResult(false, "命令层未接入: 事件已到契约入口, 未上行")
        }
        return if (LanSession.send(event)) {
            CommandResult(true, "已注入")
        } else {
            CommandResult(false, "输入上行失败: 会话已断开", "INPUT_FAILED")
        }
    }

    /** 连接码只留后两位, 避免明文进日志. */
    private fun maskCode(code: String): String {
        if (code.isEmpty()) {
            return "<empty>"
        }
        val tail = if (code.length >= 2) code.takeLast(2) else ""
        return "***$tail"
    }

    private const val ACCESS_CODE_LENGTH = 6
}
