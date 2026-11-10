# 信令与中继线协议契约 v1

| 字段 | 内容 |
|---|---|
| 契约名称 | 信令与中继线协议 |
| 契约版本 | v1.6 |
| 对应设计书 | 项目设计书-项目-202611-001-20261110 |
| 冻结日期 | 2026-11-10 |
| 批准人 | M03 架构师 |
| 调用方 | 桌面客户端、Android 薄壳、信令服务、中继服务 |
| 依据规范 | .team/specs/14_交互契约.md 第 2 节通用原则 |

> 本文件只写线协议正文，不写实现代码。消息字段、方向、错误语义以本文件为准；设计书 5.3 只列清单，不替代本文件。

## 一、传输与编码

- 信令：WSS 加 TLS 1.3，默认 443 端口由配置注入，不写死。
- 中继数据面：TLS 1.3 加端到端 AEAD，中继只转发密文。
- 编码：UTF-8 JSON；时间统一 ISO 8601 带时区偏移，例如 `2026-11-10T10:00:00+08:00`。
- 协议版本：顶层 `v` 字段，当前为 1；破坏性变更升 2，非破坏性新增可选字段不升版本。

## 二、消息包络

七字段固定，缺任一即不合法：

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| v | 整数 | 是 | 当前为 1 |
| type | 字符串 | 是 | 见第三节消息类型 |
| id | 字符串 | 是 | UUIDv4，消息唯一标识；重试复用同一 id |
| session_id | 字符串 | 否 | 会话相关消息必填 |
| ts | 字符串 | 是 | ISO 8601 带时区，服务端校验 5 分钟窗口 |
| payload | 对象 | 是 | 类型相关字段 |
| sig | 字符串 | 是 | Ed25519 签名，Base64；签名对象为不含 sig 的规范 JSON |

签名口径：

- 规范 JSON：UTF-8、键名按字典序、无多余空白、不转义非必要字符。
- 签名密钥：设备 Ed25519 私钥；服务端用注册公钥验签。
- 防重放：同一 `id` 在 5 分钟内只接受一次；`ts` 超出窗口拒绝；`session_id` 与令牌绑定。

示例：

```json
{
  "v": 1,
  "type": "offer",
  "id": "6f1c2a9e-3b7d-4c1a-9e2f-0a1b2c3d4e5f",
  "session_id": "8a7b6c5d-4e3f-2a1b-9c8d-7e6f5a4b3c2d",
  "ts": "2026-11-10T10:00:00+08:00",
  "payload": {},
  "sig": "base64-ed25519-signature"
}
```

## 三、消息类型

