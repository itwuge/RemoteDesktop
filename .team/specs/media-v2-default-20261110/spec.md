# Spec · 视频帧 v2 转为默认路径 + 低码率控制开销收敛 + 中继 1080p30 实测

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-10 |
| 上游 | 上一轮「帧封装 v2」已交付（`src/rdc/media/frame.py`、`tests/test_video_frame_v2.py`、`docs/验收/视频帧封装v2实测报告-2026-11-10.md`） |
| 约束 | 不改 `docs/contracts` 正文（增量只登记到 `.team/records`）；不碰 `src/rdc/connect/`；不碰 `android/`；门禁 6/6；不使用 `# noqa` |
| 验收口 | 默认协商结果、低码率改后 wire/media、中继 3000 kbps 下 1080p30 三场景（线上码率 / SSIM / 丢帧） |

## 1. 现状（已勘察结论）

产品路径当前仍是 v1 JSON+base64，且没有任何帧版本协商：

| 位置 | 现状 | 缺口 |
|---|---|---|
| [rdc_host_linux.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/scripts/rdc_host_linux.py) `VideoSender.run`（L151-164） | `channel.send({"kind":"video","data":b64encode(...)})` | 不发 v2；`_on_offer`（L450）完全不读对端 `media_caps`；`_answer`（L476）的 `media_caps` 无 `frame_versions` |
| [rdc_control_app.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/scripts/rdc_control_app.py) `_connect`（L234-250） | offer 的 `media_caps` 只有 `codecs/max_*` | 未声明 `frame_versions`；`_receive_loop`（L297）用 `channel.recv()` 只解析 JSON；`_on_video`（L310）强制 base64 |
| `src/rdc/commands/` | `host_ports.py` 只装配采集/编码/输入；`service.py` 无媒体能力 | 命令层没有「默认帧版本」的唯一出处 |
| [relay_public_bench.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/scripts/relay_public_bench.py) L1144-1167 / L1002 | ping 固定 20 次/秒；ping 带 `sent_ns`、pong 带 `recv_ns`，控制端都不用 | 8 秒约 11.7 kB 控制开销，在 200 kbps 媒体上占 5.3%–5.8%，把 720p10 的 wire/media 顶到 1.076–1.083 |
| `android/` | 安卓线正在把握手升到 `hs=2` | 缺 v2 帧格式与版本号书面口径 |

关键口径（沿用上一轮，保证可比）：wire/media = 控制端数据面真实入站字节 ÷ 收到媒体净荷字节，含控制帧与 AEAD 握手。

## 2. 方案

### 2.1 任务 1：产品路径默认 v2（host / controller / 命令层）

新增命令层单一出处 `src/rdc/commands/media_caps.py`：

- `DEFAULT_FRAME_VERSIONS = (VERSION_V2, VERSION_V1)`：本端优先级，v2 在前。
- `default_media_caps() -> dict[str, Any]`：`{"codecs":["h264"],"max_width":1920,"max_height":1080,"max_fps":30,"frame_versions":[2,1]}`。
- `negotiate_media(peer_caps, *, role: str) -> int`：调用 `rdc.media.frame.negotiate_frame_version(peer_caps, local_versions=DEFAULT_FRAME_VERSIONS)`；结果为 v1 时用 `rdc.core.logging.log_event("rdc.media.caps", "frame_version.degraded", level="WARNING", role=..., peer_frame_versions=...)` 记明确降级原因（对端未声明 / 只声明 v1 / 未知版本），**不静默回退**。

`scripts/rdc_host_linux.py`：

- `LinuxHostAgent._on_offer` 读 `payload["media_caps"]`，调 `negotiate_media(..., role="host")`，把结果存到 session 参数；降级时 `_emit` 一行中文原因（同时已写结构化日志）。
- `_answer` 的 `media_caps` 改为 `default_media_caps()`。
- `HostSession` 接收 `frame_version`；`VideoSender` 在 `frame_version == VERSION_V2` 时用 `encode_v2(VideoFrame(...))` + `channel.send_bytes`，否则保持 v1 分支（仅兼容，不再作为默认）。

`scripts/rdc_control_app.py`：

- offer 的 `media_caps` 改为 `default_media_caps()`。
- 收到 answer 后 `negotiate_media(payload.get("media_caps"), role="controller")`，把版本存到 `ConnectWorker`；降级时状态栏/日志明确提示（`status_changed` + `LOGGER.warning`）。
- `_receive_loop` 改用 `channel.recv_raw()`：前 2 字节是 `RF` 时 `decode_v2`，否则按 JSON 控制帧解析；`_on_video` 接受 `VideoFrame`，v1 分支仍保留（走 `decode_v1`）。

线程安全：`scripts/e2e_common.py` 的 `AeadChannel` 增加一个发送锁，覆盖 `send`/`send_bytes`，避免视频线程与控制帧并发写同一 socket 时把长度前缀写乱（现状 v1 已存在该风险）。

### 2.2 任务 2：低码率端到端 ≤ 1.05（控制类消息合并/降频 + 更短消息）

只改验收装置 `scripts/relay_public_bench.py`，产品代码的控制消息不变：

