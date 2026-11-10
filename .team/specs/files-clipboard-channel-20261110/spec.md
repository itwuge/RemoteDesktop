# 文件传输与剪贴板接入会话通道（缺口 19 / 20 收口）

状态：待批准（用户已批准路径：先做可 import 接收端，用本机真实 TCP + RelayDataPlane 跑通；host 接线由用户排期）
范围：桌面端发送引擎、跨端接收端、通道抽象、剪贴板双向、契约登记、UI 可见提示。
不动：`scripts/rdc_host_linux.py`、`scripts/android_host_agent.py`、`src/rdc/commands/media_caps.py`、帧封装与 v2 协商文件。

## 1. 现状与缺口依据

- `src/rdc/files/service.py` 的 `start_transfer` 只做本机读写：`_transfer_chunk` 从源读、写目标、回读比对，没有任何跨端命令。
- `src/rdc/clipboard/sync.py` 的 `push/pull` 只读写本进程内存 `latest`，不发送任何消息。
- 协议层已具备 9 类消息、`SessionPlaneTracker` 校验、信令降级转发；但桌面端没有数据面客户端，也没有收端落盘实现。
- 用户可见后果：进度条走完只代表本机复制完成，剪贴板显示"已同步"只代表内存赋值。缺口 19/20 的关闭依据必须落在这两点上。

## 2. 目标

1. 发送端把文件分块发到会话通道，收端真落盘；逐块 SHA-256、整文件树摘要双重校验；支持断点续传与 4 MiB 背压窗口；失败返回明确错误码，不悬挂。
2. 剪贴板经同一通道双向同步：`clipboard_update/request/ack`，带去重、开关、脱敏，未收到确认时界面显示"未同步"。
3. 通道抽象可替换：`SessionChannel` 协议 + 环路实现 + 真实 TCP 中继实现；发送/接收逻辑不依赖中继分支，后续换正式 AEAD 数据面只替换通道实现。
4. 降级通道的可见性提示：UI 常驻提示"载荷对中继可见，未达端到端加密"，日志 `rdc.session.channel / channel.degraded` 同步记录。
5. 新增命令与 `file_result`、`file_accept.resume_offsets` 登记契约变更记录；缺口 19/20 写明关闭依据与"未达端到端加密"。

## 3. 架构

```
UI (transfer/clipboard)
  └─ rdc.commands.transfer / clipboard
       └─ FileTransferService / ClipboardSyncService   ← 只依赖 SessionChannel 协议
            └─ rdc.session.channel.SessionChannel
                 ├─ LoopbackChannel      内存配对，单测/同进程
                 └─ RelayFrameChannel    真实 TCP → RelayDataPlane（4 字节长度帧 + JSON）
```

- 通道消息统一为 `{"type": str, "session_id": str, "payload": dict}`，`payload` 内仍带 `session_id`，与 `SessionPlaneTracker` 口径一致。
- 通道只搬运字节，不解析文件语义；发送端与接收端各自持有状态机。
- 可见性由 `ChannelInfo` 声明：`kind`、`encrypted`、`visible_to_relay`、`budget_kbps`、`session_id`。中继实现固定 `encrypted=False, visible_to_relay=True`；环路实现 `encrypted=False, visible_to_relay=False`（仅本机）。
- 方向与预算：RelayDataPlane 只对 `publisher→subscriber` 方向按预算丢帧；文件块从控制端（subscriber）发往被控端（publisher）时不受中继预算约束，但两端共享同一条 TCP 连接与中继出口。因此发送端**不依赖方向判断**，统一把速率上限收敛到 `max(0, min(请求限速, 通道 budget_kbps - 2500))`，并为视频保留地板档；发布方向丢帧时由 `file_ack` 缺失触发 30 秒超时失败，不静默成功。
- 跨端落盘只认收端 `file_result`：发送端 `completed` 的条件是收到 `verified=true` 且整体摘要一致；仅"发完块"不得置完成。

