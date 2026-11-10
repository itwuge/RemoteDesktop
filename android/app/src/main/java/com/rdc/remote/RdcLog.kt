// ==========================================================================================
// 文件        : RdcLog.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 控制端结构化日志入口与调试开关
// 架构定位    : 界面层公共设施, 不承载业务逻辑
// 核心功能    : 统一日志格式 [事件] 键=值; 调试级日志受 DebugSwitch 控制, 默认关闭
// 职责边界    : 不采集设备信息、不落盘、不上传; 不记录连接码与令牌明文
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34)
// 依赖关系    : 被各 Activity 与 RemoteRenderer 调用
// ==========================================================================================

package com.rdc.remote

import android.util.Log

/**
 * 调试开关.
 *
 * 默认关闭; 仅用于打开调试级日志, 不影响业务行为.
 * 关闭时 [RdcLog.debug] 不产生任何输出, release 包默认静默.
 */
internal object DebugSwitch {
    const val DEFAULT_ENABLED = false

    var enabled: Boolean = DEFAULT_ENABLED
        private set

    /** 切换调试开关, 返回切换后的状态. */
    fun toggle(): Boolean {
        enabled = !enabled
        return enabled
    }
}

/** 结构化日志: 统一输出 `[事件] 键=值 键=值`, 便于 adb logcat 过滤与取证. */
internal object RdcLog {

    fun info(tag: String, event: String, detail: String = "") {
        Log.i(prefix(tag), format(event, detail))
    }

    fun warn(tag: String, event: String, detail: String = "") {
        Log.w(prefix(tag), format(event, detail))
    }

    fun error(tag: String, event: String, detail: String = "") {
        Log.e(prefix(tag), format(event, detail))
    }

    /** 调试级日志, 受 [DebugSwitch] 控制, 默认不输出. */
    fun debug(tag: String, event: String, detail: String = "") {
        if (!DebugSwitch.enabled) {
            return
        }
        Log.d(prefix(tag), format(event, detail))
    }

    private fun prefix(tag: String): String = "Rdc$tag"

    private fun format(event: String, detail: String): String =
        if (detail.isEmpty()) "[$event]" else "[$event] $detail"
}
