// ==========================================================================================
// 文件        : settings.gradle.kts
// 所属目录    : android/
// 文件用途    : Android 侧载 APK 骨架的工程入口, 声明仓库与模块
// 架构定位    : Android 薄壳构建配置, 不承载业务逻辑
// 职责边界    : 只声明工程结构与插件仓库; 不含任何版本号以外的业务配置
// 运行环境    : JDK 17 + Android SDK 34 + Gradle 8.9
// 依赖关系    : 被 app/build.gradle.kts 引用
// ==========================================================================================

pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "rdc-android"
include(":app")
