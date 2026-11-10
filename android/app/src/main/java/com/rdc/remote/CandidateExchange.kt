// ==========================================================================================
// 文件        : CandidateExchange.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : offer/answer 候选交换: 全量上线与容错解析 (spec ipv6-punch §3.3、§3.4、§7.6)
// 架构定位    : 纯数据转换层, 不建连、不写日志格式
// 核心功能    : 候选转线上 Map; 排序/去重/过滤; answer 缺 candidates 或解析失败退回单候选
// 职责边界    : 不采集、不选路; 任何异常都不允许向上抛, 必须退回可用单候选
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34); 单测跑 JVM
// 依赖关系    : Candidate、CandidateSpec、JSONObject/JSONArray
// ==========================================================================================

package com.rdc.remote

import org.json.JSONArray
import org.json.JSONObject

/** 可上线的候选: 去重 → 排序 → 过滤回环/链路本地/未知族. */
internal fun offerCandidates(candidates: List<Candidate>): List<Candidate> =
    sortCandidates(dedupeCandidates(candidates)).filter { isPublishable(it) }

/**
 * 候选转线上 Map.
 *
 * 注意必须用 Map 而不是 JSONObject: 签名包络走 [CanonicalJson], 只认 Map/Iterable/基本类型.
 */
internal fun candidateWireMap(item: Candidate): LinkedHashMap<String, Any?> = linkedMapOf(
    "kind" to item.kind,
    "address" to item.address,
    "port" to item.port,
    "priority" to item.priority,
    "family" to item.family,
    "scope" to item.scope,
    "origin" to item.origin,
    "related" to item.related,
    "proto" to item.proto,
)

/** 把 answer 里的 Map 元素还原成候选; 未知键忽略, 非法值取默认. */
internal fun candidateFromMap(map: Map<*, *>): Candidate {
    val objectValue = JSONObject()
    for ((key, value) in map) {
        if (key == null || value == null || value === JSONObject.NULL) {
            continue
        }
        objectValue.put(key.toString(), value)
    }
    return Candidate.fromJson(objectValue)
}

/**
 * 解析 answer 的对端候选.
 *
 * 顺序: `candidates` 数组 → `chosen_candidate` 单对象 → 调用方给的兜底单候选.
 * 缺字段、类型不符、单条解析失败都只跳过该条, 绝不抛异常.
 */
internal fun parseAnswerCandidates(payload: Map<*, *>?, fallback: Candidate): List<Candidate> {
    if (payload == null) {
        return listOf(fallback)
    }
    val parsed = ArrayList<Candidate>()
    when (val raw = payload["candidates"]) {
        is List<*> -> {
            for (item in raw) {
                val map = item as? Map<*, *> ?: continue
                val candidate = runCatching { candidateFromMap(map) }.getOrNull() ?: continue
                if (usableCandidate(candidate)) {
                    parsed.add(candidate)
                }
            }
        }
        is JSONArray -> {
            for (candidate in parseCandidates(raw)) {
                if (usableCandidate(candidate)) {
                    parsed.add(candidate)
                }
            }
        }
    }
    if (parsed.isNotEmpty()) {
        return sortCandidates(dedupeCandidates(parsed))
    }
    val chosen = payload["chosen_candidate"] as? Map<*, *>
    if (chosen != null) {
        val candidate = runCatching { candidateFromMap(chosen) }.getOrNull()
        if (candidate != null && candidate.port in 1..65535 && candidate.address.isNotEmpty()) {
            return listOf(candidate)
        }
    }
    return listOf(fallback)
}

/** 单条候选是否可用于后续选路: 地址与端口合法, 且不是回环/链路本地. */
private fun usableCandidate(item: Candidate): Boolean =
    item.port in 1..65535 && item.address.isNotEmpty() && isPublishable(item)