## 4. 精确改动

### 4.1 新增 `src/rdc/session/__init__.py` 与 `src/rdc/session/channel.py`

文件头按仓库 8 行格式（文件/所属目录/文件用途/架构定位/核心功能/职责边界/运行环境/依赖关系），中文注释。

```python
CHANNEL_BACKPRESSURE = "CHANNEL_BACKPRESSURE"
CHANNEL_TIMEOUT = "CHANNEL_TIMEOUT"
CHANNEL_UNAVAILABLE = "CHANNEL_UNAVAILABLE"
CHANNEL_AUTH_FAILED = "CHANNEL_AUTH_FAILED"
CHANNEL_CLOSED = "CHANNEL_CLOSED"

@dataclass(frozen=True)
class ChannelInfo:
    kind: str                  # relay / loopback
    session_id: str
    role: str                  # publisher / subscriber（中继语义）
    encrypted: bool
    visible_to_relay: bool
    budget_kbps: int = 0

class SessionChannelError(Exception):
    def __init__(self, code: str, message: str) -> None: ...

class SessionChannel(Protocol):
    def open(self) -> None: ...
    def send(self, msg_type: str, payload: dict[str, Any]) -> None: ...
    def receive(self, timeout: float = 0.0) -> tuple[str, dict[str, Any]] | None: ...
    def close(self) -> None: ...
    def info(self) -> ChannelInfo: ...
```

`LoopbackChannel`：

- `LoopbackChannel.pair(session_id: str, capacity: int = 64) -> tuple[LoopbackChannel, LoopbackChannel]`。
- 内部 `queue.Queue(maxsize=capacity)`；`send` 在满时抛 `SessionChannelError(CHANNEL_BACKPRESSURE, "对端接收窗口已满")`；`receive(0)` 非阻塞。
- `info()` 返回 `kind="loopback", encrypted=False, visible_to_relay=False`。

`RelayFrameChannel`：

```python
def __init__(
    self,
    host: str,
    port: int,
    relay_token: str,
    session_id: str,
    role: str,                 # publisher / subscriber
    *,
    ssl_context: ssl.SSLContext | None = None,
    read_queue: int = 512,
    write_timeout_s: float = 0.5,
) -> None: ...
```

- `open()`：TCP/TLS 连接；`send_frame(sock, {"plane":"data","v":1,"type":"relay_hello","relay_token":...,"session_id":...,"role":...})`；读一帧必须为 `relay_ready`，否则关连接并抛 `CHANNEL_AUTH_FAILED`；记录 `budget_kbps`；起守护读线程把帧放入有界队列。
- `send()`：JSON 序列化后 `sendall`，`sock.settimeout(write_timeout_s)`；超时抛 `CHANNEL_BACKPRESSURE`（由上层下轮重试，不阻塞界面线程）。
- `receive(timeout)`：从队列取，空返回 `None`；连接断开且队列空后抛 `CHANNEL_CLOSED`。
- `info()`：`kind="relay", encrypted=False, visible_to_relay=True`。
- 模块级工厂 `connect_relay_channel(...) -> RelayFrameChannel` 便于装配处一行接线。

### 4.2 新增 `src/rdc/files/digest.py`

```python
def file_digest(path: Path) -> str: ...          # 1 MiB 分块读
def tree_digest(entries: list[tuple[str, Path]]) -> str: ...  # 按 relative 排序，relative+b"\0"+内容+b"\0"
```

`src/rdc/files/service.py::_tree_digest` 改为调用 `tree_digest`，行为不变（现有测试必须全绿）。接收端复用同一实现，避免两套哈希口径。

### 4.3 新增 `src/rdc/files/receiver.py`（可 import 入口）

```python
@dataclass
class FileTransferReceiver:
    destination_root: Path
    on_complete: Callable[[dict[str, Any]], None] | None = None

    def handle(self, msg_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """返回待回发的 {"type","payload"}；无需回包返回 None。不抛异常。"""
```

