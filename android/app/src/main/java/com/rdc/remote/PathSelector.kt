// ==========================================================================================
// 文件        : PathSelector.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : L1/L2/L3 选路预算与判据 (spec ipv6-punch §4.1、§4.2、§7.8)
// 架构定位    : 纯决策层, 不建连、不开 socket、不依赖 Android 运行时
// 核心功能    : 对称型映射短路; EIM 才进 L2; hairpin 剔除; 打洞结果映射为确定 reason
// 职责边界    : 不实现探测与数据面; 只回答"该不该进 L2""失败原因是什么"
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34); 单测跑 JVM
// 依赖关系    : Candidate、StunClient、PunchChecker
// ==========================================================================================

package com.rdc.remote

/** 与桌面 policy.py 相同的一套预算, 不允许出现手机版口径. */
internal object ConnectBudget {
    const val L1_PER_CANDIDATE_MS = 800
    const val L1_ATTEMPTS = 2
    const val L1_BUDGET_MS = 1500
    const val L2_PER_PAIR_MS = 2000
    const val L2_BUDGET_MS = 5000
    const val L3_BUDGET_MS = 8000
    const val CHECK_INTERVAL_MS = 100
    const val SUCCESS_MIN_RESPONSES = 2
    const val SUCCESS_MIN_RTT_SAMPLES = 3
    const val RELAY_PREFETCH_AT_MS = 2000
}

/** 传输类型, 与桌面 TRANSPORT_* 一致. */
internal object Transport {
    /** 尚未确定路径 (连接开始/失败时使用). */
    const val PENDING = "pending"
    const val DIRECT_IPV6 = "direct_ipv6"
    const val DIRECT_UDP = "direct_udp"
    const val RELAY = "relay"
}

/** 失败原因码, 全部为确定值, 不写"失败"这类含糊文案. */
internal object PathReason {
    const val OK = "ok"
    const val SYMMETRIC_NAT = "symmetric_nat"
    const val SINGLE_DIRECTION = "single_direction"
    const val TIMEOUT = "timeout"
    const val NO_CANDIDATE = "no_candidate"
    const val HAIRPIN_BLOCKED = "hairpin_blocked"
    const val IPV6_NO_GLOBAL = "ipv6_no_global"
    const val IPV6_UNREACHABLE = "ipv6_unreachable"
    const val IPV6_DISABLED = "ipv6_disabled"
    const val PUNCH_DISABLED = "punch_disabled"
    const val RELAY_UNAVAILABLE = "relay_unavailable"
    const val NETWORK_CHANGED = "network_changed"
}

/** L2 是否进入打洞, 以及不进入时的确定原因. */
internal data class PunchGate(val attempt: Boolean, val reason: String)

/**
 * L2 前置判据.
 *
 * 本端对称型映射且没有 mapped 候选时直接判 `symmetric_nat`, 不进入 L2 空耗 5000 ms;
 * 关闭打洞开关时判 `punch_disabled`; 对端没有任何可打洞的 v4 候选时判 `no_candidate`.
 */
internal fun gatePunch(
    punchEnabled: Boolean,
    mapping: String,
    hasMapped: Boolean,
    peerCandidates: List<Candidate>,
): PunchGate {
    if (!punchEnabled) {
        return PunchGate(false, PathReason.PUNCH_DISABLED)
    }
    if (mapping == StunClient.MAPPING_SYMMETRIC && !hasMapped) {
        return PunchGate(false, PathReason.SYMMETRIC_NAT)
    }
    if (!hasPunchCandidates(peerCandidates)) {
        return PunchGate(false, PathReason.NO_CANDIDATE)
    }
    return PunchGate(true, PathReason.OK)
}

/** 对端是否存在可打洞的 IPv4 候选: host 或 srflx, 端口合法. */
internal fun hasPunchCandidates(peer: List<Candidate>): Boolean = peer.any {
    it.family == CandidateSpec.FAMILY_V4 &&
        it.port in 1..65535 &&
        (it.kind == CandidateSpec.KIND_HOST || it.kind == CandidateSpec.KIND_SRFLX)
}

/**
 * 打洞结果 → 确定 reason.
 *
 * 双向都达不到 ≥2 响应判 `timeout`; 只有单向达标或 RTT 样本不足判 `single_direction`.
 */
internal fun punchReason(result: PunchResult): String {
    if (result.success) {
        return PathReason.OK
    }
    val forwardOk = result.forward >= ConnectBudget.SUCCESS_MIN_RESPONSES
    val backwardOk = result.backward >= ConnectBudget.SUCCESS_MIN_RESPONSES
    if (!forwardOk && !backwardOk) {
        return PathReason.TIMEOUT
    }
    return PathReason.SINGLE_DIRECTION
}

/**
 * 剔除同一出口的 hairpin 候选.
 *
 * 对端候选里与本端 srflx 公网地址相同的 host/srflx/relay 一律移除: 同出口回连依赖路由器
 * hairpin, 现网不支持, 保留只会把"路由不支持回环"误判成打洞失败.
 *
 * @return (保留的候选, 被剔除的数量)
 */
internal fun removeHairpin(
    peer: List<Candidate>,
    local: List<Candidate>,
): Pair<List<Candidate>, Int> {
    val public = local
        .filter { it.kind == CandidateSpec.KIND_SRFLX && it.address.isNotEmpty() }
        .map { it.address }
        .toSet()
    if (public.isEmpty()) {
        return peer to 0
    }
    val kept = ArrayList<Candidate>(peer.size)
    var removed = 0
    for (item in peer) {
        val sameEgress = item.address.isNotEmpty() &&
            item.address in public &&
            (item.kind == CandidateSpec.KIND_HOST ||
                item.kind == CandidateSpec.KIND_SRFLX ||
                item.kind == CandidateSpec.KIND_RELAY)
        if (sameEgress) {
            removed += 1
        } else {
            kept.add(item)
        }
    }
    return kept to removed
}

/** 按对端 srflx 是否唯一判断对端映射; 少于两个样本时无法判定. */
internal fun classifyPeerMapping(peer: List<Candidate>): String {
    val srflx = peer.filter { it.kind == CandidateSpec.KIND_SRFLX }
    if (srflx.size < 2) {
        return StunClient.MAPPING_UNKNOWN
    }
    val unique = srflx.map { "${it.address}:${it.port}" }.toSet()
    return if (unique.size == 1) StunClient.MAPPING_EIM else StunClient.MAPPING_SYMMETRIC
}

/** 任一端对称都按对称处理, 与桌面 `establish_session` 的口径一致. */
internal fun effectiveMapping(localMapping: String, peer: List<Candidate>): String =
    if (classifyPeerMapping(peer) == StunClient.MAPPING_SYMMETRIC) {
        StunClient.MAPPING_SYMMETRIC
    } else {
        localMapping
    }
