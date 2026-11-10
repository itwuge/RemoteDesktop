package com.rdc.remote

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 连接码挑战应答与 Python 侧 rdc.session.handshake.build_access_proof 的向量对拍.
 * 向量由 Python 生成: code=778899, sid=S-1, from=FROMDEV, to=TODEV, nonce=16 个 0 字节.
 */
class AccessProofTest {

    private val nonce = "AAAAAAAAAAAAAAAAAAAAAA=="

    @Test
    fun matchesPythonVector() {
        assertEquals(
            "8b9fd98c708baf7df5bc65943146bed546524436f209133210bb66409d9fd8f1",
            LanSession.accessProofResponse("778899", "S-1", "FROMDEV", "TODEV", nonce),
        )
    }

    @Test
    fun differentCodeProducesDifferentProof() {
        val a = LanSession.accessProofResponse("778899", "S-1", "FROMDEV", "TODEV", nonce)
        val b = LanSession.accessProofResponse("778890", "S-1", "FROMDEV", "TODEV", nonce)
        assertEquals(64, a.length)
        org.junit.Assert.assertNotEquals(a, b)
    }

    @Test
    fun sessionAndPeerAreBoundIntoProof() {
        val base = LanSession.accessProofResponse("778899", "S-1", "FROMDEV", "TODEV", nonce)
        org.junit.Assert.assertNotEquals(
            base,
            LanSession.accessProofResponse("778899", "S-2", "FROMDEV", "TODEV", nonce),
        )
        org.junit.Assert.assertNotEquals(
            base,
            LanSession.accessProofResponse("778899", "S-1", "FROMDEV", "OTHERDEV", nonce),
        )
    }
}