状态：`self.transfers: dict[str, _Incoming]`。`_Incoming` 保存 `session_id/transfer_id/items[(relative,size,sha256)]/section/chunk_size/received{relative:int}/parts{relative:Path}/accepted/started_at`。

- `file_offer`：
  - `chunk_size` 必须 `1..256 KiB`，`digest_alg=="sha256"`，`total_bytes==sum(size)`，条目 1..4096，`sha256` 为 64 位十六进制。
  - `_safe_relative(relative)`：拒绝绝对路径、盘符、空段、`.`、`..`；任一非法→回 `file_abort`（`FILE_OFFER_REJECTED`，"相对路径非法"），不创建任何目录。
  - 对每个文件在 `<destination_root>/<relative>.part` 上取已存在大小，`min(size, part_size)` 作为续传偏移；`resume_offsets` 仅保留 >0 的项。
  - 回 `file_accept`：`{session_id, transfer_id, accepted_items: [relative...], resume_offsets: {relative: bytes}}`，顺序与 offer 一致（`file_index` 即此顺序）。
- `file_chunk`：
  - `offset` 必须等于该文件当前 `received`；否则 `file_abort`（`FILE_CHUNK_INVALID`）。
  - base64 解码长度必须等于 `length`，`sha256(data)==payload.sha256`；不符→`file_abort`（`CHUNK_CHECKSUM_MISMATCH`），保留 `.part` 供续传但记失败。
  - 以 `r+b`/`wb` 写入并 `flush()`；`received += length`；回 `file_ack {file_index, acked_bytes: received[relative]}`。
- `file_complete`：
  - 每个条目 `received == size`，否则 `file_result {verified: false, error_code: TRANSFER_IO_ERROR}`。
  - `tree_digest([(relative, part)])` 必须等于 `payload.digest`；不等→`file_result {verified:false, digest: 实际值, error_code: TRANSFER_HASH_MISMATCH}`，删除 `.part`。
  - 相等→逐个 `part.replace(final)`，清理状态，回调 `on_complete({"transfer_id","files":[relative...],"digest"})`，回 `file_result {verified:true, digest}`。
- `file_abort`：关闭并删除本次 `.part`，清理状态，不回包。
- `snapshot()`：`{"transfers": n, "received_bytes": int, "completed": int}`，不含路径与摘要。

### 4.4 新增 `src/rdc/clipboard/receiver.py`

```python
@dataclass
class ClipboardReceiver:
    on_apply: Callable[[str, str], None] | None = None   # (kind, content)
    def handle(self, msg_type: str, payload: dict[str, Any]) -> dict[str, Any] | None: ...
```

- `clipboard_update`：校验 `kind/size_bytes/content/digest`（文本 UTF-8 字节数，图片 base64 解码后长度）；摘要不符→回 `error {code: CLIPBOARD_DIGEST_MISMATCH}`；`(kind,digest)` 与内存最近一致→`clipboard_ack {applied: false}` 且不回调；否则更新内存并回调 `on_apply`，回 `clipboard_ack {applied: true}`。
- `clipboard_request`：有该 `kind` 的最新内容→回 `clipboard_update`（同一份 payload）；无→回 `error {code: CLIPBOARD_EMPTY}`。
- `set_local(kind, content)`：把本端系统剪贴板内容缓存在内存，供对端 `clipboard_request` 应答；不落盘、不写日志明文。

### 4.5 扩展 `src/rdc/files/service.py`

新增构造字段（默认值保持现有单测可显式开启本地模式）：

```python
channel: SessionChannel | None = None
allow_local_staging: bool = False
ack_timeout_s: float = 30.0
inflight_limit_bytes: int = 4 * 1024 * 1024
acked_bytes: int = 0   # 放 TransferJob
```

