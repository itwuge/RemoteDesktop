// ==========================================================================================
// 文件        : RdcApp.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 应用入口, 启动时把资源绑定给设计令牌
// 架构定位    : 进程级初始化, 不含业务逻辑
// 核心功能    : RdcColor.bind, 使颜色/字号/尺寸令牌可从 colors.xml 与 dimens.xml 取值
// 职责边界    : 不申请权限、不发起网络与采集
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35)
// 依赖关系    : RdcTheme
// ==========================================================================================

package com.rdc.remote

import android.app.Application

class RdcApp : Application() {
    override fun onCreate() {
        super.onCreate()
        RdcColor.bind(this)
    }
}
