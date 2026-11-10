// ==========================================================================================
// 文件        : RemoteAccountClientTest.kt
// 所属目录    : android/app/src/test/java/com/rdc/remote/
// 文件用途    : 账号中心 /v1 客户端的纯 JVM 单测, 传输层全部注入桩
// 架构定位    : 数据层单测; 不依赖真实网络与 Android 运行时
// 核心功能    : 挑战+签名登录字段; 幂等键; Bearer 头; 设备列表解析; 令牌失效码判定
// 职责边界    : 只测 RemoteAccountClient, 不测界面与存储
// 运行环境    : JDK 17, JUnit 4, org.json 官方实现
// 依赖关系    : RemoteAccountClient
// ==========================================================================================

package com.rdc.remote

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class RemoteAccountClientTest {

    private val identity = FakeIdentity()
    private val transport = FakeTransport()
    private val client = RemoteAccountClient(
        endpoint = "https://relay.example.test:8760",
        identity = identity,
        transport = transport,
        devicePlatform = "android",
    )

    @Test
    fun `login sends challenge then signed login`() {
        transport.enqueue(200, envelope(JSONObject().put("nonce", "nonce-1")))
        transport.enqueue(
            200,
            envelope(
                JSONObject()
                    .put("account_id", "acc-1")
                    .put("account_name", "wuge")
                    .put("device_id", "dev-android")
                    .put("device_token", "device-token")
                    .put("session_token", "session-token")
                    .put("expires_at", "2026-11-10T12:15:00+08:00")
                    .put("device_expires_at", "2026-12-10T12:00:00+08:00")
                    .put("trusted", true),
            ),
        )

        val result = client.login("wuge", "password-1", "Pixel 8")

        assertEquals(2, transport.requests.size)
        val challenge = transport.requests[0]
        assertEquals("POST", challenge.method)
        assertEquals("/v1/devices/challenges", challenge.path)
        val challengeBody = JSONObject(String(challenge.body))
        assertEquals("dev-android", challengeBody.getString("device_id"))
        assertEquals(identity.publicKeyB64, challengeBody.getString("device_public_key"))
        assertTrue(challenge.headers.containsKey("Idempotency-Key"))

        val login = transport.requests[1]
        assertEquals("/v1/accounts/login", login.path)
        val loginBody = JSONObject(String(login.body))
        assertEquals("wuge", loginBody.getString("account"))
        assertEquals("nonce-1", loginBody.getString("nonce"))
        assertEquals("android", loginBody.getString("device_platform"))
        assertEquals("Pixel 8", loginBody.getString("device_name"))
        assertEquals(FakeIdentity.SIGNATURE, loginBody.getString("signature"))
        assertEquals("acc-1", result.accountId)
        assertEquals("device-token", result.deviceToken)
        assertEquals("session-token", result.sessionToken)
        assertTrue(result.trusted)
        assertTrue(result.deviceExpiresAt > result.sessionExpiresAt)
    }

    @Test
    fun `login requires nonce from challenge`() {
        transport.enqueue(200, envelope(JSONObject()))

        assertRejected(RemoteAccountClient.CODE_STORAGE_UNAVAILABLE) {
            client.login("wuge", "password-1", "Pixel")
        }
        assertEquals(1, transport.requests.size)
    }

    @Test
    fun `login rejects response without tokens`() {
        transport.enqueue(200, envelope(JSONObject().put("nonce", "n")))
        transport.enqueue(200, envelope(JSONObject().put("account_id", "acc-1")))

        try {
            client.login("wuge", "password-1", "Pixel")
            fail("应当拒绝缺少令牌的响应")
        } catch (error: RemoteBadResponse) {
            assertTrue(error.message.orEmpty().contains("令牌"))
        }
    }

    @Test
    fun `login error envelope carries server code and chinese message`() {
        transport.enqueue(200, envelope(JSONObject().put("nonce", "n")))
        transport.enqueue(401, errorEnvelope(RemoteAccountClient.CODE_CREDENTIAL_INVALID, "账号或口令不正确"))

        try {
            client.login("wuge", "bad-password", "Pixel")
            fail("应当抛出账号或口令错误")
        } catch (error: RemoteRejected) {
            assertEquals(RemoteAccountClient.CODE_CREDENTIAL_INVALID, error.code)
            assertEquals("账号或口令不正确", error.message)
            assertTrue(client.isSessionInvalid(error.code).not())
        }
    }

    @Test
    fun `devices parses items and marks invalid session codes`() {
        transport.enqueue(
            200,
            envelope(
                JSONObject().put(
                    "items",
                    org.json.JSONArray()
                        .put(
                            JSONObject()
                                .put("device_id", "dev-a")
                                .put("display_name", "书房电脑")
                                .put("platform", "windows")
                                .put("online", true)
                                .put("presence_state", "online")
                                .put("last_seen_at", "2026-11-10T12:00:00+08:00")
                                .put("trusted", true)
                                .put("trust_source", "owner")
                                .put("trusted_at", "2026-11-09T20:10:00+08:00")
                                .put("last_connected_at", "2026-11-10T12:00:00+08:00")
                                .put("current", false),
                        )
                        .put(JSONObject().put("device_id", "")),
                ),
            ),
        )

        val items = client.devices("device-token")

        assertEquals(1, items.size)
        assertEquals("dev-a", items[0].deviceId)
        assertEquals("书房电脑", items[0].displayName)
        assertEquals("windows", items[0].platform)
        assertTrue(items[0].online)
        assertTrue(items[0].trusted)
        assertEquals("owner", items[0].trustSource)
        assertEquals("2026-11-09T20:10:00+08:00", items[0].trustedAt)
        assertEquals("2026-11-10T12:00:00+08:00", items[0].lastConnectedAt)
        assertEquals("Bearer device-token", transport.requests[0].headers["Authorization"])
        assertTrue(client.isSessionInvalid(RemoteAccountClient.CODE_TOKEN_REVOKED))
        assertTrue(client.isSessionInvalid(RemoteAccountClient.CODE_TOKEN_EXPIRED))
        assertFalse(client.isSessionInvalid(RemoteAccountClient.CODE_CREDENTIAL_INVALID))
    }

    @Test
    fun `logout reports revoked tokens`() {
        transport.enqueue(
            200,
            envelope(JSONObject().put("revoked_tokens", 3).put("kicked_connections", 1)),
        )

        val result = client.logout("device-token")

        assertEquals(3, result.revokedTokens)
        assertEquals(1, result.kickedConnections)
        assertEquals("/v1/accounts/logout", transport.requests[0].path)
        assertTrue(transport.requests[0].headers.containsKey("Idempotency-Key"))
    }

    @Test
    fun `register posts account and password`() {
        transport.enqueue(
            200,
            envelope(
                JSONObject()
                    .put("account_id", "acc-2")
                    .put("account_name", "new-user")
                    .put("created_at", "2026-11-10T12:00:00+08:00"),
            ),
        )

        val result = client.register("new-user", "password-1")

        assertEquals("acc-2", result.accountId)
        val body = JSONObject(String(transport.requests[0].body))
        assertEquals("new-user", body.getString("account"))
        assertEquals("password-1", body.getString("password"))
    }

    @Test
    fun `transport failure maps to chinese unreachable message`() {
        transport.failWith(java.io.IOException("connection refused"))

        try {
            client.devices("device-token")
            fail("应当抛出不可达")
        } catch (error: RemoteUnavailable) {
            assertEquals(RemoteAccountClient.UNREACHABLE_MESSAGE, error.message)
        }
    }

    @Test
    fun `unparseable body is reported as bad response`() {
        transport.enqueue(200, "<html>gateway</html>")

        try {
            client.devices("device-token")
            fail("应当拒绝无法解析的响应")
        } catch (error: RemoteBadResponse) {
            assertTrue(error.message.orEmpty().contains("无法解析"))
        }
    }

    @Test
    fun `iso timestamps parse with offset and reject garbage`() {
        assertEquals(1762747200000L, RemoteAccountClient.parseIsoMillis("2025-11-10T12:00:00+08:00"))
        assertNull(RemoteAccountClient.parseIsoMillis("not-a-time"))
        assertNull(RemoteAccountClient.parseIsoMillis(""))
    }

    private fun assertRejected(expectedCode: String, block: () -> Unit) {
        try {
            block()
            fail("应当抛出 $expectedCode")
        } catch (error: RemoteRejected) {
            assertEquals(expectedCode, error.code)
        }
    }

    private fun envelope(data: JSONObject): String =
        JSONObject().put("code", 0).put("message", "ok").put("data", data).toString()

    private fun errorEnvelope(code: String, message: String): String =
        JSONObject().put("code", code).put("message", message).toString()

    private class FakeIdentity : AccountIdentity {
        override val deviceId: String = "dev-android"
        override val publicKeyB64: String = "cHVibGljLWtleQ=="
        override fun sign(payload: Map<String, String>): String {
            assertEquals("wuge", payload["account"])
            assertEquals("dev-android", payload["device_id"])
            require(!payload["nonce"].isNullOrBlank()) { "签名载荷缺少 nonce" }
            return SIGNATURE
        }

        companion object {
            const val SIGNATURE = "c2lnbmF0dXJl"
        }
    }

    private class FakeTransport : AccountTransport {
        data class Request(
            val method: String,
            val path: String,
            val headers: Map<String, String>,
            val body: ByteArray,
        )

        val requests = ArrayList<Request>()
        private val responses = ArrayDeque<AccountHttpResponse>()
        private var failure: Exception? = null

        fun enqueue(status: Int, body: String) {
            responses.addLast(AccountHttpResponse(status, body))
        }

        fun failWith(error: Exception) {
            failure = error
        }

        override fun request(
            method: String,
            path: String,
            headers: Map<String, String>,
            body: ByteArray,
        ): AccountHttpResponse {
            requests.add(Request(method, path, headers, body))
            failure?.let { throw it }
            return responses.removeFirstOrNull() ?: error("未准备桩响应: $method $path")
        }
    }
}