- `bind_channel(channel) -> CommandResult`：写入通道、记录 `channel.bound` 日志；`channel=None` 解除。
- `channel_status() -> CommandResult`：data 为 `{"bound", "kind", "session_id", "encrypted", "visible_to_relay", "budget_kbps", "mode"}`；`mode` 取 `channel / local_staging / unbound`。
- `start_transfer`：
  - 无通道且 `allow_local_staging=False` → `SESSION_CHANNEL_UNAVAILABLE`（"尚未接入会话通道，无法跨端传输"）。
  - 有通道：`channel.open()`（未打开时）；预算 `budget_kbps = max(0, min(rate_limit_kbps or ∞, channel.budget_kbps - DEFAULT_VIDEO_FLOOR_KBPS))`，≤0 时 `TRANSFER_RATE_INVALID`（"链路预算不足以保留视频地板档"）；任务 `channel="session"`、`remote=True`。
  - 断点续传由收端决定：`file_offer` 不带 `resume`；收端按自己 `.part` 的实际大小在 `file_accept.resume_offsets` 回传偏移，发送端据此对齐后只发剩余块。进程重启后重新 `start_transfer` 即走此路径，无需发送端日志。
  - 源文件摘要采用"轮询推进"：`hashing` 阶段每次 `get_transfer_progress` 最多读 `poll_budget_bytes`，同时计算逐文件 SHA-256 与按 relative 排序的整树摘要；完成后发 `file_offer`，状态 `offering`，不阻塞界面线程。
  - 无通道且显式 `allow_local_staging=True`：保留现有本机路径，快照 `channel="local_staging"`，UI 不提供该开关。
- `get_transfer_progress`：`remote` 分支调用 `_advance_channel(job)`；本机分支不动。
- `_advance_channel(job)`：
  1. 最多 drain 64 条通道消息（`receive(0)`），分发 `file_accept/file_ack/file_result/file_abort/error`；`CHANNEL_BACKPRESSURE` 立即返回，下轮重试。
  2. `offering`：收到 accept→按 `resume_offsets` 设置各 `item.sent`、`job.sent_bytes`、`job.acked_bytes`；`accepted_items` 顺序映射 `file_index`；转 `accepted`。
  3. `accepted`：在 `inflight = sent_bytes - acked_bytes < 4 MiB` 与 `TokenBucket` 预算内逐块读取、base64、`file_chunk`；`CHANNEL_BACKPRESSURE` 停止本轮。
  4. 全部 `sent==size`→发 `file_complete`（`tree_digest` 源文件），转 `completing`。
  5. `completing`：收 `file_result`；`verified=true` 且 digest 一致→`completed`；否则 `TRANSFER_HASH_MISMATCH`。
  6. 任一等待态超过 `ack_timeout_s`（用注入 `clock`）→失败 `SESSION_CHANNEL_TIMEOUT`（"对端 30 秒未确认，可能链路丢帧"），不悬挂。
  7. `file_abort` 或 `error`→按对端 `error_code` 失败，文案透传。
- `pause/resume/cancel`：remote 分支照常改状态；`cancel` 额外发 `file_abort {reason:"用户取消"}`。

### 4.6 扩展 `src/rdc/clipboard/sync.py`

- 新增字段：`channel`、`channel_pending: dict | None`、`receiver: ClipboardReceiver`、`applied_digest: str | None`。
- `bind_channel(channel)`：绑定并把 `receiver.on_apply` 指到内部 `_apply_remote`（更新 `latest`，防环回）。
- `push`：有通道时组装 `clipboard_update`（`size_bytes`、`digest=sha256(raw)`、`content`），`channel.send`；置 `channel_pending={"kind","digest","sent_at"}`；返回 `ok=True, data={"applied": False, "pending": True, "digest":...}`，文案"已发送, 等待对端确认"。
- `pull`：有通道时发 `clipboard_request {kind}`，置 pending；返回 `{"pending": True}`，不谎报成功。
- `poll_channel()`（命令层 `get_clipboard_status` 内部先调用）：drain 通道→`receiver.handle`→回包 `send`；收到 `clipboard_ack` 时按 `applied` 更新 `channel_pending`（确认/被去重）；收到 `clipboard_update` 时更新 `latest` 并回 ack。
- `status()` 追加 `mode`、`bound`、`visible_to_relay`、`pending`、`pending_kind`、`pending_digest`、`confirmed`；仍不含明文。
- 无通道时保持现有内存行为，但 `status().mode="local"`，UI 明确显示"仅本机，未跨端"。

