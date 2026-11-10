// ==========================================================================================
// 文件        : app/build.gradle.kts
// 所属目录    : android/app/
// 文件用途    : Android 侧载 APK 的模块配置: 包名、SDK 版本、ABI、签名
// 架构定位    : Android 薄壳构建配置, 不承载业务逻辑
// 核心功能    : arm64-v8a 单一 ABI; minSdk 29 / targetSdk 34; 签名口令按环境变量或仓库外配置注入
// 职责边界    : 不写业务代码; 签名口令不写死, 缺失时 release 不出包
// 运行环境    : JDK 17 + Android SDK 34 + Gradle 8.9
// 依赖关系    : 签名口令来自环境变量或 D:/RDC-Build/secrets/keystore.properties(仓库外)
// ==========================================================================================

import java.io.File
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// 签名口令来源优先级:
// 1) 环境变量 RDC_KEYSTORE_FILE / RDC_KEYSTORE_PASSWORD(可选 RDC_KEY_ALIAS / RDC_KEY_PASSWORD), 不落盘;
// 2) 仓库外部的本地配置文件, 默认 D:/RDC-Build/secrets/keystore.properties, 可用 RDC_KEYSTORE_PROPERTIES 覆盖.
// 仓库目录内不再保存任何口令文件.
val keystorePropertiesFile = System.getenv("RDC_KEYSTORE_PROPERTIES")
    ?.takeIf { it.isNotBlank() }
    ?.let(::File)
    ?: File("D:/RDC-Build/secrets/keystore.properties")
val envStoreFile = System.getenv("RDC_KEYSTORE_FILE").orEmpty()
val envStorePassword = System.getenv("RDC_KEYSTORE_PASSWORD").orEmpty()
val hasEnvCredentials = envStoreFile.isNotBlank() && envStorePassword.isNotBlank()
val keystoreProperties = Properties().apply {
    if (hasEnvCredentials) {
        setProperty("storeFile", envStoreFile)
        setProperty("storePassword", envStorePassword)
        setProperty("keyAlias", System.getenv("RDC_KEY_ALIAS") ?: "rdc-release")
        setProperty("keyPassword", System.getenv("RDC_KEY_PASSWORD") ?: envStorePassword)
    } else if (keystorePropertiesFile.exists()) {
        keystorePropertiesFile.inputStream().use { load(it) }
    }
}
val hasSigningConfig = hasEnvCredentials || keystorePropertiesFile.exists()

android {
    namespace = "com.rdc.remote"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.rdc.remote"
        minSdk = 29
        targetSdk = 34
        versionCode = 10
        versionName = "1.0.0"
        // 账号中心地址: 默认生产; 联调用 -PrdcAccountEndpoint=https://<主机>:<端口> 覆盖构建产物
        val accountEndpoint = (project.findProperty("rdcAccountEndpoint") as String?)
            ?.takeIf { it.isNotBlank() }
            ?: "https://relay.wuge.fun:8760"
        buildConfigField("String", "ACCOUNT_ENDPOINT", "\"$accountEndpoint\"")
        // 会话 TLS 主机列表: 默认内置中继; 联调/其它部署可用 -PrdcTlsHosts=host1,host2 覆盖
        val sessionTlsHosts = (project.findProperty("rdcTlsHosts") as String?)
            ?.takeIf { it.isNotBlank() }
            ?: "47.109.32.249"
        buildConfigField("String", "SESSION_TLS_HOSTS", "\"$sessionTlsHosts\"")
        // 打洞回滚开关: 默认开启; 出包时用 -Pandroid.punch_enabled=false 关掉, 退回纯中继
        val punchEnabled = (project.findProperty("android.punch_enabled") as String?)
            ?.toBooleanStrictOrNull()
            ?: true
        buildConfigField("boolean", "PUNCH_ENABLED", "$punchEnabled")
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

    buildFeatures {
        buildConfig = true
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    testOptions {
        unitTests {
            // 协议层单测不拉起 Android 运行时, 框架方法返回默认值而不是抛 "Stub!"
            isReturnDefaultValues = true
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    // BouncyCastle: 仅用于 Ed25519/X25519 原语, 许可证为 Bouncy Castle License(类 MIT), 不收费
    // 1.78.1 修复 CVE-2024-29857 / 30171 / 30172; 不得回退到 1.77
    implementation("org.bouncycastle:bcprov-jdk18on:1.78.1")

    // 纯 JVM 单测: 注入桩验证协议与账号客户端, 不依赖真机或模拟器
    testImplementation("junit:junit:4.13.2")
    // Android 自带的 org.json 在单元测试里是空壳, 这里用官方实现顶上
    testImplementation("org.json:json:20240303")
}
