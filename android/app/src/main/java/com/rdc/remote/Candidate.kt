// ==========================================================================================
// 文件        : Candidate.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 连接候选对象与 offer/answer 候选字段口径 (spec ipv6-punch §3、§7.4)
// 架构定位    : 纯数据与纯函数, 不建连、不开 socket、不依赖 Android 运行时
// 核心功能    : 全字段默认值; opt* 容错解析; family/scope/origin/proto 推断; 优先级表; 去重排序
// 职责边界    : 不采集、不选路; 未知 kind 只改写为 relay 并记日志, 不抛异常
// 运行环境    : Android 10 (API 29) 至 Android 14 (API 34); 单测跑 JVM
// 依赖关系    : 被 StunClient、CandidateGatherer 与后续打洞/选路层使用
// ==========================================================================================

package com.rdc.remote

import org.json.JSONArray
import org.json.JSONObject
import java.net.Inet6Address
import java.net.InetAddress
import java.net.UnknownHostException

/** 候选字段枚举与优先级表, 与 spec ipv6-punch §3.1、§3.2 一一对应. */
internal object CandidateSpec {

    const val KIND_HOST = "host"
    const val KIND_SRFLX = "srflx"
    const val KIND_RELAY = "relay"

    const val FAMILY_UNKNOWN = 0
    const val FAMILY_V4 = 4
    const val FAMILY_V6 = 6

    const val SCOPE_GLOBAL = "global"
    const val SCOPE_ULA = "ula"
    const val SCOPE_LINK = "link"
    const val SCOPE_PRIVATE = "private"

    const val ORIGIN_HOST = "host"
    const val ORIGIN_STUN = "stun"
    const val ORIGIN_RELAY = "relay"

    const val PROTO_TCP = "tcp"
    const val PROTO_UDP = "udp"

    const val PRIORITY_IPV6_GLOBAL = 100
    const val PRIORITY_HOST_V4_PRIVATE = 96
    const val PRIORITY_SRFLX = 90
    const val PRIORITY_HOST_V4_PUBLIC = 85
    const val PRIORITY_IPV6_ULA = 70
    const val PRIORITY_RELAY = 10

    /** 线上允许出现的 kind; 本地态 mapped 不在其中. */
    val PUBLISHED_KINDS = setOf(KIND_HOST, KIND_SRFLX, KIND_RELAY)
}

/**
 * 一个连通候选, 字段与 spec §3.1 一一对应, 全部有默认值.
 *
 * 解析只走 [fromJson] 的 opt* 路径: 未知键忽略、缺失键取默认值、未知 kind 按 relay 处理.
 */
internal data class Candidate(
    val kind: String = CandidateSpec.KIND_RELAY,
    val address: String = "",
    val port: Int = 0,
    val priority: Int = 0,
    val family: Int = CandidateSpec.FAMILY_UNKNOWN,
    val scope: String = "",
    val origin: String = "",
    val related: String = "",
    val proto: String = "",
) {

    /** 按 §3.3 编码为 offer/answer 元素; 地址不带方括号. */
    fun toJson(): JSONObject = JSONObject().apply {
        put("kind", kind)
        put("address", address)
        put("port", port)
        put("priority", priority)
        put("family", family)
        put("scope", scope)
        put("origin", origin)
        put("related", related)
        put("proto", proto)
    }

    companion object {

        /** 容错解析一条候选; 缺失或非法 kind 一律按 relay 处理, 不抛异常. */
        fun fromJson(value: JSONObject?): Candidate {
            if (value == null) {
                return Candidate()
            }
            val rawKind = jsonText(value, "kind", CandidateSpec.KIND_RELAY)
            val kind = if (rawKind in CandidateSpec.PUBLISHED_KINDS) {
                rawKind
            } else {
                ConnectLog.unknownKind(rawKind)
                CandidateSpec.KIND_RELAY
            }
            val address = normalizeAddress(jsonText(value, "address", ""))
            val declaredFamily = value.optInt("family", CandidateSpec.FAMILY_UNKNOWN)
            val family = if (declaredFamily == CandidateSpec.FAMILY_V4 || declaredFamily == CandidateSpec.FAMILY_V6) {
                declaredFamily
            } else {
                inferFamily(address)
            }
            val rawScope = jsonText(value, "scope", "")
            val rawOrigin = jsonText(value, "origin", "")
            val rawProto = jsonText(value, "proto", "")
            return Candidate(
                kind = kind,
                address = address,
                port = value.optInt("port", 0),
                priority = value.optInt("priority", 0),
                family = family,
                scope = rawScope.ifEmpty { scopeOf(address) },
                origin = rawOrigin.ifEmpty { originOf(kind) },
                related = normalizeAddress(jsonText(value, "related", "")),
                proto = rawProto.ifEmpty { protoOf(kind, family) },
            )
        }
    }
}

