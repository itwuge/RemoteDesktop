// ==========================================================================================
// 文件        : CanonicalJsonTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : 登录声明签名所用规范化 JSON 的纯 JVM 单测
// 架构定位    : 协议层单测; 与桌面端 json.dumps(sort_keys=True, separators=(",", ":")) 对齐
// 核心功能    : 键排序、紧凑分隔符、中文不转义、控制字符转义、嵌套结构
// 职责边界    : 只测 CanonicalJson, 不测 Ed25519 签名本身
// 运行环境    : JDK 17, JUnit 4
// 依赖关系    : CanonicalJson
// ==========================================================================================

package com.rdc.remote

import org.junit.Assert.assertEquals
import org.junit.Test

class CanonicalJsonTest {

    @Test
    fun `keys are sorted and separators are compact`() {
        val encoded = CanonicalJson.encode(
            linkedMapOf("b" to 1, "a" to "中文", "c" to listOf(true, false)),
        )

        assertEquals("{\"a\":\"中文\",\"b\":1,\"c\":[true,false]}", encoded)
    }

    @Test
    fun `login attestation payload matches desktop order`() {
        val encoded = CanonicalJson.encode(
            linkedMapOf("account" to "wuge", "device_id" to "dev-1", "nonce" to "n-1"),
        )

        assertEquals("{\"account\":\"wuge\",\"device_id\":\"dev-1\",\"nonce\":\"n-1\"}", encoded)
    }

    @Test
    fun `quotes backslashes and newlines are escaped`() {
        val encoded = CanonicalJson.encode(mapOf("text" to "a\"b\\c\nd"))

        assertEquals("{\"text\":\"a\\\"b\\\\c\\nd\"}", encoded)
    }

    @Test
    fun `nested maps are also sorted`() {
        val encoded = CanonicalJson.encode(
            mapOf("outer" to linkedMapOf("z" to 1, "a" to 2)),
        )

        assertEquals("{\"outer\":{\"a\":2,\"z\":1}}", encoded)
    }

    @Test
    fun `null booleans and numbers keep json literals`() {
        val encoded = CanonicalJson.encode(
            linkedMapOf("n" to null, "t" to true, "f" to false, "i" to 7, "l" to 9007199254740993L),
        )

        assertEquals("{\"f\":false,\"i\":7,\"l\":9007199254740993,\"n\":null,\"t\":true}", encoded)
    }
}
