// ==========================================================================================
// 文件        : EndpointParseTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : 连接页地址解析与 TLS 策略的纯 JVM 单测
// 架构定位    : 契约层单测; 不建连、不依赖 Android 运行时
// 核心功能    : IPv4/主机名、IPv6 字面量（带/不带方括号与端口）、tls:// 前缀、TLS 主机策略
// 职责边界    : 只测 parseLanEndpoint 与 SessionTlsPolicy, 不测 Socket 与证书
// 运行环境    : JDK 17, JUnit 4
// 依赖关系    : ControlContract
// ==========================================================================================

package com.rdc.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class EndpointParseTest {

    @Test
    fun `ipv4 with port keeps host and port`() {
        val endpoint = parseLanEndpoint("192.168.31.80:18750")
        assertEquals("192.168.31.80", endpoint?.host)
        assertEquals(18750, endpoint?.port ?: -1)
    }

    @Test
    fun `hostname without port falls back to default port`() {
        val endpoint = parseLanEndpoint("relay.example.test")
        assertEquals("relay.example.test", endpoint?.host)
        assertEquals(LanSession.DEFAULT_PORT, endpoint?.port ?: -1)
    }

    @Test
    fun `bracketed ipv6 with port`() {
        val endpoint = parseLanEndpoint("[2408:8207:1234:5678::1]:8750")
        assertEquals("2408:8207:1234:5678::1", endpoint?.host)
        assertEquals(8750, endpoint?.port ?: -1)
    }

    @Test
    fun `bracketed ipv6 without port uses default port`() {
        val endpoint = parseLanEndpoint("[2408:8207:1234:5678::1]")
        assertEquals("2408:8207:1234:5678::1", endpoint?.host)
        assertEquals(LanSession.DEFAULT_PORT, endpoint?.port ?: -1)
    }

    @Test
    fun `bare ipv6 uses whole address and default port`() {
        val endpoint = parseLanEndpoint("2408:8207:1234:5678::1")
        assertEquals("2408:8207:1234:5678::1", endpoint?.host)
        assertEquals(LanSession.DEFAULT_PORT, endpoint?.port ?: -1)
    }

    @Test
    fun `ipv6 loopback is not mistaken for host and port`() {
        val endpoint = parseLanEndpoint("::1")
        assertEquals("::1", endpoint?.host)
        assertEquals(LanSession.DEFAULT_PORT, endpoint?.port ?: -1)
    }

    @Test
    fun `tls scheme forces tls for bare ipv6`() {
        val endpoint = parseLanEndpoint("tls://2408:8207:1234:5678::1")
        assertEquals("2408:8207:1234:5678::1", endpoint?.host)
        assertTrue(endpoint?.useTls == true)
    }

    @Test
    fun `tls scheme with hostname reports server name`() {
        val endpoint = parseLanEndpoint("tls://relay.example.test:9443")
        assertEquals("relay.example.test", endpoint?.host)
        assertEquals(9443, endpoint?.port ?: -1)
        assertTrue(endpoint?.useTls == true)
        assertEquals("relay.example.test", endpoint?.tlsServerName)
    }

    @Test
    fun `builtin relay host uses tls by default`() {
        val endpoint = parseLanEndpoint("47.109.32.249:8750")
        assertTrue(endpoint?.useTls == true)
        assertNull(endpoint?.tlsServerName)
    }

    @Test
    fun `lan address stays plaintext by default`() {
        val endpoint = parseLanEndpoint("192.168.1.227:8750")
        assertFalse(endpoint?.useTls == true)
    }

    @Test
    fun `invalid port and malformed bracket are rejected`() {
        assertNull(parseLanEndpoint("[2408::1]:70000"))
        assertNull(parseLanEndpoint("[2408::1]:abc"))
        assertNull(parseLanEndpoint("[2408::1]extra"))
        assertNull(parseLanEndpoint(""))
        assertNull(parseLanEndpoint("tls://"))
    }

    @Test
    fun `long ipv6 address is not truncated`() {
        val address = "2408:8207:1234:5678:9abc:def0:1234:5678"
        val endpoint = parseLanEndpoint("[$address]:8750")
        assertEquals(address, endpoint?.host)
        assertTrue(address.length + 6 > 40)
    }
}
