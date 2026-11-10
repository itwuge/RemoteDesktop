// ==========================================================================================
// 文件        : build.gradle.kts
// 所属目录    : android/
// 文件用途    : 根构建脚本, 只声明插件版本, 不应用到根工程
// 架构定位    : Android 薄壳构建配置
// 职责边界    : 只锁插件版本; 模块配置在 app/build.gradle.kts
// 运行环境    : Gradle 8.9, AGP 8.7.3, Kotlin 2.0.21
// 依赖关系    : 无
// ==========================================================================================

plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
}