| 类型 | 方向 | 必填 payload 字段 | 可选字段 | 说明 |
|---|---|---|---|---|
| register | 客户端 → 信令服务 | device_id、public_key、capabilities、protocol_version | display_name | 注册设备公钥与能力；device_id 由公钥摘要生成 |
| challenge | 信令服务 → 客户端 | nonce、expires_at | 无 | 一次性挑战，nonce 至少 16 字节随机 |
| auth | 客户端 → 信令服务 | device_id、signature | 无 | 对 nonce 签名，证明持有私钥 |
| auth_ok | 信令服务 → 客户端 | token、expires_at、relay_endpoint | 无 | 短期令牌与中继端点；令牌不进日志 |
| offer | 控制端 → 信令服务 → 被控端 | session_id、candidates、media_caps、access_proof | 无 | 发起会话，携带候选与媒体能力 |
| answer | 被控端 → 信令服务 → 控制端 | session_id、chosen_candidate、media_caps | 无 | 接受会话，选择候选与媒体能力 |
| candidate | 双向经信令服务 | session_id、kind、address、port、priority | 无 | 增量候选；kind 取 host、srflx、relay |
| relay_allocate | 请求方 → 信令服务 | session_id、requested_kbps、est_duration_s | 无 | 申请中继预算 |
| relay_allocated | 信令服务 → 请求方 | session_id、relay_token、endpoint、budget_kbps、quality_profile | 无 | 中继分配结果；预算由 RelayPolicy 计算 |
| session_start | 双向经信令服务 | session_id、transport_kind、quality_profile | 无 | 通知会话开始 |
| session_end | 双向经信令服务 | session_id、reason | 无 | 通知会话结束；reason 为用户可读文案 |
| display_update | 被控端 → 信令服务 → 控制端 | session_id、width、height、rotation | 无 | 方向与画面尺寸同步；rotation 取 0、90、180、270，表示远端画面顺时针旋转角度；控制端按此计算 Surface 重建与坐标映射 |
| heartbeat | 双向 | session_id、seq | 无 | 保活与 RTT 采样；seq 单调递增 |
| presence | 客户端 → 信令服务 | device_id、state | last_seen_at | 设备级在线心跳；30 秒一条，90 秒未收到判离线；只用于在线状态，不参与鉴权 |
| file_offer | 发送端 → 接收端（会话数据面） | session_id、transfer_id、items、total_bytes、chunk_size、digest_alg、digest | resume | 发起文件传输；items 每项含 relative、size、sha256；digest 为整树摘要；单次最多 4096 项，单文件不超过 4 GiB |
| file_accept | 接收端 → 发送端（会话数据面） | session_id、transfer_id、accepted_items | resume | 接受传输；accepted_items 为 relative 列表；resume 为续传偏移列表，每项含 relative、received_bytes，由接收端按本地未完成分块实际长度给出 |
| file_chunk | 发送端 → 接收端（会话数据面） | session_id、transfer_id、file_index、offset、length、sha256、data | 无 | 分块数据；单块不超过 256 KiB；offset 必须等于该文件已确认字节；data 必须密文 |
| file_ack | 接收端 → 发送端（会话数据面） | session_id、transfer_id、file_index、acked_bytes | sha256 | 累计确认；acked_bytes 只增不减，可一次确认多块 |
| file_complete | 发送端 → 接收端（会话数据面） | session_id、transfer_id、digest | 无 | 全部块确认后发送整体摘要 |
| file_abort | 双向（会话数据面） | session_id、transfer_id、reason | 无 | 任一方中止；reason 为用户可读文案 |
| file_result | 接收端 → 发送端（会话数据面） | session_id、transfer_id、verified、digest | error_code、error_message | 整文件校验回执；verified 为真表示收端已落盘且整树摘要一致；为假必须带 error_code |
| clipboard_update | 双向（会话数据面） | session_id、kind、size_bytes、digest、content | encoding | 剪贴板内容；kind 取 text、image；text 用 UTF-8 |
| clipboard_request | 接收端 → 来源端（会话数据面） | session_id、kind | 无 | 请求当前剪贴板内容 |
| clipboard_ack | 来源端 → 请求端（会话数据面） | session_id、kind、digest、applied | 无 | applied 为假表示去重或环回抑制，不再回写 |
| wall_focus | 控制端 → 信令服务 | wall_id、cell | 无 | 申请聚焦名额；全局同一时刻只允许一块墙聚焦，占用时返回 `WALL_BUDGET_EXCEEDED` |
| wall_focus_granted | 信令服务 → 控制端 | wall_id、cell、focus_token、expires_at | budget_kbps、quality_profile | 聚焦签发结果；focus_token 同时用于该路数据面接入，有效期不超过 300 秒 |
| wall_unfocus | 控制端 → 信令服务 | wall_id | 无 | 退出聚焦；幂等，未持有时也返回成功 |
| wall_unfocused | 信令服务 → 控制端 | wall_id、released | 无 | released 为假表示该墙原本未持有聚焦名额 |
| wall_preview | 控制端 → 信令服务 | wall_id、cell | 无 | 申请预览令牌；同一墙同一格同一账号重新申请即吊销旧令牌 |
| wall_preview_granted | 信令服务 → 控制端 | wall_id、cell、preview_token、expires_at | 无 | 预览令牌 60 秒有效，绑定墙、格、账号与设备，scope 固定 view |
| wall_preview_revoked | 信令服务 → 控制端 | wall_id、cell、reason | 无 | 墙关闭、重新签发或令牌失效时推送；收到后必须停止使用旧令牌 |
| wall_bind | 被控端 → 信令服务 | wall_id、session_id | 无 | 登记屏幕墙归属；必须已认证、验签通过，且发送方是该会话的被控端 |
| wall_bound | 信令服务 → 被控端 | wall_id、account_id、bound_at | 无 | 登记结果；同墙同账号重复上报幂等，同墙换账号返回 `WALL_OWNERSHIP_CONFLICT` |
| error | 双向 | code、message | 无 | 统一错误；不得带异常类名、堆栈、路径 |