### 4.7 命令层

- `src/rdc/commands/transfer.py` 新增：
  ```python
  def get_transfer_channel_status() -> CommandResult: ...
  ```
  data 见 4.5。`configure` 签名不变。
- `src/rdc/commands/clipboard.py`：`get_clipboard_status` 不变签名，内部触发 `poll_channel()`；`configure` 签名不变。
- 不新增用户可调用的"绑定通道"命令；绑通道由装配处调用 `transfer.get_service().bind_channel(...)` / `clipboard.get_service().bind_channel(...)`，与 `validate_preview_token` 同属内部接入 API，仅登记变更记录不占命令编号。

### 4.8 协议与服务端校验（非禁改文件）

- `src/rdc/signaling/messages.py`：`MESSAGE_TYPES` 增加 `file_result`（32 类）；`REQUIRED_PAYLOAD["file_result"] = ("session_id","transfer_id","verified","digest")`；`SESSION_PLANE_TYPES` 增加 `file_result`；文件头与 docstring 的"31 类"改"32 类"。
- `src/rdc/signaling/server.py`：新增 `_handle_file_result` 一行转发到 `_session_plane`（与其余 9 类同形）。
- `src/rdc/signaling/session_plane.py`：
  - `_check_file_accept` 读取可选 `resume_offsets`（dict，relative 必须在 items 内，`0<=v<=size`），初始化 `state.acked[index]=v`、`state.sent[index]=v`；非法→`FILE_OFFER_REJECTED`。
  - 新增 `_check_file_result`：transfers 必须存在且 `state=="completed"`；`verified` 必须为布尔；`digest` 为 64 位十六进制；可选 `error_code` 为非空字符串；通过后不改状态。
- `file_result` 语义（写入契约）：
  - `verified=true`：`digest` 必须等于发送端 `file_complete.digest`，否则发送端判 `TRANSFER_HASH_MISMATCH`；
  - `verified=false`：必须带 `error_code`（`TRANSFER_HASH_MISMATCH`、`TRANSFER_IO_ERROR`、`TRANSFER_PERMISSION_DENIED`），发送端原样透传到任务 `error_code`；
  - 收端不得在 `verified=true` 前删除 `.part`。
- `tests/test_protocol.py`：31→32，docstring 同步。
- 服务端降级通道仍会把明文载荷转发给对端，因此 `file_result` 同属"对中继可见"，不因新增字段改变加密状态。

## 5. 契约登记

新增 `.team/records/契约变更记录-契约-202611-014-20261110.md`：

- 类型：非破坏性增量（新增消息类型、可选字段、命令、错误码）。
- `protocol-v1` 升 v1.5：`file_result` 行、`file_accept.resume_offsets` 可选字段、3.1 顺序补 `file_complete → file_result`、版本段落追加；注明"降级通道未使用会话密钥，载荷对中继可见，仅限过渡"。
- `desktop-v1` 升 v1.8：第 50 条 `get_transfer_channel_status`；3.5 增补"无通道不得静默本机复制"；新增错误码 `SESSION_CHANNEL_UNAVAILABLE`、`SESSION_CHANNEL_TIMEOUT`、`CHANNEL_BACKPRESSURE`；剪贴板状态新增只读字段。
- 缺口 19 关闭依据：`ClipboardReceiver` + `ClipboardSyncService` 通道模式已实现双向 `clipboard_update/request/ack`、去重、开关、脱敏、未确认显示"未同步"；验收命令与截图留证。
- 缺口 20 关闭依据：`FileTransferReceiver` 真落盘 + 逐块/整体校验 + 续传 + 背压；本机真实 TCP + `RelayDataPlane` 端到端实测通过；明写"未达端到端加密（信令降级通道，中继可见载荷）"。

