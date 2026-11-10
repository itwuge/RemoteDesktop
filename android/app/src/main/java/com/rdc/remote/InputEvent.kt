// ==========================================================================================
// 文件        : InputEvent.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 控制端输入事件模型: 触摸转指针、键盘、手势
// 架构定位    : 界面层与契约层之间的数据模型, 不含业务判断
// 核心功能    : 统一坐标归一化 (0..1, 相对远端画面内容区) 与事件描述
// 职责边界    : 不做坐标到远端像素的换算; 不直连网络; 不注入到本机其他应用
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34)
// 依赖关系    : 由 SessionActivity 产生, 经 ControlContract.injectInput 交契约入口
// ==========================================================================================

package com.rdc.remote

/**
 * 控制端上行输入事件.
 *
 * 坐标一律归一化到远端画面内容区 (已扣黑边), 取值 [0.0, 1.0],
 * 具体像素换算与方向映射由接收侧按 display_update 处理, 界面层不承担.
 */
internal sealed class InputEvent {

    /** 事件类型名, 用于日志与契约字段. */
    abstract val kind: String

    /** 供日志输出的可读描述. */
    fun describe(): String = when (this) {
        is Pointer -> "kind=$kind action=$action x=${fmt(x)} y=${fmt(y)} button=$button"
        is Key -> "kind=$kind action=$action key=$keyCode"
        is Gesture -> "kind=$kind gesture=$gesture x=${fmt(x)} y=${fmt(y)} dx=${fmt(dx)} dy=${fmt(dy)}"
    }

    /** 指针事件: 触摸按下/移动/抬起, 映射为远端鼠标事件. */
    data class Pointer(
        val action: Action,
        val x: Float,
        val y: Float,
        val button: Int = BUTTON_LEFT,
    ) : InputEvent() {
        override val kind: String = "pointer"

        enum class Action { MOVE, DOWN, UP }

        companion object {
            const val BUTTON_LEFT = 0
        }
    }

    /** 键盘事件: 软键盘或物理键盘转发. */
    data class Key(
        val action: Action,
        val keyCode: String,
    ) : InputEvent() {
        override val kind: String = "key"

        enum class Action { DOWN, UP }
    }

    /** 手势事件: 手势条上的系统手势, 交给远端执行. */
    data class Gesture(
        val gesture: String,
        val x: Float,
        val y: Float,
        val dx: Float,
        val dy: Float,
    ) : InputEvent() {
        override val kind: String = "gesture"
    }

    companion object {
        /** 日志保留三位小数, 避免刷屏. */
        fun fmt(value: Float): String = String.format(java.util.Locale.US, "%.3f", value)
    }
}