### 3.1 文件传输与剪贴板顺序语义（v1.3 追加，v1.6 修订）

顺序：

1. `file_offer` → `file_accept` →（`file_chunk` → `file_ack`）* → `file_complete` → `file_result`；`file_abort` 可在任意阶段出现并立即终止。
2. `file_complete` 或 `file_abort` 之后不得再发 `file_chunk`；收到即丢弃并回 `FILE_CHUNK_INVALID`。
3. `file_chunk` 的 `offset` 必须等于该文件已确认字节；重复块幂等处理，`file_ack` 的 `acked_bytes` 不得回退。
4. 发送端只有收到 `file_result` 且 `verified=true`、`digest` 与自身一致时才可标记完成；在此之前界面必须显示「等待对端校验」，不得把已发送当作已到达。

大小上限：

- 单块 payload ≤ 256 KiB；未确认在途 ≤ 4 MiB（16 块）；单文件 ≤ 4 GiB；单次 `file_offer` 条目 ≤ 4096。
- 剪贴板 `text` ≤ 256 KiB（超限截断并在界面提示）；`image` ≤ 4 MiB（超限拒绝，不截断）。

校验与授权：

- `sha256` 为块内容摘要；`digest` 为按 relative 排序后逐文件内容摘要拼接再哈希的结果；不一致必须中止并返回 `TRANSFER_HASH_MISMATCH`。
- 发起前必须完成会话鉴权且授权范围覆盖 `control`；`view` 一律拒绝并发 `SCOPE_INSUFFICIENT`。
- `file_chunk` 与 `clipboard_update` 必须使用会话密钥加密；内容、摘要不得写入日志。
- 传输走低优先级通道并受限速约束，不得挤占画面与输入通道。

剪贴板语义：

- `clipboard_update` 为最新覆盖（last-writer-wins）；同一端重复收到相同 `digest` 不得回写，回 `clipboard_ack` 且 `applied=false`。
- `clipboard_request` 可重复发送，回复必须带对应 `digest`；`kind` 不支持时回 `CLIPBOARD_UNSUPPORTED_KIND`。
- 摘录与摘要不一致时丢弃内容并回 `CLIPBOARD_DIGEST_MISMATCH`。

### 3.2 屏幕墙聚焦与预览消息语义（v1.4 追加）

- 只有控制端可发送 `wall_focus`、`wall_unfocus`、`wall_preview`；身份取自已认证连接，不得由 payload 自报账号。
- 聚焦：先 `wall_focus` 后 `wall_focus_granted`；未持有名额时 `wall_unfocus` 也返回成功且 `released=false`；全局只允许一块墙聚焦，占用时返回 `WALL_BUDGET_EXCEEDED`，客户端应提示先退出原聚焦。
- 预览：先 `wall_preview` 后 `wall_preview_granted`，再以 `preview_token` 接入数据面；令牌 60 秒有效、非一次性、不得跨格跨墙跨账号复用。
- 失效：服务端关闭墙、重新签发同格令牌或主动吊销时推送 `wall_preview_revoked`；未收到推送但令牌过期的，按 `PREVIEW_TOKEN_INVALID` 处理并重新申请。
- 令牌只出现在 `wall_focus_granted` 与 `wall_preview_granted` 的 payload 中，不得写入日志、审计详情或界面文案。
- 消息与命令层对应关系：`wall_focus`/`wall_unfocus` 对应第 30、45 条，`wall_preview` 对应第 47 条；服务端只签发凭证，不代替命令层的预算与格号校验。

### 3.3 屏幕墙归属登记消息语义（v1.5 追加）