1. **更短消息头**：ping 改 `{"kind":"ping","seq":n}`，pong 改 `{"kind":"pong","seq":n}`；删掉双方都不读的 `sent_ns` / `recv_ns`（控制端本来就用本地 `perf_counter_ns()` 计时）。
2. **按数据流自动降频**：新增 `RTT_ACTIVE_INTERVAL_S = 0.5`（有媒体在流）与 `RTT_IDLE_INTERVAL_S = 1.0`（1 秒内无媒体帧）；删掉固定 `RTT_INTERVAL_S = 0.05`。理由：媒体帧本身已经证明链路活跃，20 次/秒的心跳式探测在低码率下纯属浪费；活跃期 2 Hz、空闲期 1 Hz 足够采样 RTT。
3. **与心跳合并**：数据面不再维护独立心跳，RTT 探测即保活；信令侧 10 秒 `heartbeat` 在 `rdc_host_linux.py` / `rdc_control_app.py` 中保持不变，不属于数据面带宽。

预估（8 秒、200 kbps 媒体 = 200 kB）：控制帧 11.7 kB → 约 1.5–2 kB，wire/media 从 1.076–1.083 降到约 1.030–1.035。

若实测仍未达 1.05，按「改不动就说明为什么」如实给出分解（帧封装固定 60 B/帧、AEAD 握手固定开销、剩余控制帧），不得用调口径的方式凑数。

### 2.3 任务 3：Android 对接说明（只写文档，不改 `android/`）

新增 `docs/验收/安卓-v2帧格式对接说明-2026-11-10.md`，给出：

- 28 字节定长头字段表（偏移/长度/类型/含义）、`magic="RF"`、`version=2`、flags 位定义、CRC32 可选语义；
- 原始 H.264 净荷、无 base64、无 JSON；单帧上限 8 MiB；
- 分流规则：明文前 2 字节等于 `RF` → v2，否则按 v1 JSON 解析；
- 协商：`media_caps.frame_versions` 数组（优先级从高到低），未声明或未知版本降级 v1；与 `hs=2` 握手的衔接顺序（握手完成后再协商帧版本，二者独立）；
- 12 个错误码语义与建议处理（丢弃该帧 / 断链）；
- 逐字节样例（可用 `encode_v2` 生成的十六进制头）。

### 2.4 任务 4：中继 3000 kbps 预算下 1080p30 三场景

用本地回环中继 `_start_local_relay(work, egress_mbps=3.0)`（`overhead_ratio=0`、`max_sessions=1` → 预算恰为 3000 kbps），v2，静态/文本/视频三场景（`--contents static,text,video`），1080p30，编码目标码率 2.8 Mbps（给封装与控制帧留 ~200 kbps）。

```powershell
python scripts/relay_public_bench.py quality --work evidence/relay-3mbps-1080p30-v2 `
  --width 1920 --height 1080 --fps 30 --contents static,text,video `
  --bitrates 2800000 --frame-versions 2 --duration 10 --egress-mbps 3
```

输出：线上码率（`wire_bitrate_kbps`）、媒体码率、SSIM（`ssim_mean` / `ssim_text_mean`）、丢帧（`loss_ratio_estimate`、`missing_seq_estimate`、`frames_decoded` vs `packets_received`）。与既有 `evidence/quality-3mbps-1080p30`（v1、12000 kbps 预算）及 720p30 档对照，给出「中继档能否从 720p30 升到 1080p30」的判断。

## 3. 交付物

1. 代码：`src/rdc/commands/media_caps.py`（新增）、`scripts/rdc_host_linux.py`、`scripts/rdc_control_app.py`、`scripts/e2e_common.py`（发送锁）、`scripts/relay_public_bench.py`（控制帧降频/缩短）。
2. 单测：`tests/test_media_caps.py`（新增）覆盖默认声明含 v2、对端只支持 v1 → 降级 v1 且返回可判定原因、对端未声明 → v1、对端声明 v2 → v2、未知版本 → v1。
3. 文档：`docs/验收/安卓-v2帧格式对接说明-2026-11-10.md`、更新 `docs/验收/视频帧封装v2实测报告-2026-11-10.md`（新增第 11–13 节：默认协商、控制开销前后、1080p30 中继三场景）。
4. 契约增量登记：追加到 `.team/records/` 新记录（默认协商策略 + 控制面降频属实现约定，不改正文）。
5. 证据：`evidence/frame-v2-720p10-ctrl/`、`evidence/relay-3mbps-1080p30-v2/`、门禁输出。

## 4. 验证

| 步骤 | 命令 | 判据 |
|---|---|---|
| 单测 | `python -m pytest tests/test_media_caps.py tests/test_video_frame_v2.py -q` | 全过 |
| 低码率复测 | `python scripts/relay_public_bench.py quality --work evidence/frame-v2-720p10-ctrl --width 1280 --height 720 --fps 10 --contents static,text --bitrates 200000 --frame-versions 2 --duration 8` | wire/media ≤ 1.05 |
| 中继 1080p30 | 见 2.4 | 三场景均有 wire 码率、SSIM、丢帧 |
| 门禁 | `python scripts/gate.py` | 6/6 |

## 5. 风险与边界

- 产品两条入口脚本仍各自内置 v1 分支：只为兼容旧对端，默认永远先协商 v2；降级必留日志，不静默。
- 控制帧降频只作用于验收装置的数据面探测，产品数据面本来就没有 ping/pong；不影响线上行为。
- 不改 `src/rdc/connect/relay.py` 里硬编码的 `media_caps`（禁改），该字段是候选交换阶段的能力声明，帧版本以 host/controller 的 offer/answer 为准；此缺口在契约记录中登记。