/** 解析候选数组; 缺失或类型不符一律返回空表, 由调用方退回单候选逻辑. */
internal fun parseCandidates(array: JSONArray?): List<Candidate> {
    if (array == null) {
        return emptyList()
    }
    val result = ArrayList<Candidate>(array.length())
    for (index in 0 until array.length()) {
        val item = array.optJSONObject(index) ?: continue
        result.add(Candidate.fromJson(item))
    }
    return result
}

/** 去掉方括号与 `%zone`, 与线上 address 字段口径一致. */
internal fun normalizeAddress(address: String): String {
    val text = address.trim().removePrefix("[").removeSuffix("]")
    val zone = text.indexOf('%')
    return if (zone >= 0) text.substring(0, zone) else text
}

/** 只解析 IP 字面量, 不做 DNS 解析; 非字面量返回 null. */
internal fun parseLiteralAddress(address: String): InetAddress? {
    val text = normalizeAddress(address)
    if (text.isEmpty()) {
        return null
    }
    val looksLikeV6 = text.contains(':')
    val matches = if (looksLikeV6) IPV6.matches(text) else IPV4.matches(text)
    if (!matches) {
        return null
    }
    return try {
        InetAddress.getByName(text)
    } catch (_: UnknownHostException) {
        null
    }
}

/** 按地址字面量推断地址族; 非字面量返回 0, 仅可挂在 relay 候选上. */
internal fun inferFamily(address: String): Int {
    val ip = parseLiteralAddress(address) ?: return CandidateSpec.FAMILY_UNKNOWN
    return if (ip is Inet6Address) CandidateSpec.FAMILY_V6 else CandidateSpec.FAMILY_V4
}

/** 按地址推断作用域; 仅 IPv6 有完整语义, IPv4 只区分私网. */
internal fun scopeOf(address: String): String {
    val ip = parseLiteralAddress(address) ?: return ""
    if (ip is Inet6Address) {
        if (ip.isLinkLocalAddress) {
            return CandidateSpec.SCOPE_LINK
        }
        val first = ip.address[0].toInt() and 0xff
        if (first and 0xfe == 0xfc) {
            return CandidateSpec.SCOPE_ULA
        }
        return if (first and 0xe0 == 0x20) CandidateSpec.SCOPE_GLOBAL else ""
    }
    if (ip.isLoopbackAddress || ip.isLinkLocalAddress) {
        return ""
    }
    if (ip.isSiteLocalAddress) {
        return CandidateSpec.SCOPE_PRIVATE
    }
    val bytes = ip.address
    // 100.64.0.0/10 (运营商级 NAT) 与桌面 ipaddress.is_private 保持同一判定
    if ((bytes[0].toInt() and 0xff) == 100 && (bytes[1].toInt() and 0xc0) == 0x40) {
        return CandidateSpec.SCOPE_PRIVATE
    }
    return ""
}

/** 按 kind 推断缺省 origin. */
internal fun originOf(kind: String): String = when (kind) {
    CandidateSpec.KIND_HOST -> CandidateSpec.ORIGIN_HOST
    CandidateSpec.KIND_SRFLX -> CandidateSpec.ORIGIN_STUN
    CandidateSpec.KIND_RELAY -> CandidateSpec.ORIGIN_RELAY
    else -> ""
}

/** 按 kind 与地址族推断缺省 proto: relay=tcp, srflx=udp, v6 host=tcp, v4 host=udp. */
internal fun protoOf(kind: String, family: Int): String = when {
    kind == CandidateSpec.KIND_RELAY -> CandidateSpec.PROTO_TCP
    kind == CandidateSpec.KIND_SRFLX -> CandidateSpec.PROTO_UDP
    kind == CandidateSpec.KIND_HOST && family == CandidateSpec.FAMILY_V6 -> CandidateSpec.PROTO_TCP
    kind == CandidateSpec.KIND_HOST -> CandidateSpec.PROTO_UDP
    else -> ""
}