## 6. UI 改动

- `src/rdc/ui/pages/transfer.py`：表单上方常驻状态条；`get_transfer_channel_status()`：
  - `unbound`：琥珀色"未接入会话通道，当前无法跨端传输"，开始按钮禁用并给 tooltip。
  - `channel` 且 `visible_to_relay`：琥珀色"当前走信令降级通道：文件内容与剪贴板对中继可见，未达端到端加密"。
  - `local_staging`：红色"本机暂存模式，文件未离开本机"。
- `src/rdc/ui/pages/clipboard_page.py`：同上提示条；`pending=True` 时状态 chip 显示"未同步（等待对端确认）"，`confirmed=True` 才显示"已同步"；`mode=local` 显示"仅本机"。
- `src/rdc/ui/pages/session.py` 若引用传输/剪贴板状态，同步读取新字段（仅展示，不改交互）。

## 7. 测试与验收

新增：

1. `tests/test_session_channel.py`：环路收发、容量上限背压；真实 TCP + `RelayDataPlane` 双端握手与一次 offer/accept 往返。
2. `tests/test_file_channel_transfer.py`：
   - 2 MiB 文件端到端：对端文件逐字节一致、`state=completed`、`digest` 一致、`sent_bytes` 等于大小；
   - 块摘要篡改→双方明确失败（`CHUNK_CHECKSUM_MISMATCH`），不悬挂；
   - 续传：预置 `.part` 前 N 字节，offer 带 resume，accept 返回 `resume_offsets`，实际重传字节 < 总量，最终哈希一致；
   - 背压：`poll_budget` 大于窗口时在途不超过 4 MiB；
   - 对端不 ack→`SESSION_CHANNEL_TIMEOUT`；
   - 无通道→`SESSION_CHANNEL_UNAVAILABLE`；路径穿越 `../evil`→中止且不落盘到根目录外。
3. `tests/test_clipboard_channel.py`：双向 push→ack、重复 digest 去重、pull→update、关闭开关拒绝、未确认时 `pending=True`。
4. `tests/test_transfer_clipboard_pages.py` 增：无通道提示条与按钮禁用；有通道且 `visible_to_relay` 时提示含"未达端到端加密"；pending 显示"未同步"。
5. 现有 `tests/test_files_transfer.py` 的构造统一加 `allow_local_staging=True`（本机分支回归不改语义）；`test_clipboard_sync.py` 不变。

### 7.1 跨进程实测装置（必须真跨进程）

新增 `scripts/verify_file_channel_e2e.py`，三个 OS 进程 + 本机真实 TCP：

```powershell
python scripts/verify_file_channel_e2e.py --file <测试文件> --dest <接收目录>
```

- `--role relay`：先占端口，启动 `RelayDataPlane(resolve_grant=固定 RelayGrant(budget_kbps=10000), max_sessions=4)`；accept 后读 4 字节长度帧与 `relay_hello`，`plane.attach(conn, hello)`。
- `--role receiver`：`RelayFrameChannel(role="publisher")` + `FileTransferReceiver(dest)`；循环 `receive(0.2)` → `handle` → 回包；结束后写 JSON 结果（路径、字节、摘要、是否全部落盘）。
- `--role sender`：`RelayFrameChannel(role="subscriber")` + `FileTransferService(allow_local_staging=False)`；`bind_channel` 后 `start_transfer`，轮询到终态；写 JSON（大小、耗时、速率、`digest`、状态、错误码）。
- 编排模式（默认）拉起三进程，校验两端摘要一致、收端文件逐字节一致、发送端 `completed`；失败非零退出。
- 产物：`evidence/file-channel-<时间戳>/sender.json`、`receiver.json`、`relay.log`、`summary.md`。

