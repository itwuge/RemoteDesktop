// ==========================================================================================
// 文件        : LanSession.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 控制端真实链路: 信令注册认证、offer/answer、relay_allocate、数据面握手与 AEAD 收发
// 架构定位    : Android 控制端真实链路层, 严格按 Python 参考实现 (src/rdc 与 scripts/e2e_common.py) 移植
// 核心功能    : 七字段签名包络; 4 字节长度帧; 挑战签名认证; H.264 帧下行; 指针/滚轮/键盘上行
// 职责边界    : 不改协议字段, 不实现画质降级; 不采集屏幕; 公网中继只加 TLS 1.3 传输层
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35); 公网中继 TLS 1.3, 局域网明文
// 依赖关系    : RdcCrypto、InputEvent、RdcLog; 信任锚 res/raw/relay_ca.crt

package com.rdc.remote

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.os.Handler
import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.EOFException
import java.io.InputStream
import java.io.OutputStream
import java.net.ConnectException
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.net.NoRouteToHostException
import java.net.Socket
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import javax.net.ssl.SSLException

/**
 * 真实会话单例.
 *
 * 流程与 scripts/session_controller.py 一致: 注册 → challenge → auth → offer → answer →
 * session_start → relay_allocate → relay_allocated → 数据面 relay_hello/relay_ready →
 * e2e_hello/e2e_hello_ack → AEAD 通道. 不新增任何协议字段.
 */
internal object LanSession {

    /** 公网中继入口: 与继电器部署参数一致, 也是连接页默认地址. */
    const val RELAY_HOST = "47.109.32.249"
    const val DEFAULT_PORT = 8750
    const val DEFAULT_ENDPOINT = "$RELAY_HOST:$DEFAULT_PORT"

    /** 中继固定档 720p30; 与 deploy/relay 的 target_kbps 对齐. */
    const val TARGET_WIDTH = 1280
    const val TARGET_HEIGHT = 720
    const val TARGET_FPS = 30
    const val TARGET_KBPS = 2900

    /** AEAD 握手版本: 2 表示带会话绑定, 与 e2e_common.HANDSHAKE_VERSION 一致. */
    private const val HANDSHAKE_VERSION = 2

    /** 直连建立后 e2e 握手的上限; 超时即判直连失败并回落中继. */
    private const val DIRECT_HANDSHAKE_TIMEOUT_MS = 3000

    /** 连接码挑战应答算法标识, 与 rdc.session.handshake.ACCESS_PROOF_ALG 一致. */
    internal const val ACCESS_PROOF_ALG = "hmac-sha256-v1"

    /** 一帧远端 H.264 数据. */
    data class VideoFrame(
        val width: Int,
        val height: Int,
        val fps: Int,
        val key: Boolean,
        val pts: Long,
        val data: ByteArray,
    )

    sealed class ConnectResult {
        data class Ok(val sessionId: String) : ConnectResult()
        data class Fail(val message: String) : ConnectResult()
    }

    private class LanFailure(message: String) : Exception(message)

    @Volatile
    var active: Boolean = false
        private set

    @Volatile
    var sessionId: String = ""
        private set

    @Volatile
    var peerId: String = ""
        private set

    @Volatile
    var lastError: String = ""
        private set

    /** 本次会话协商出的帧封装版本: 2 为二进制帧, 1 为兼容 JSON 帧. */
    @Volatile
    var frameVersion: Int = FrameCodec.VERSION_V1
        private set

    @Volatile
    private var channel: AeadChannel? = null

    @Volatile
    private var videoSink: ((VideoFrame) -> Unit)? = null

    /** 界面尚未挂载接收端时, 缓存从最近关键帧起的少量帧, 避免首屏等待下一个关键帧. */
    private val pendingFrames = ArrayDeque<VideoFrame>()
    private var pendingFromKeyframe = false
    private val pendingLock = Any()

    @Volatile
    private var dataOutput: OutputStream? = null

    private var signalSocket: Socket? = null
    private var dataSocket: Socket? = null
    private var readerThread: Thread? = null

    /** 采集/打洞/数据面共用的单个 IPv4 UDP socket; 走中继时关闭. */
    private var udpSocket: DatagramSocket? = null
    private var udpPlane: UdpSessionPlane? = null
    private var directSocket: Socket? = null
    private val sendLock = Any()

    /** 会话延迟采样; 口径与桌面 remote.py 的 ping/pong 一致. */
    private val latency = LatencyTracker()
    private var pingThread: Thread? = null

    /** 被控端 ready 报出的实际分辨率/帧率档位, 如 "1080P 30 帧". */
    @Volatile
    var streamProfile: String = ""
        private set