/** 生成 host 候选; 回环、链路本地、组播与非法字面量优先级为 0, 不参与上线. */
internal fun hostCandidate(address: String, port: Int): Candidate {
    val ip = parseLiteralAddress(address)
    val family = when {
        ip == null -> CandidateSpec.FAMILY_UNKNOWN
        ip is Inet6Address -> CandidateSpec.FAMILY_V6
        else -> CandidateSpec.FAMILY_V4
    }
    val scope = scopeOf(address)
    val blocked = ip == null ||
        ip.isLoopbackAddress ||
        ip.isLinkLocalAddress ||
        ip.isMulticastAddress ||
        ip.isAnyLocalAddress
    val priority = when {
        blocked -> 0
        family == CandidateSpec.FAMILY_V6 && scope == CandidateSpec.SCOPE_GLOBAL -> CandidateSpec.PRIORITY_IPV6_GLOBAL
        family == CandidateSpec.FAMILY_V6 && scope == CandidateSpec.SCOPE_ULA -> CandidateSpec.PRIORITY_IPV6_ULA
        family == CandidateSpec.FAMILY_V4 && scope == CandidateSpec.SCOPE_PRIVATE -> CandidateSpec.PRIORITY_HOST_V4_PRIVATE
        family == CandidateSpec.FAMILY_V4 -> CandidateSpec.PRIORITY_HOST_V4_PUBLIC
        else -> 0
    }
    return Candidate(
        kind = CandidateSpec.KIND_HOST,
        address = normalizeAddress(address),
        port = port,
        priority = priority,
        family = family,
        scope = scope,
        origin = CandidateSpec.ORIGIN_HOST,
        proto = protoOf(CandidateSpec.KIND_HOST, family),
    )
}

/** 生成 STUN 反射候选: 优先级固定 90, related 为本地基地址. */
internal fun srflxCandidate(address: String, port: Int, related: String): Candidate = Candidate(
    kind = CandidateSpec.KIND_SRFLX,
    address = normalizeAddress(address),
    port = port,
    priority = CandidateSpec.PRIORITY_SRFLX,
    family = inferFamily(address),
    scope = "",
    origin = CandidateSpec.ORIGIN_STUN,
    related = normalizeAddress(related),
    proto = CandidateSpec.PROTO_UDP,
)

/** 生成中继候选: 优先级固定 10; 主机名地址 family=0, 只能走中继. */
internal fun relayCandidate(address: String, port: Int): Candidate = Candidate(
    kind = CandidateSpec.KIND_RELAY,
    address = normalizeAddress(address),
    port = port,
    priority = CandidateSpec.PRIORITY_RELAY,
    family = inferFamily(address),
    scope = "",
    origin = CandidateSpec.ORIGIN_RELAY,
    related = "",
    proto = CandidateSpec.PROTO_TCP,
)

/** 能否写进 offer/answer: 无地址/端口、回环/链路本地/组播、IPv6 link-local、未知 kind 一律不上线. */
internal fun isPublishable(item: Candidate): Boolean {
    if (item.port !in 1..65535 || item.address.isEmpty()) {
        return false
    }
    if (item.kind !in CandidateSpec.PUBLISHED_KINDS) {
        return false
    }
    val ip = parseLiteralAddress(item.address)
    if (ip != null && (ip.isLoopbackAddress || ip.isLinkLocalAddress ||
            ip.isMulticastAddress || ip.isAnyLocalAddress)
    ) {
        return false
    }
    if (item.kind == CandidateSpec.KIND_HOST && item.family == CandidateSpec.FAMILY_V6) {
        return item.scope == CandidateSpec.SCOPE_GLOBAL || item.scope == CandidateSpec.SCOPE_ULA
    }
    if (item.family == CandidateSpec.FAMILY_UNKNOWN) {
        return item.kind == CandidateSpec.KIND_RELAY
    }
    return true
}

/** 同一 `(kind, address, port)` 只保留优先级最高的一条. */
internal fun dedupeCandidates(items: List<Candidate>): List<Candidate> {
    val kept = LinkedHashMap<String, Candidate>()
    for (item in items) {
        val key = "${item.kind}|${item.address}|${item.port}"
        val current = kept[key]
        if (current == null || item.priority > current.priority) {
            kept[key] = item
        }
    }
    return kept.values.toList()
}

/** 排序键 `(-priority, family, address, port)`, 与桌面 `_candidate_sort_key` 一致. */
internal fun sortCandidates(items: List<Candidate>): List<Candidate> = items.sortedWith(
    compareBy({ -it.priority }, { it.family }, { it.address }, { it.port }),
)

private fun jsonText(value: JSONObject, key: String, fallback: String): String {
    val raw = value.opt(key) ?: return fallback
    if (raw == JSONObject.NULL) {
        return fallback
    }
    val text = raw.toString()
    return text.ifEmpty { fallback }
}

private val IPV4 = Regex("""^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$""")
private val IPV6 = Regex("""^[0-9A-Fa-f:.]+$""")
