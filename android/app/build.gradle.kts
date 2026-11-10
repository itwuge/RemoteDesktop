// ==========================================================================================
// 文件        : app/build.gradle.kts
// 所属目录    : android/app/
// 文件用途    : Android 侧载 APK 的模块配置: 包名、SDK 版本、ABI、签名
// 架构定位    : Android 薄壳构建配置, 不承载业务逻辑
// 核心功能    : arm64-v8a 单一 ABI; minSdk 29 / targetSdk 34; 从 keystore.properties 读签名
// 职责边界    : 不写业务代码; 签名口令不写死, 缺失时 release 不出包
// 运行环境    : JDK 17 + Android SDK 34 + Gradle 8.9
// 依赖关系    : 读取 android/keystore.properties, 由 scripts/gen_keystore.ps1 生成
// ==========================================================================================

import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// 签名配置来源: 由 scripts/gen_keystore.ps1 写入 android/keystore.properties; 该文件不入库
val keystorePropertiesFile = rootProject.file("keystore.properties")
val keystoreProperties = Properties().apply {
    if (keystorePropertiesFile.exists()) {
        keystorePropertiesFile.inputStream().use { load(it) }
    }
}
val hasSigningConfig = keystorePropertiesFile.exists()

android {
    namespace = "com.rdc.remote"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.rdc.remote"
        minSdk = 29
        targetSdk = 34
        versionCode = 2
        versionName = "0.2.0"
        ndk {
            // 只发 arm64-v8a; 不追求全 ABI
            abiFilters += listOf("arm64-v8a")
        }
    }

    signingConfigs {
        if (hasSigningConfig) {
            create("release") {
                storeFile = file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (hasSigningConfig) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
}