实测证据（首个文件跑通即回报）：

- 文件大小、耗时、速率、`digest`、收端落盘路径与字节比对、`RelayDataPlane` 转发帧数；
- 报告模板：`docs/验收/文件传输与剪贴板会话通道实测-2026-11-10.md`，含命令、输出摘要与时间戳。

门禁：`python scripts/gate.py` 6/6；行宽 120、LF、无 BOM、中文文件头。

## 7.2 实施顺序（用户明确要求）

1. 先做 UI "降级通道 / 对中继可见 / 未达端到端加密" 提示条与未接入禁用，不依赖通道已接好；此时 `channel_status()` 返回 `unbound` 也能显示真话。
2. 再做通道与文件端到端。
3. 最后做剪贴板与契约登记。

## 7.3 与并行线的冲突面

- `src/rdc/signaling/server.py` 另一条线将加入 host 上报 wall_id→account 绑定消息。本轮只在该文件追加 `_handle_file_result` 一个方法（紧邻 `_handle_file_abort`），不重排、不改其它函数；若合并冲突，在回报中列明冲突位置与我的两行改动。
- 禁改文件（`scripts/rdc_host_linux.py`、`scripts/android_host_agent.py`、`src/rdc/commands/media_caps.py`、帧封装与 v2 协商）零改动；host 接线只出清单。

## 8. host 接线最小改动清单（不代改，交付给排期）

1. `scripts/rdc_host_linux.py::_on_allocated`（约 475 行的 `relay_allocated` 分支）：在写入 `relay_endpoint` 后追加 6 行：构造 `RelayFrameChannel(host, port, relay_token, session_id, role="publisher")`，`open()`，`commands_transfer.get_service().bind_channel(channel)` 与 `commands_clipboard.get_service().bind_channel(channel)`；异常时 `_emit("[状态] 数据通道未建立: ...")` 并继续（不阻断画面）。
2. 同文件新增模块级函数 `_pump_session_plane(channel, stop_event)`（约 22 行）：`while not stop_event.is_set()` → `msg = channel.receive(0.2)` → `receiver.handle(msg_type, payload)` → `reply and channel.send(reply["type"], reply["payload"])`；`FileTransferReceiver(Path(destination))` 与 `ClipboardReceiver(on_apply=...)` 在会话建立处构造一次。
3. 同文件会话建立点（`_on_offer` 接受分支之后）：`threading.Thread(target=_pump_session_plane, args=(channel, self.stop_event), daemon=True).start()`；停止会话时 `channel.close()`。
4. 若 host 需要接收文件：接收目录取 `commands_transfer.default_destination()`；仅在用户已配置目录时启用，避免默认写入临时目录。
5. 剪贴板系统读写不在本轮 host 改动内；`ClipboardReceiver.on_apply` 先接日志（"已接收剪贴板文本 N 字节"），系统剪贴板写入由 host 线后续补。

以上合计 3 处插入、约 30 行；不改变 host 既有画面/输入逻辑。

## 9. 非目标

- 不实现正式数据面会话密钥（SPAKE2/AEAD）；不宣称已端到端加密。
- 不做图片剪贴板的系统级读写；不做多文件并发多 transfer。
- 不改安卓 host agent；不改帧封装、v2 协商、`media_caps`。

## 10. 风险与回滚

- 风险：中继发布方向按预算丢帧会造成丢失块，因此发送端必须限速在预算内并设 ack 超时；超时明确失败，用户可重试续传。
- 风险：`file_result` 属新增消息，旧 peer 不识别会回 `error`；发送端在 `completing` 收到 `error` 时失败并提示"对端版本不支持传输确认"，不做静默成功。
- 回滚：新增文件可整体移除；契约增量按变更记录撤条目；`FileTransferService` 保留 `allow_local_staging` 仅用于测试与本机暂存，不对外暴露。