- `wall_bind` 只能由被控端（host）发送，用于把 `wall_id` 归属到该设备所绑定的账号；服务端回 `wall_bound`。
- 身份与授权：必须使用已认证连接并携带 A1 设备公钥可验证的包络签名；账号由服务端按 `device_id` 从账号库解析，不得由 payload 自报；发送方必须是 `session_id` 对应会话的参与者且为该会话确认过的被控端（`answer` 发送方）。任一条件不满足返回 `AUTH_FAILED`，不得写入归属。
- 幂等：同一 `wall_id` 由同一账号重复上报返回成功，`bound_at` 保持首次绑定时间不变。
- 冲突：同一 `wall_id` 已归属其它账号时返回 `WALL_OWNERSHIP_CONFLICT`，保留原绑定，不静默覆盖。
- 持久化：归属写入服务端账号库（`accounts.db` 的 `walls` 表），服务重启后仍生效；成功与拒绝都写一条审计事件，冲突拒绝事后可查。
- 未登记归属的墙被视为不存在：`wall_preview`、`wall_focus` 返回 `SCOPE_INSUFFICIENT`，不得因注册设备即可操作任意墙。

### 3.4 文件回执、续传与降级通道语义（v1.6 追加）

- 整文件校验：接收端在 `file_complete` 后按 relative 排序重算整树摘要；一致才回 `file_result(verified=true)` 并把 `.part` 落为正式文件，不一致清理临时文件并回 `verified=false` 与 `TRANSFER_HASH_MISMATCH`。
- 逐块校验：`file_chunk` 的 `sha256` 与 `data` 不符回 `CHUNK_CHECKSUM_MISMATCH` 并中止该传输；不得把无效块计入 `acked_bytes`。
- 续传：接收端按本地 `.part` 实际长度在 `file_accept.resume` 回报偏移，发送端从该偏移起传；发送端不得自行假设偏移，也不得重发已确认前缀。
- 背压：未确认在途超过 4 MiB 时发送端必须停发等待 `file_ack`，不得阻塞媒体与输入通道；单次等待超过实现对端确认超时（默认 30 秒）按 `SESSION_CHANNEL_TIMEOUT` 失败并给出原因。
- 通道可替换：文件与剪贴板只依赖会话通道抽象；信令降级通道与后续正式数据面会话密钥可互换，传输逻辑不得写死在中继分支。
- 降级可见性：经信令降级通道转发时，`file_chunk`、`clipboard_update` 的载荷对中继可见，**未达端到端加密**；界面与日志必须给出可感知提示，缺口关闭依据必须明写该限制，敏感内容不得在未加密通道传输。

## 四、设备鉴权流程

1. 首次启动生成 Ed25519 密钥对，私钥写入系统密钥库；`device_id` 取公钥 SHA-256 的 Base32 前 20 字符。
2. 客户端发送 `register`，只上传公钥、能力与协议版本；服务端校验 `device_id` 与公钥一致后保存。
3. 服务端下发 `challenge`；客户端对 nonce 与 `device_id` 签名，发送 `auth`。
4. 服务端验签通过后签发短期 `token` 与 `relay_endpoint`；令牌默认 5 分钟有效。
5. 控制端发起 `offer`，被控端回 `answer`；双方在信令通道上交换 SPAKE2 消息，服务端只转发不透明数据。
6. 口令认证成功后，双方各自派生会话密钥并发送 `session_start`；服务端不持有会话密钥。

口令规则：

- 临时连接：被控端显示 6 位一次性口令，5 分钟有效，失败 5 次锁定 15 分钟。
- 固定连接：被控端保存 Argon2id 散列，双方用 SPAKE2 认证，口令不经网络传输。
- 任何消息与日志不得出现口令原文、令牌原文与私钥。

presence 规则：

- 客户端在 `auth_ok` 后立即发送一条 `presence`，此后每 30 秒一条；服务端以收到时间记为 `last_seen_at`。
- 90 秒未收到 `presence` 判为离线；离线只影响在线状态，不吊销令牌、不结束会话。
- `presence` 不参与鉴权与授权判定；未发送 `presence` 的旧客户端在在线表中记为 `unknown`。
- 在线状态只经信令服务内部查询（供 `list_devices` 使用），不向其它设备广播，不携带画面、输入与剪贴板内容。

## 五、会话密钥与媒体加密

- 每次会话生成临时 X25519 密钥对，经 HKDF-SHA256 派生双向密钥。
- 媒体与控制消息使用 ChaCha20-Poly1305；96 位随机起始 nonce 加单调计数器，禁止重用。
- 每 30 分钟或累计 1GB 触发重密钥；旧密钥保留一个短窗口，避免丢包。
- 重放保护：消息序号单调递增；重复或过期消息丢弃并计数。

