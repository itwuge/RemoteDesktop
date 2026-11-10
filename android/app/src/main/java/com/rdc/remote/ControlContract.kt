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

/**
 * 命令契约接缝.
 *
 * 现状: 命令层 (Chaquopy 核心) 未接入, [WIRED] 恒为 false;
 * 本对象只做入参口径校验与调用留痕, 便于在模拟器上用 adb logcat 验证
 * 「界面 -> 契约入口」这一段是通的, 不据此宣称任何链路可用.
 */
internal object ControlContract {

    /** 命令层是否已接上. 当前恒为 false. */
    const val WIRED = false

    /** 直连可选画质档, 以 desktop-v1 第 7 条为准. */
    val DIRECT_PROFILES = listOf("auto", "4K60", "1440p60", "1080p60", "1080p30")

    /** 本机设备标识; 设备身份未接入前给占位. */
    fun localDeviceId(): String = "待设备身份接入"

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
            return CommandResult(false, "请输入对方设备 ID", "DEVICE_NOT_FOUND")
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
        if (!WIRED) {
            return CommandResult(false, "命令层未接入: 无指标")
        }
        return CommandResult(true, "查询完成")
    }

    /**
     * 取会话状态, 对应 desktop-v1 `get_session_status(session_id)`.
     */
    fun getSessionStatus(sessionId: String): CommandResult {
        RdcLog.debug("Contract", "get_session_status", "session_id=$sessionId")
        if (!WIRED) {
            return CommandResult(false, "命令层未接入: 无会话")
        }
        return CommandResult(true, "查询完成")
    }

    /**
     * 停止会话, 对应 desktop-v1 `stop_session(session_id)`.
     */
    fun stopSession(sessionId: String): CommandResult {
        RdcLog.info("Contract", "stop_session", "session_id=$sessionId")
        if (!WIRED) {
            return CommandResult(false, "命令层未接入: 本地已关闭会话窗")
        }
        return CommandResult(true, "会话已停止")
    }

    /**
     * 输入事件入口, 对应 desktop-v1 模块端口 13 `InputInjector.inject(event)`.
     * 控制端负责产生事件并交给通路上行; 这里只做入口留痕与基础校验.
     */
    fun injectInput(event: InputEvent): CommandResult {
        RdcLog.info("Contract", "inject", event.describe())
        if (!WIRED) {
            return CommandResult(false, "命令层未接入: 事件已到契约入口, 未上行")
        }
        return CommandResult(true, "已注入")
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
