// ==========================================================================================
// 文件        : AppPrefs.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 控制端本地设置读写: 默认画质档、自动降档、剪贴板同步、系统通知已读
// 架构定位    : 界面层本地偏好, 不属于命令契约
// 核心功能    : 读写 SharedPreferences, 提供默认值
// 职责边界    : 不保存设备 ID、连接码、令牌与剪贴板内容; 不做网络同步
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34)
// 依赖关系    : 被 MainActivity、SessionActivity 调用
// ==========================================================================================

package com.rdc.remote

import android.content.Context

internal object AppPrefs {

    private const val FILE_NAME = "rdc_control_prefs"
    private const val KEY_PROFILE = "default_profile"
    private const val KEY_AUTO_DOWNGRADE = "auto_downgrade"
    private const val KEY_CLIPBOARD = "clipboard_sync"
    private const val KEY_NOTICE_READ = "system_notice_read"

    const val DEFAULT_PROFILE = "auto"
    const val DEFAULT_AUTO_DOWNGRADE = true
    const val DEFAULT_CLIPBOARD = true

    private fun prefs(context: Context) =
        context.getSharedPreferences(FILE_NAME, Context.MODE_PRIVATE)

    /** 默认画质档; 只接受 desktop-v1 直连档位. */
    fun profile(context: Context): String {
        val value = prefs(context).getString(KEY_PROFILE, DEFAULT_PROFILE) ?: DEFAULT_PROFILE
        return if (value in ControlContract.DIRECT_PROFILES) value else DEFAULT_PROFILE
    }

    fun setProfile(context: Context, value: String) {
        prefs(context).edit().putString(KEY_PROFILE, value).apply()
    }

    /** 中继降级时是否自动降档. */
    fun autoDowngrade(context: Context): Boolean =
        prefs(context).getBoolean(KEY_AUTO_DOWNGRADE, DEFAULT_AUTO_DOWNGRADE)

    fun setAutoDowngrade(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean(KEY_AUTO_DOWNGRADE, enabled).apply()
    }

    /** 会话内剪贴板同步开关. */
    fun clipboardSync(context: Context): Boolean =
        prefs(context).getBoolean(KEY_CLIPBOARD, DEFAULT_CLIPBOARD)

    fun setClipboardSync(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean(KEY_CLIPBOARD, enabled).apply()
    }

    /** 内置系统通知是否已读. */
    fun noticeRead(context: Context): Boolean =
        prefs(context).getBoolean(KEY_NOTICE_READ, false)

    fun setNoticeRead(context: Context, read: Boolean) {
        prefs(context).edit().putBoolean(KEY_NOTICE_READ, read).apply()
    }
}