## 六、中继数据面

- 中继连接使用 TLS 1.3，`relay_token` 认证；令牌绑定 `session_id`、设备标识、预算与过期时间。
- 中继只转发 AEAD 密文与最少元数据，不解析屏幕、输入与剪贴板内容。
- 预算由 `RelayPolicy` 计算：`egress_mbps`、`overhead_ratio`、`min_session_kbps`、`degrade_profile` 均来自配置；3 Mbps 只是当前部署默认值，不是协议常量。
- 预算不足时中继返回 `RELAY_FULL`，客户端降档或提示稍后重试；不得回退到明文。
- 数据面帧不单独携带 `from_device_id`：`relay_token` 已绑定 `session_id` 与设备，V1 每条会话严格为 1:1 发布/订阅，接收方由会话配对确定发送方；若后续引入 N:1 分屏扇出或跨账号归因，必须在 v2 增补发送方标识与校验规则（裁决见契约-202611-009）。
- `offer`、`answer`、`candidate`、`session_start`、`session_end` 的 `from_device_id`、`to_device_id` 保留为可选提示字段；服务端投递以连接身份与 `session_id` 为准，两字段不得作为鉴权或授权依据。

## 七、错误码

| 错误码 | 触发场景 | 调用方处理 |
|---|---|---|
| AUTH_FAILED | 设备签名或口令认证失败 | 提示重新输入口令；累计失败触发锁定 |
| TOKEN_INVALID | 令牌过期、被篡改或与设备不匹配 | 重新走 device 鉴权，不重试原消息 |
| PEER_OFFLINE | 对端设备不在线 | 提示稍后重试 |
| RELAY_FULL | 中继预算不足 | 降档或提示稍后重试 |
| RELAY_UNAVAILABLE | 中继不可达 | 提示检查网络或端点 |
| PROTOCOL_VERSION_UNSUPPORTED | 版本不兼容 | 提示升级客户端 |
| RATE_LIMITED | 请求过快或失败次数过多 | 按提示等待后重试 |
| FILE_OFFER_REJECTED | 接收端拒绝传输提议（空间不足、路径不安全、scope 不足） | 停止发送并提示原因，不重试同一提议 |
| FILE_CHUNK_INVALID | 块长度、偏移或摘要非法，或 complete/abort 后仍收到块 | 丢弃该块并要求重传，必要时中止任务 |
| CLIPBOARD_DIGEST_MISMATCH | 剪贴板内容与摘要不一致 | 丢弃内容并请求重发 |
| WALL_OWNERSHIP_CONFLICT | `wall_bind` 的 `wall_id` 已归属其它账号 | 停止重试；由原归属账号先解绑或改用其它墙标识 |

错误 `message` 为用户可读中文，不出现异常类名、堆栈、SQL 与文件路径。

## 八、版本与兼容

- v1 冻结日期 2026-11-10；v1.1 于同日按契约-202611-002 追加 `display_update` 并扩容画质档位枚举，只增不删；v1.2 于同日按契约-202611-006 追加 `presence` 设备级在线心跳与 90 秒过期口径，只增不删；v1.3 于同日按契约-202611-007 追加文件传输 6 类与剪贴板 3 类消息、大小上限与顺序语义，只增不删；v1.4 于同日按契约-202611-009 追加屏幕墙聚焦与预览 7 类消息及失效语义，只增不删；v1.5 于同日按契约-202611-015 追加屏幕墙归属登记 `wall_bind`/`wall_bound` 与 `WALL_OWNERSHIP_CONFLICT`，只增不删；v1.6 于同日按契约-202611-014 追加 `file_result` 整文件校验回执、`file_offer.digest` 列为必填、`file_accept.resume` 明确为续传偏移列表，并补 3.4 节背压与降级通道可见性语义，只增不删；新增可选字段不升版本，删除字段、改字段语义、改消息类型含义必须升 v2。
- 新旧版本至少并行一个发布周期；下线日期写入本文件。
- 客户端与服务端在 `register` 与 `auth_ok` 中交换版本；不兼容时返回 `PROTOCOL_VERSION_UNSUPPORTED`。
- 任何字段改动走契约变更记录，经 M03 批准后落地。
