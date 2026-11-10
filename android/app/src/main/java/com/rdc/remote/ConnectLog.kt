// ==========================================================================================
// 文件        : ConnectLog.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 连接路径结构化日志 (spec ipv6-punch §7.9)
// 架构定位    : 连接层公共设施, 只做字段拼装与输出, 不承载选路逻辑
// 核心功能    : logcat tag 固定 RDC_CONNECT, 每行一个 JSON, 字段名与桌面 §4.4 一致
// 职责边界    : 不记录访问码、令牌、密钥; 不落盘、不上传
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34)
// 依赖关系    : 被 StunClient、CandidateGatherer 与后续选路层调用
// ==========================================================================================

package com.rdc.remote

import android.util.Log
import org.json.JSONArray
import org.json.JSONObject

/**
 * 连接路径日志.
 *
 * 必带字段按 §7.9: event、transport、peer_address、peer_family、relay_used、reason、elapsed_ms;
 * 打洞与采集另带 candidates、tried、rtt_ms. 轮询采集阶段尚无对端, peer_address 留空、
 * peer_family 记 0, 路径确定后由选路层另外输出 connect.path_selected.
 */
internal object ConnectLog {

    const val TAG = "RDC_CONNECT"

    fun emit(
        event: String,
        transport: String,
        peerAddress: String = "",
        peerFamily: Int = 0,
        relayUsed: Boolean = false,
        reason: String = "ok",
        elapsedMs: Long = 0,
        extra: Map<String, Any?> = emptyMap(),
        warn: Boolean = false,
    ) {
        val json = JSONObject()
        json.put("event", event)
        json.put("transport", transport)
        json.put("peer_address", peerAddress)
        json.put("peer_family", peerFamily)
        json.put("relay_used", relayUsed)
        json.put("reason", reason)
        json.put("elapsed_ms", elapsedMs)
        for ((key, value) in extra) {
            json.put(
                key,
                when (value) {
                    null -> JSONObject.NULL
                    is Collection<*> -> JSONArray(value)
                    else -> value
                },
            )
        }
        val line = json.toString()
        if (warn) {
            Log.w(TAG, line)
        } else {
            Log.i(TAG, line)
        }
    }

    /** 收到未知 kind (含本地态 mapped) 时留痕, 按 relay 处理并继续. */
    fun unknownKind(rawKind: String) {
        emit(
            event = "connect.candidate_kind_unknown",
            transport = "relay",
            reason = "unknown_kind",
            extra = linkedMapOf("kind" to rawKind),
            warn = true,
        )
    }
}