    /** 输入上行单线程, 保证事件顺序, 且不阻塞界面主线程. */
    private val inputExecutor = java.util.concurrent.Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "lan-input-send").apply { isDaemon = true }
    }
    private val main = Handler(Looper.getMainLooper())
    private val connecting = AtomicBoolean(false)
    private val frames = AtomicLong(0)
    private val bytes = AtomicLong(0)
    private val startedAt = AtomicLong(0)

    fun setVideoSink(sink: ((VideoFrame) -> Unit)?) {
        videoSink = sink
        if (sink == null) {
            synchronized(pendingLock) {
                pendingFrames.clear()
                pendingFromKeyframe = false
            }
            return
        }
        val ready = synchronized(pendingLock) {
            pendingFrames.toList().also {
                pendingFrames.clear()
                pendingFromKeyframe = false
            }
        }
        if (ready.isNotEmpty()) {
            RdcLog.info(TAG, "sink_flush", "frames=${ready.size}")
            for (frame in ready) {
                sink.invoke(frame)
            }
        }
    }

    fun fps(): Int {
        val start = startedAt.get()
        if (start == 0L) {
            return 0
        }
        val seconds = (System.nanoTime() - start) / 1_000_000_000.0
        return if (seconds > 0.5) (frames.get() / seconds).toInt() else 0
    }

    fun bitrateKbps(): Int {
        val start = startedAt.get()
        if (start == 0L) {
            return 0
        }
        val seconds = (System.nanoTime() - start) / 1_000_000_000.0
        return if (seconds > 0.5) ((bytes.get() * 8 / 1000) / seconds).toInt() else 0
    }

    /**
     * 发起会话连接; 公网中继自动走 TLS 1.3, 局域网保持明文; 结果回到主线程.
     *
     * @param host 中继或被控端 IP 或主机名
     * @param port 信令与中继共用端口
     * @param peer 被控端设备 ID
     * @param code 6 位临时连接码
     */
    fun connect(
        context: Context,
        host: String,
        port: Int,
        peer: String,
        code: String,
        useTls: Boolean = false,
        tlsServerName: String? = null,
        callback: (ConnectResult) -> Unit,
    ) {
        if (!connecting.compareAndSet(false, true)) {
            main.post { callback(ConnectResult.Fail("正在连接中，请稍候")) }
            return
        }
        stopInternal(clearSink = false)
        val identity = try {
            RdcIdentity.load(context.applicationContext)
        } catch (error: Exception) {
            connecting.set(false)
            RdcLog.error(
                TAG,
                "identity_init_failed",
                error.stackTraceToString().replace('\n', ' '),
            )
            val message = if (error is java.security.NoSuchAlgorithmException) {
                "本机不支持 Ed25519 设备身份，请使用 Android 13 及以上系统"
            } else {
                "设备身份初始化失败：${error.message ?: error.javaClass.simpleName}"
            }
            main.post { callback(ConnectResult.Fail(message)) }
            return
        }
        Thread(
            {
                runConnect(
                    context.applicationContext,
                    identity,
                    host,
                    port,
                    peer,
                    code,
                    useTls,
                    tlsServerName,
                    callback,
                )
            },
            "lan-connect",
        ).start()
    }

    private fun runConnect(
        context: Context,
        identity: RdcIdentity,
        host: String,
        port: Int,
        peer: String,
        code: String,
        useTls: Boolean,
        tlsServerName: String?,
        callback: (ConnectResult) -> Unit,
    ) {
        var stage = "连接被控端地址"
        frameVersion = FrameCodec.VERSION_V1
        try {
            ConnectLog.emit(
                event = "connect.start",
                transport = Transport.PENDING,
                reason = PathReason.OK,
                extra = linkedMapOf(
                    "host" to host,
                    "port" to port,
                    "peer" to peer,
                    "punch_enabled" to BuildConfig.PUNCH_ENABLED,
                ),
            )
            val signal = openSocket(context, host, port, SIGNAL_TIMEOUT_MS, useTls, tlsServerName)
            signalSocket = signal
            val input = BufferedInputStream(signal.getInputStream())
            val output = BufferedOutputStream(signal.getOutputStream())
            val sid = UUID.randomUUID().toString()

            stage = "设备认证"
            RdcLog.info(TAG, "connect_start", "host=$host port=$port peer=$peer")
            registerAndAuth(identity, input, output)

            // S9 候选采集: 打洞开关关闭时退回纯中继, 不采集、不建 UDP socket
            stage = "采集候选"
            val punchEnabled = BuildConfig.PUNCH_ENABLED
            val relayFallback = relayCandidate(host, port)
            val gathered = if (punchEnabled) {
                CandidateGatherer(context).gather(relayCandidates = listOf(relayFallback))
            } else {
                null
            }
            udpSocket = gathered?.socket
            val localCandidates = gathered?.candidates?.takeIf { it.isNotEmpty() }
                ?: listOf(relayFallback)
            val offerList = offerCandidates(localCandidates).ifEmpty { listOf(relayFallback) }

            stage = "发送会话请求"
            sendEnvelope(
                output,
                identity,
                "offer",
                linkedMapOf(
                    "session_id" to sid,
                    "from_device_id" to identity.deviceId,
                    "to_device_id" to peer,
                    "candidates" to offerList.map { candidateWireMap(it) },
                    "media_caps" to linkedMapOf(
                        "codecs" to listOf("h264"),
                        "max_width" to TARGET_WIDTH,
                        "max_height" to TARGET_HEIGHT,
                        "max_fps" to TARGET_FPS,
                        // 产品路径默认 v2 二进制帧; v1 只作为对端不支持的兜底
                        "frame_versions" to FrameCodec.LOCAL_VERSIONS,
                    ),
                    "access_proof" to accessProof(code, sid, identity.deviceId, peer),
                ),
                sid,
            )

            stage = "等待被控端应答"
            val answer = waitType(input, "answer")
            frameVersion = negotiatedFrameVersion(answer)
            if (frameVersion == FrameCodec.VERSION_V2) {
                RdcLog.info(TAG, "frame_version", "version=2")
            } else {
                RdcLog.warn(TAG, "frame_version_degraded", "version=1 reason=peer_caps_missing_v2")
            }

            // S9 选路: L1 IPv6 → L2 打洞 → 失败自动回落 L3 中继
            stage = "选择连接路径"
            val peerCandidates = parseAnswerCandidates(answer["payload"] as? Map<*, *>, relayFallback)
            ConnectLog.emit(
                event = "connect.candidate_exchange",
                transport = Transport.RELAY,
                reason = PathReason.OK,
                extra = linkedMapOf(
                    "local" to offerList.size,
                    "peer" to peerCandidates.size,
                    "peer_kinds" to peerCandidates.joinToString(",") { it.kind },
                ),
            )
            val pick = tryDirectTransport(
                context = context,
                identity = identity,
                sid = sid,
                peer = peer,
                gather = gathered,
                localCandidates = localCandidates,
                peerCandidates = peerCandidates,
                punchEnabled = punchEnabled,
                relayFallback = relayFallback,
            )
            val direct = pick.transport
            if (direct != null) {
                stage = "确认会话"
                sendEnvelope(
                    output,
                    identity,
                    "session_start",
                    linkedMapOf(
                        "session_id" to sid,
                        "from_device_id" to identity.deviceId,
                        "to_device_id" to peer,
                        "transport_kind" to direct.transport,
                        // 直连档位随用户选择, 实际档位以被控端 ready 报出的分辨率为准
                        "quality_profile" to AppPrefs.profile(context),
                    ),
                    sid,
                )
                activateSession(sid, peer, direct)
                ConnectLog.emit(
                    event = "connect.path_selected",
                    transport = direct.transport,
                    peerAddress = direct.peerAddress,
                    peerFamily = direct.peerFamily,
                    relayUsed = false,
                    reason = PathReason.OK,
                    elapsedMs = pick.elapsedMs,
                    extra = linkedMapOf(
                        "peer_port" to direct.peerPort,
                        "relay_used" to false,
                    ),
                )
                RdcLog.info(TAG, "connected", "session=${sid.take(8)} peer=$peer transport=${direct.transport}")
                main.post { callback(ConnectResult.Ok(sid)) }
                return
            }
            ConnectLog.emit(
                event = "connect.path_selected",
                transport = Transport.RELAY,
                peerAddress = "",
                peerFamily = 0,
                relayUsed = true,
                reason = pick.reason,
                elapsedMs = pick.elapsedMs,
                extra = linkedMapOf("relay_used" to true, "fallback" to true),
                warn = pick.reason != PathReason.OK && pick.reason != PathReason.NO_CANDIDATE,
            )
            // 直连未成立: 关闭采集阶段的 UDP socket, 走既有中继流程
            runCatching { udpSocket?.close() }
            udpSocket = null

            stage = "确认会话"
            sendEnvelope(
                output,
                identity,
                "session_start",
                linkedMapOf(
                    "session_id" to sid,
                    "from_device_id" to identity.deviceId,
                    "to_device_id" to peer,
                    "transport_kind" to "relay",
                    "quality_profile" to "L0",
                ),
                sid,
            )

            stage = "申请中继预算"
            sendEnvelope(
                output,
                identity,
                "relay_allocate",
                linkedMapOf(
                    "session_id" to sid,
                    "requested_kbps" to TARGET_KBPS,
                    "est_duration_s" to 3600,
                ),
                sid,
            )
            val allocated = waitType(input, "relay_allocated")
            val allocatedPayload = allocated["payload"] as? Map<*, *> ?: emptyMap<Any, Any>()
            val token = allocatedPayload["relay_token"]?.toString().orEmpty()
            if (token.isEmpty()) {
                throw LanFailure("被控端未下发中继令牌")
            }

            stage = "接入数据面"
            val data = openSocket(context, host, port, DATA_TIMEOUT_MS, useTls, tlsServerName)
            dataSocket = data
            val dataIn = BufferedInputStream(data.getInputStream())
            val dataOut = BufferedOutputStream(data.getOutputStream())
            dataOutput = dataOut
            sendJson(
                dataOut,
                linkedMapOf(
                    "plane" to "data",
                    "v" to 1,
                    "type" to "relay_hello",
                    "relay_token" to token,
                    "session_id" to sid,
                    "role" to "subscriber",
                ),
            )
            val ready = readJsonMap(dataIn) ?: throw LanFailure("被控端数据面未就绪")
            if (ready["type"] != "relay_ready") {
                throw LanFailure("数据面握手失败：${ready["type"]}")
            }

            stage = "建立端到端加密"
            val aead = controllerHandshake(identity, dataIn, dataOut, sid, peer)

            sessionId = sid
            peerId = peer
            channel = aead
            frames.set(0)
            bytes.set(0)
            startedAt.set(System.nanoTime())
            lastError = ""
            active = true
            startLatencyProbe()
            readerThread = Thread({ readLoop(dataIn, aead) }, "lan-reader").apply { start() }
            RdcLog.info(TAG, "connected", "session=${sid.take(8)} peer=$peer")
            main.post { callback(ConnectResult.Ok(sid)) }
        } catch (error: Throwable) {
            val message = describeFailure(error, stage, host, port)
            lastError = message
            active = false
            channel = null
            ConnectLog.emit(
                event = "connect.failed",
                transport = Transport.PENDING,
                reason = error.javaClass.simpleName,
                extra = linkedMapOf("stage" to stage),
                warn = true,
            )
            RdcLog.error(TAG, "connect_failed", "stage=$stage reason=${error.javaClass.simpleName} message=$message")
            main.post { callback(ConnectResult.Fail(message)) }
            stopInternal(clearSink = false)
        } finally {
            connecting.set(false)
        }
    }

    /** 一条已建立的直连数据面: 传输类型、对端端点、流与 AEAD 通道. */
    private class DirectTransport(
        val transport: String,
        val peerAddress: String,
        val peerFamily: Int,
        val peerPort: Int,
        val input: InputStream,
        val output: OutputStream,
        val aead: AeadChannel,
    )

    /** 选路结果; transport 为空表示直连未成立, reason 给出确定回落原因. */
    private data class PathAttempt(
        val transport: DirectTransport?,
        val reason: String,
        val elapsedMs: Long,
    )

    /**
     * L1 → L2 依次尝试建立直连.
     *
     * L1 判据是 TCP 建连成功; L2 判据是双向各 ≥2 响应且 ≥3 个 RTT 样本.
     * 本端对称型映射且无 mapped 候选时直接判 `symmetric_nat`, 不进入 L2 空耗.
     * 打洞成功后在同一个 UDP socket 上跑 e2e 握手, P2P 路径不发 relay_allocate.
     */
    private fun tryDirectTransport(
        context: Context,
        identity: RdcIdentity,
        sid: String,
        peer: String,
        gather: GatherResult?,
        localCandidates: List<Candidate>,
        peerCandidates: List<Candidate>,
        punchEnabled: Boolean,
        relayFallback: Candidate,
    ): PathAttempt {
        val started = System.nanoTime()
        if (!punchEnabled) {
            return PathAttempt(null, PathReason.PUNCH_DISABLED, elapsedMillis(started))
        }

        // 与桌面 policy.level_ipv6 对齐: 取全部 v6 候选, 不限 kind
        val v6 = peerCandidates.filter {
            it.family == CandidateSpec.FAMILY_V6 && it.port in 1..65535
        }
        val l1Started = System.nanoTime()
        val l1 = if (v6.isEmpty()) null else probeIpv6Tcp(v6, started)
        val l1Ok = l1 != null && l1.error.isEmpty()
        ConnectLog.emit(
            event = "connect.level_result",
            transport = Transport.DIRECT_IPV6,
            peerAddress = l1?.peerAddress.orEmpty(),
            peerFamily = if (l1Ok) CandidateSpec.FAMILY_V6 else 0,
            relayUsed = false,
            reason = when {
                l1Ok -> PathReason.OK
                v6.isEmpty() -> PathReason.NO_CANDIDATE
                else -> PathReason.IPV6_UNREACHABLE
            },
            elapsedMs = elapsedMillis(l1Started),
            extra = linkedMapOf(
                "level" to "L1",
                "ok" to l1Ok,
                "candidates" to v6.size,
                "tried" to (l1?.tried ?: 0),
                "rtt_ms" to (l1?.rttMs ?: emptyList<Long>()),
                "peer_port" to (l1?.peerPort ?: 0),
                "error" to (l1?.error.orEmpty()),
            ),
            warn = !l1Ok && v6.isNotEmpty(),
        )
        // L1 只判可达; 判定成立后新建 TCP 到命中的 v6 候选, 首帧发 e2e_hello (方案 B)
        if (l1 != null && l1.error.isEmpty()) {
            val channel = establishIpv6Channel(identity, sid, peer, l1)
            if (channel != null) {
                return PathAttempt(channel, PathReason.OK, elapsedMillis(started))
            }
        }

        val (peerV4, hairpin) = removeHairpin(peerCandidates, localCandidates)
        val mapping = effectiveMapping(gather?.mapping ?: StunClient.MAPPING_UNKNOWN, peerCandidates)
        // 现网 UPnP/NAT-PMP/PCP 全不可用, mapped 候选预期为空; 保留判定以便后续接入
        val hasMapped = localCandidates.any { it.origin == "mapped" }
        val gate = gatePunch(punchEnabled, mapping, hasMapped, peerV4)
        if (!gate.attempt) {
            ConnectLog.emit(
                event = "connect.level_result",
                transport = Transport.DIRECT_UDP,
                reason = gate.reason,
                extra = linkedMapOf(
                    "level" to "L2",
                    "mapping" to mapping,
                    "hairpin_removed" to hairpin,
                ),
                warn = true,
            )
            return PathAttempt(null, gate.reason, elapsedMillis(started))
        }

        var socket = gather?.socket
        if (socket == null) {
            return PathAttempt(null, PathReason.NO_CANDIDATE, elapsedMillis(started))
        }
        var switched = false
        val watcher = watchNetwork(context) { switched = true }
        try {
            var punch = PunchChecker(socket).run(
                peer = peerV4,
                budgetMs = ConnectBudget.L2_BUDGET_MS,
                perCandidateMs = ConnectBudget.L2_PER_PAIR_MS,
                shouldAbort = { switched },
            )
            if (switched && !punch.success) {
                // 采集阶段已注销回调, 这里由数据面自己感知切换并重新采集一次
                runCatching { socket?.close() }
                val regathered = CandidateGatherer(context).gather(
                    relayCandidates = listOf(relayFallback),
                )
                socket = regathered.socket
                udpSocket = socket
                switched = false
                ConnectLog.emit(
                    event = "connect.level_result",
                    transport = Transport.DIRECT_UDP,
                    reason = PathReason.NETWORK_CHANGED,
                    extra = linkedMapOf("level" to "L2", "regathered" to true),
                    warn = true,
                )
                if (socket == null || regathered.networkChanged) {
                    return PathAttempt(null, PathReason.NETWORK_CHANGED, elapsedMillis(started))
                }
                val retrySocket = socket ?: return PathAttempt(null, PathReason.NO_CANDIDATE, elapsedMillis(started))
                punch = PunchChecker(retrySocket).run(
                    peer = peerV4,
                    budgetMs = ConnectBudget.L2_BUDGET_MS,
                    perCandidateMs = ConnectBudget.L2_PER_PAIR_MS,
                )
            }
            ConnectLog.emit(
                event = "connect.level_result",
                transport = Transport.DIRECT_UDP,
                peerAddress = punch.peerAddress,
                peerFamily = if (punch.peerAddress.isEmpty()) 0 else CandidateSpec.FAMILY_V4,
                reason = punch.reason,
                elapsedMs = punch.elapsedMs,
                extra = linkedMapOf(
                    "level" to "L2",
                    "forward" to punch.forward,
                    "backward" to punch.backward,
                    "sent" to punch.sent,
                    "rtt_ms" to punch.rttSamples,
                    "hairpin_removed" to hairpin,
                    "mapping" to mapping,
                    "peer_port" to punch.peerPort,
                    "targets" to punch.targets,
                ),
                warn = !punch.success,
            )
            if (!punch.success) {
                return PathAttempt(null, punchReason(punch), elapsedMillis(started))
            }

            val connected = socket
                ?: return PathAttempt(null, PathReason.NO_CANDIDATE, elapsedMillis(started))
            val endpoint = InetSocketAddress(punch.peerAddress, punch.peerPort)
            runCatching { connected.connect(endpoint) }
            val plane = UdpSessionPlane(connected, endpoint) {
                ConnectLog.emit(
                    event = "connect.path_dead",
                    transport = Transport.DIRECT_UDP,
                    peerAddress = endpoint.address?.hostAddress.orEmpty(),
                    peerFamily = CandidateSpec.FAMILY_V4,
                    relayUsed = false,
                    reason = PathReason.TIMEOUT,
                    extra = linkedMapOf("idle_ms" to UdpSessionPlane.PATH_DEAD_MS),
                    warn = true,
                )
            }
            udpPlane = plane
            val handshakeDone = java.util.concurrent.atomic.AtomicBoolean(false)
            val guard = Thread {
                try {
                    Thread.sleep(DIRECT_HANDSHAKE_TIMEOUT_MS.toLong())
                } catch (_: InterruptedException) {
                    return@Thread
                }
                if (!handshakeDone.get()) {
                    runCatching { plane.close() }
                }
            }.apply {
                isDaemon = true
                start()
            }
            try {
                val aead = controllerHandshake(identity, plane.input, plane.output, sid, peer)
                handshakeDone.set(true)
                return PathAttempt(
                    DirectTransport(
                        transport = Transport.DIRECT_UDP,
                        peerAddress = endpoint.address?.hostAddress.orEmpty(),
                        peerFamily = CandidateSpec.FAMILY_V4,
                        peerPort = endpoint.port,
                        input = plane.input,
                        output = plane.output,
                        aead = aead,
                    ),
                    PathReason.OK,
                    elapsedMillis(started),
                )
            } catch (error: Throwable) {
                handshakeDone.set(true)
                runCatching { plane.close() }
                udpPlane = null
                RdcLog.warn(TAG, "direct_handshake_failed", "transport=direct_udp reason=${error.message}")
                return PathAttempt(null, PathReason.TIMEOUT, elapsedMillis(started))
            } finally {
                guard.interrupt()
            }
        } finally {
            unwatchNetwork(context, watcher)
        }
    }

    /** L1 纯 TCP 可达探测结果; 只证明三次握手可达, 不占用数据面连接. */
    private data class L1Probe(
        val peerAddress: String,
        val peerPort: Int,
        val rttMs: List<Long>,
        val tried: Int,
        val error: String,
    )

    /**
     * L1 IPv6 直连探测: 800 ms/次、最多 2 次、层预算 1500 ms.
     *
     * 与桌面 TcpDirectProber 对齐: 只做 TCP 三次握手, 成功即记 RTT 并立即关闭,
     * 不在探测连接上跑 e2e 握手, 也不占用数据面 (数据面接入另行确认).
     */
    private fun probeIpv6Tcp(
        candidates: List<Candidate>,
        started: Long,
    ): L1Probe {
        val deadline = started + ConnectBudget.L1_BUDGET_MS * 1_000_000L
        val rtts = ArrayList<Long>(ConnectBudget.L1_ATTEMPTS)
        var tried = 0
        var lastError = ""
        for (candidate in candidates) {
            if (tried >= ConnectBudget.L1_ATTEMPTS || System.nanoTime() >= deadline) {
                break
            }
            tried += 1
            val remaining = ((deadline - System.nanoTime()) / 1_000_000L).toInt().coerceAtLeast(1)
            val connectTimeout = minOf(ConnectBudget.L1_PER_CANDIDATE_MS, remaining)
            val socket = Socket()
            val probeStarted = System.nanoTime()
            try {
                socket.tcpNoDelay = true
                socket.connect(InetSocketAddress(candidate.address, candidate.port), connectTimeout)
                val rtt = elapsedMillis(probeStarted)
                rtts.add(rtt)
                runCatching { socket.close() }
                RdcLog.info(TAG, "l1_probe_ok", "peer=${candidate.address}:${candidate.port} rtt_ms=$rtt")
                return L1Probe(candidate.address, candidate.port, rtts, tried, "")
            } catch (error: Throwable) {
                lastError = "${candidate.address}:${candidate.port} ${error.javaClass.simpleName}"
                RdcLog.warn(
                    TAG,
                    "l1_probe_failed",
                    "peer=${candidate.address}:${candidate.port} reason=${error.javaClass.simpleName} " +
                        "message=${error.message.orEmpty()}",
                )
                runCatching { socket.close() }
            }
        }
        return L1Probe("", 0, rtts, tried, lastError)
    }

    /**
     * 方案 B: L1 判定成立后新建一条 TCP 到命中的 v6 候选, 首帧发 e2e_hello.
     *
     * 被控端按首帧分流后把该连接升级为加密媒体通道; 握手失败则回落 L2/中继.
     */
    private fun establishIpv6Channel(
        identity: RdcIdentity,
        sid: String,
        peer: String,
        probe: L1Probe,
    ): DirectTransport? {
        val socket = Socket()
        val channelStarted = System.nanoTime()
        try {
            socket.tcpNoDelay = true
            socket.connect(
                InetSocketAddress(probe.peerAddress, probe.peerPort),
                ConnectBudget.L1_PER_CANDIDATE_MS,
            )
            socket.soTimeout = DIRECT_HANDSHAKE_TIMEOUT_MS
            val input = BufferedInputStream(socket.getInputStream())
            val output = BufferedOutputStream(socket.getOutputStream())
            val aead = controllerHandshake(identity, input, output, sid, peer)
            directSocket = socket
            return DirectTransport(
                transport = Transport.DIRECT_IPV6,
                peerAddress = probe.peerAddress,
                peerFamily = CandidateSpec.FAMILY_V6,
                peerPort = probe.peerPort,
                input = input,
                output = output,
                aead = aead,
            )
        } catch (error: Throwable) {
            RdcLog.warn(
                TAG,
                "l1_channel_failed",
                "peer=${probe.peerAddress}:${probe.peerPort} reason=${error.javaClass.simpleName} " +
                    "message=${error.message.orEmpty()}",
            )
            ConnectLog.emit(
                event = "connect.direct_channel_failed",
                transport = Transport.DIRECT_IPV6,
                peerAddress = probe.peerAddress,
                peerFamily = CandidateSpec.FAMILY_V6,
                reason = error.javaClass.simpleName,
                elapsedMs = elapsedMillis(channelStarted),
                extra = linkedMapOf("peer_port" to probe.peerPort),
                warn = true,
            )
            runCatching { socket.close() }
            return null
        }
    }

    /** 网络切换监听: 默认网络变化时回调; 采集阶段已注销, 打洞由这里重新接管. */
    private fun watchNetwork(
        context: Context,
        onSwitch: () -> Unit,
    ): ConnectivityManager.NetworkCallback? {
        val manager = context.getSystemService(ConnectivityManager::class.java) ?: return null
        val initial = manager.activeNetwork ?: return null
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                if (network != initial) {
                    onSwitch()
                }
            }

            override fun onLost(network: Network) {
                if (network == initial) {
                    onSwitch()
                }
            }
        }
        return try {
            manager.registerDefaultNetworkCallback(callback)
            callback
        } catch (_: Exception) {
            null
        }
    }

    private fun unwatchNetwork(context: Context, callback: ConnectivityManager.NetworkCallback?) {
        if (callback == null) {
            return
        }
        runCatching {
            context.getSystemService(ConnectivityManager::class.java)?.unregisterNetworkCallback(callback)
        }
    }

    /** 直连成立后的会话绑定: 与中继路径共用同一套 AEAD 收发与读循环. */
    private fun activateSession(sid: String, peer: String, direct: DirectTransport) {
        sessionId = sid
        peerId = peer
        channel = direct.aead
        frames.set(0)
        bytes.set(0)
        startedAt.set(System.nanoTime())
        lastError = ""
        active = true
        dataOutput = direct.output
        startLatencyProbe()
        readerThread = Thread({ readLoop(direct.input, direct.aead) }, "lan-reader").apply { start() }
    }

    /** 会话建立后按 1 s 周期发 ping, 收到 pong 记一次 RTT; 探测失败不影响会话. */
    private fun startLatencyProbe() {
        latency.reset()
        streamProfile = ""
        pingThread = Thread({ latencyLoop() }, "lan-ping").apply {
            isDaemon = true
            start()
        }
    }

    private fun latencyLoop() {
        while (active) {
            try {
                Thread.sleep(LatencyTracker.PING_INTERVAL_MS)
            } catch (_: InterruptedException) {
                return
            }
            if (!active) {
                return
            }
            val aead = channel ?: continue
            val out = dataOutput ?: continue
            val sentAt = System.nanoTime()
            val seq = latency.nextProbe(sentAt)
            try {
                val body = CanonicalJson.bytes(
                    linkedMapOf("kind" to "ping", "seq" to seq, "t" to sentAt),
                )
                val ciphertext = aead.encrypt(body)
                synchronized(sendLock) { writeFrame(out, ciphertext) }
            } catch (error: Exception) {
                // 单次探测失败只丢样本, 不影响会话与码率统计
                RdcLog.warn(
                    TAG,
                    "ping_send_failed",
                    "seq=$seq reason=${error.javaClass.simpleName}",
                )
            }
            latency.expire(System.nanoTime())
        }
    }

    /** 与桌面 remote.py `_actual_profile` 同口径: 4K 阈值 2160, 其余按高度取 P. */
    private fun actualProfile(width: Int, height: Int, fps: Int): String {
        if (width <= 0 || height <= 0) {
            return ""
        }
        val label = if (height >= 2160) "4K" else "${height}P"
        return if (fps > 0) "$label $fps 帧" else label
    }

    fun latencySnapshot(): LatencySnapshot = latency.snapshot()

    private fun elapsedMillis(startedNanos: Long): Long = (System.nanoTime() - startedNanos) / 1_000_000L

    /** 建连并按目标选择明文或 TLS 1.3; TLS 失败时关闭底层连接, 由上层给出中文原因. */
    private fun openSocket(
        context: Context,
        host: String,
        port: Int,
        readTimeoutMs: Int,
        useTls: Boolean,
        tlsServerName: String?,
    ): Socket {
        if (useTls) {
            return RelayTls.connect(context, host, port, CONNECT_TIMEOUT_MS, readTimeoutMs, tlsServerName)
        }
        return Socket().apply {
            tcpNoDelay = true
            connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
            soTimeout = readTimeoutMs
        }
    }

    /**
     * 连接码挑战应答: 对「算法|会话号|主叫|被叫|nonce」做 HMAC-SHA256, 只传证明不传连接码.
     * 与 rdc.session.handshake.build_access_proof 对齐.
     */
    private fun accessProof(code: String, sessionId: String, fromId: String, toId: String): Map<String, String> {
        val nonce = ByteArray(16).also { java.security.SecureRandom().nextBytes(it) }
        val nonceB64 = b64(nonce)
        return linkedMapOf(
            "alg" to ACCESS_PROOF_ALG,
            "nonce" to nonceB64,
            "response" to accessProofResponse(code, sessionId, fromId, toId, nonceB64),
        )
    }

    /** 纯函数形式, 便于与 Python 向量对拍. */
    internal fun accessProofResponse(
        code: String,
        sessionId: String,
        fromId: String,
        toId: String,
        nonceB64: String,
    ): String {
        val message = "$ACCESS_PROOF_ALG|$sessionId|$fromId|$toId|$nonceB64".toByteArray(Charsets.UTF_8)
        val mac = javax.crypto.Mac.getInstance("HmacSHA256")
        mac.init(javax.crypto.spec.SecretKeySpec(code.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        return mac.doFinal(message).joinToString("") { "%02x".format(it) }
    }

    private fun registerAndAuth(identity: RdcIdentity, input: InputStream, output: OutputStream) {
        sendEnvelope(
            output,
            identity,
            "register",
            linkedMapOf(
                "device_id" to identity.deviceId,
                "public_key" to b64(identity.publicKey),
                "capabilities" to listOf("desktop", "control", "clipboard", "file_transfer"),
                "protocol_version" to 1,
                "display_name" to "安卓控制端",
            ),
        )
        val challenge = waitType(input, "challenge")
        RdcLog.info(TAG, "challenge_received", "device=${identity.deviceId}")
        val payload = challenge["payload"] as? Map<*, *> ?: emptyMap<Any, Any>()
        val nonce = payload["nonce"]?.toString().orEmpty()
        if (nonce.isEmpty()) {
            throw LanFailure("服务端挑战缺少 nonce")
        }
        val signature = b64(
            identity.sign(
                CanonicalJson.bytes(
                    linkedMapOf(
                        "device_id" to identity.deviceId,
                        "nonce" to nonce,
                    ),
                ),
            ),
        )
        sendEnvelope(
            output,
            identity,
            "auth",
            linkedMapOf(
                "device_id" to identity.deviceId,
                "signature" to signature,
            ),
        )
        waitType(input, "auth_ok")
        RdcLog.info(TAG, "auth_ok", "device=${identity.deviceId}")
    }

    /** 控制端 AEAD 握手, 并校验被控端身份签名; 与 e2e_common.controller_handshake 对齐. */
    private fun controllerHandshake(
        identity: RdcIdentity,
        input: InputStream,
        output: OutputStream,
        sessionId: String,
        peerId: String,
    ): AeadChannel {
        val pair = X25519Pair.generate()
        val salt = ByteArray(16).also { java.security.SecureRandom().nextBytes(it) }
        val hello = linkedMapOf<String, Any?>(
            "kind" to "e2e_hello",
            "v" to 1,
            // hs=2 表示握手带会话绑定, 与会话号、双方设备标识一起进入 HKDF 上下文
            "hs" to HANDSHAKE_VERSION,
            "x25519" to b64(pair.publicKey),
            "salt" to b64(salt),
            "device_id" to identity.deviceId,
            "public_key" to b64(identity.publicKey),
            "session_id" to sessionId,
            "peer_device_id" to peerId,
        )
        hello["sig"] = b64(identity.sign(CanonicalJson.bytes(hello)))
        sendJson(output, hello)

        val ack = readJsonMap(input) ?: throw LanFailure("被控端握手应答缺失")
        if (ack["kind"] != "e2e_hello_ack") {
            throw LanFailure("被控端握手类型不符")
        }
        if ((ack["hs"] as? Number)?.toInt() != HANDSHAKE_VERSION) {
            throw LanFailure("被控端握手未声明会话绑定")
        }
        if (ack["session_id"]?.toString() != sessionId) {
            throw LanFailure("被控端握手会话号不匹配")
        }
        if (ack["peer_device_id"]?.toString() != identity.deviceId) {
            throw LanFailure("被控端握手未回指本控制端")
        }
        val unsigned = ack.filterKeys { it != "sig" }
        val publicKeyText = ack["public_key"] as? String ?: throw LanFailure("被控端未提供公钥")
        val signatureText = ack["sig"] as? String ?: throw LanFailure("被控端未提供签名")
        val publicKey = b64d(publicKeyText)
        if (!RdcIdentity.verify(publicKey, CanonicalJson.bytes(unsigned), b64d(signatureText))) {
            throw LanFailure("被控端身份签名无效")
        }
        if (RdcIdentity.deviceIdFromPublicKey(publicKey) != ack["device_id"]?.toString()) {
            throw LanFailure("被控端设备标识与公钥不一致")
        }
        if (ack["device_id"]?.toString() != peerId) {
            throw LanFailure("被控端设备标识与目标设备不一致")
        }
        val peerEphemeral = ack["x25519"] as? String ?: throw LanFailure("被控端未提供临时公钥")
        val shared = x25519Shared(pair.privateKey, b64d(peerEphemeral))
        val context = "hs$HANDSHAKE_VERSION|$sessionId|${identity.deviceId}|$peerId".toByteArray(Charsets.UTF_8)
        return deriveAeadSession(shared, salt, initiator = true, context = context)
    }

    /** answer 中显式声明的帧版本优先, 否则按双方能力表求交集; 缺省一律 v1. */
    private fun negotiatedFrameVersion(answer: Map<String, Any?>): Int {
        val payload = answer["payload"] as? Map<*, *>
        val caps = payload?.get("media_caps") as? Map<*, *>
        val declared = (caps?.get("frame_version") as? Number)?.toInt()
        if (declared != null && declared in FrameCodec.LOCAL_VERSIONS) {
            return declared
        }
        return FrameCodec.negotiateVersion(caps)
    }

    private fun decodeV2Frame(packet: ByteArray): VideoFrame {
        val decoded = try {
            FrameCodec.decodeV2(packet)
        } catch (error: FrameCodec.FrameException) {
            throw LanFailure("视频帧无效（${error.code}）：${error.message}")
        }
        return wrapFrame(decoded, TARGET_FPS)
    }

    private fun wrapFrame(decoded: FrameCodec.DecodedFrame, fps: Int): VideoFrame = VideoFrame(
        width = decoded.width,
        height = decoded.height,
        fps = fps,
        key = decoded.keyframe,
        pts = decoded.pts,
        data = decoded.payload,
    )

    private fun dispatchFrame(frame: VideoFrame) {
        frames.incrementAndGet()
        bytes.addAndGet(frame.data.size.toLong())
        val sink = videoSink
        if (sink != null) {
            sink.invoke(frame)
        } else {
            bufferFrame(frame)
        }
    }

    private fun readLoop(input: InputStream, aead: AeadChannel) {
        try {
            while (active) {
                val body = readFrame(input) ?: throw LanFailure("连接已断开")
                val plaintext = aead.decrypt(body)
                if (FrameCodec.isV2(plaintext)) {
                    dispatchFrame(decodeV2Frame(plaintext))
                    continue
                }
                val message = JSONObject(String(plaintext, Charsets.UTF_8))
                when (message.optString("kind")) {
                    "video" -> dispatchFrame(
                        wrapFrame(FrameCodec.decodeV1(jsonObjectToMap(message)), TARGET_FPS),
                    )
                    "pong" -> latency.onPong(message.optLong("seq", -1L), System.nanoTime())
                    "ready" -> {
                        val width = message.optInt("w", 0)
                        val height = message.optInt("h", 0)
                        val fps = message.optInt("fps", 0)
                        streamProfile = actualProfile(width, height, fps)
                        RdcLog.info(TAG, "stream_ready", "w=$width h=$height fps=$fps")
                    }
                    else -> RdcLog.debug(TAG, "data_message", "kind=${message.optString("kind")}")
                }
            }
        } catch (error: Throwable) {
            if (active) {
                lastError = "会话已断开：${error.message ?: error.javaClass.simpleName}"
                RdcLog.warn(TAG, "session_closed", "reason=${error.message}")
            }
        } finally {
            active = false
            channel = null
            dataOutput = null
        }
    }

    private fun bufferFrame(frame: VideoFrame) {
        synchronized(pendingLock) {
            if (frame.key) {
                pendingFrames.clear()
                pendingFromKeyframe = true
            }
            if (!pendingFromKeyframe) {
                return
            }
            pendingFrames.addLast(frame)
            while (pendingFrames.size > PENDING_LIMIT) {
                pendingFrames.removeFirst()
            }
        }
    }

    /** 输入上行; 未连接立即返回 false, 已连接则排队异步写, 避免主线程做网络 IO. */
    fun send(event: InputEvent): Boolean {
        val aead = channel ?: return false
        val out = dataOutput ?: return false
        if (!active) {
            return false
        }
        val body = CanonicalJson.bytes(inputPayload(event))
        inputExecutor.execute {
            try {
                val ciphertext = aead.encrypt(body)
                synchronized(sendLock) {
                    writeFrame(out, ciphertext)
                }
            } catch (error: Exception) {
                RdcLog.warn(
                    TAG,
                    "input_send_failed",
                    "reason=${error.message} type=${error.javaClass.name}",
                )
            }
        }
        return true
    }

    private fun inputPayload(event: InputEvent): Map<String, Any?> = when (event) {
        is InputEvent.Pointer -> linkedMapOf(
            "kind" to "pointer",
            "action" to event.action.name,
            "x" to event.x,
            "y" to event.y,
            "button" to event.button,
        )
        is InputEvent.Key -> linkedMapOf(
            "kind" to "key",
            "action" to event.action.name,
            "key" to event.keyCode,
        )
        is InputEvent.Gesture -> linkedMapOf(
            "kind" to "gesture",
            "gesture" to event.gesture,
            "x" to event.x,
            "y" to event.y,
            "dx" to event.dx,
            "dy" to event.dy,
        )
    }

    fun stop() {
        stopInternal(clearSink = true)
    }

    private fun stopInternal(clearSink: Boolean) {
        active = false
        channel = null
        dataOutput = null
        synchronized(pendingLock) {
            pendingFrames.clear()
            pendingFromKeyframe = false
        }
        if (clearSink) {
            videoSink = null
        }
        readerThread?.interrupt()
        readerThread = null
        pingThread?.interrupt()
        pingThread = null
        latency.reset()
        streamProfile = ""
        try {
            dataSocket?.close()
        } catch (_: Exception) {
        }
        dataSocket = null
        try {
            udpPlane?.close()
        } catch (_: Exception) {
        }
        udpPlane = null
        try {
            udpSocket?.close()
        } catch (_: Exception) {
        }
        udpSocket = null
        try {
            directSocket?.close()
        } catch (_: Exception) {
        }
        directSocket = null
        try {
            signalSocket?.close()
        } catch (_: Exception) {
        }
        signalSocket = null
        startedAt.set(0)
        RdcLog.info(TAG, "stopped", "session=${sessionId.take(8)}")
    }

    // ---------------------------------------------------------------- 帧与包络

    private fun sendEnvelope(
        output: OutputStream,
        identity: RdcIdentity,
        type: String,
        payload: Map<String, Any?>,
        sessionId: String? = null,
    ) {
        val envelope = linkedMapOf<String, Any?>(
            "v" to 1,
            "type" to type,
            "id" to UUID.randomUUID().toString(),
            "session_id" to sessionId,
            "ts" to nowIso(),
            "payload" to payload,
        )
        envelope["sig"] = b64(identity.sign(CanonicalJson.bytes(envelope)))
        sendJson(output, envelope)
    }

    private fun waitType(input: InputStream, type: String): Map<String, Any?> {
        val deadline = System.currentTimeMillis() + SIGNAL_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            val frame = readJsonMap(input) ?: throw LanFailure("连接被对端关闭：请核对连接码与设备编号")
            when (frame["type"]) {
                "error" -> throw LanFailure(describeErrorPayload(frame))
                type -> return frame
                else -> RdcLog.debug(TAG, "skip_envelope", "type=${frame["type"]}")
            }
        }
        throw LanFailure("等待 $type 超时")
    }

    private fun describeErrorPayload(frame: Map<String, Any?>): String {
        val payload = frame["payload"] as? Map<*, *> ?: emptyMap<Any, Any>()
        val code = payload["code"]?.toString().orEmpty()
        val detail = payload["message"]?.toString().orEmpty()
        return when (code) {
            "PEER_OFFLINE" -> "被控端不在线：请核对设备编号是否正确、电脑端是否已启动"
            "AUTH_FAILED" -> "认证失败：设备身份被服务端拒绝"
            // 继电器在并发上限 1 路时返回 RELAY_FULL, message 即面向用户的中文原因
            "RELAY_FULL" -> detail.ifBlank { "中继通道已满，请等待或改用直连" }
            else -> "被控端返回错误：${if (detail.isEmpty()) code else detail}"
        }
    }

    private fun sendJson(output: OutputStream, payload: Map<String, Any?>) {
        writeFrame(output, CanonicalJson.bytes(payload))
    }

    private fun writeFrame(output: OutputStream, payload: ByteArray) {
        val length = payload.size
        output.write(
            byteArrayOf(
                (length ushr 24).toByte(),
                (length ushr 16).toByte(),
                (length ushr 8).toByte(),
                length.toByte(),
            ),
        )
        output.write(payload)
        output.flush()
    }

    private fun readJsonMap(input: InputStream): Map<String, Any?>? {
        val body = readFrame(input) ?: return null
        val value = JSONObject(String(body, Charsets.UTF_8))
        @Suppress("UNCHECKED_CAST")
        return jsonObjectToMap(value)
    }

    private fun readFrame(input: InputStream): ByteArray? {
        val header = readExactly(input, 4) ?: return null
        val length = ((header[0].toInt() and 0xff) shl 24) or
            ((header[1].toInt() and 0xff) shl 16) or
            ((header[2].toInt() and 0xff) shl 8) or
            (header[3].toInt() and 0xff)
        if (length <= 0 || length > MAX_FRAME_BYTES) {
            throw LanFailure("非法帧长度：$length")
        }
        return readExactly(input, length) ?: throw EOFException("帧体被截断")
    }

    private fun readExactly(input: InputStream, size: Int): ByteArray? {
        val buffer = ByteArray(size)
        var offset = 0
        while (offset < size) {
            val read = input.read(buffer, offset, size - offset)
            if (read <= 0) {
                return null
            }
            offset += read
        }
        return buffer
    }

    private fun jsonObjectToMap(value: JSONObject): Map<String, Any?> {
        val result = linkedMapOf<String, Any?>()
        val keys = value.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            result[key] = jsonValue(value.opt(key))
        }
        return result
    }

    private fun jsonValue(value: Any?): Any? = when (value) {
        null, JSONObject.NULL -> null
        is JSONObject -> jsonObjectToMap(value)
        is JSONArray -> (0 until value.length()).map { jsonValue(value.opt(it)) }
        else -> value
    }

    private fun describeFailure(error: Throwable, stage: String, host: String, port: Int): String {
        return when (error) {
            is LanFailure -> error.message ?: "连接失败"
            is SSLException -> "安全连接失败：中继证书校验未通过或 TLS 1.3 协商失败"
            is SocketTimeoutException -> "网络不通：连接 ${formatEndpoint(host, port)} 超时，请检查网络或防火墙"
            is ConnectException -> "被控端未启动，请确认对端已打开 RDC 并处于可连接状态（端口 $port）"
            is NoRouteToHostException -> "网络不通：无法到达 ${formatHost(host)}，请检查网络与防火墙"
            is UnknownHostException -> "地址无效：${formatHost(host)}"
            is EOFException -> "设备编号或连接码错误，请核对后重试"
            else -> "$stage 失败：${error.message ?: error.javaClass.simpleName}"
        }
    }

    /** IPv6 展示时补方括号, 避免 "地址:端口" 看起来像多段冒号. */
    private fun formatEndpoint(host: String, port: Int): String =
        "${formatHost(host)}:$port"

    private fun formatHost(host: String): String =
        if (host.contains(':')) "[$host]" else host

    private fun nowIso(): String =
        SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US).format(Date())

    private const val TAG = "Lan"
    private const val CONNECT_TIMEOUT_MS = 6000
    private const val SIGNAL_TIMEOUT_MS = 20000
    private const val DATA_TIMEOUT_MS = 15000
    private const val MAX_FRAME_BYTES = 8 * 1024 * 1024
    private const val PENDING_LIMIT = 64
}
