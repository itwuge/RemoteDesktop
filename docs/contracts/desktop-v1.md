# 桌面命令与端口契约 v1

| 字段 | 内容 |
|---|---|
| 契约名称 | 桌面命令与端口契约 |
| 契约版本 | v1.7 |
| 对应设计书 | 项目设计书-项目-202611-001-20261110 |
| 冻结日期 | 2026-11-10 |
| 批准人 | M03 架构师 |
| 调用方 | 桌面界面层、Android 薄壳、会话编排层、平台适配层 |
| 提供方 | 命令层、端口实现方 |
| 依据规范 | .team/specs/14_交互契约.md 第 2 节与第 3 节 |

> 本文件只写契约正文，不写实现代码。命令函数与端口签名、返回结构、错误码以本文件为准；设计书只列清单与签名，不替代本文件。任何改动走契约变更记录，先经 M03 批准。

## 一、通用原则

- 先定契约、再写实现；契约未冻结不得开工写实现。
- 上层只依赖接口声明，具体实现由装配处注入；函数内部不直接构造网络客户端、采集器、编码器。
- 契约只描述行为，不出现文件路径、控件类名、窗口类名、SQL 与表名。
- 层边界统一返回 `CommandResult`，不向上抛原生异常类型。
- 命令函数命名以动词开头，首词限定为 create、delete、update、list、get、start、stop、cancel、confirm、open、close、export、import、validate、refresh、apply；v1.5 起追加 revoke、transfer、issue、logout，v1.6 起追加 recover、set，既有命令不改名。
- 一个用户动作对应一个命令函数，不按参数分叉动作。

## 二、统一返回结构

`CommandResult` 五字段固定：

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| ok | 布尔 | 是 | 是否成功 |
| message | 字符串 | 是 | 用户可见文案，界面层原样呈现，不得为空 |
| error_code | 字符串 | 否 | 仅调用方需要分支判断时给码，其余留空 |
| data | 对象 | 否 | 成功时的结果数据，失败时不得携带可被误当成功的数据 |
| confirm | 对象 | 否 | 需确认时出现，含提示文案与确认令牌；出现时 ok 必须为假 |

## 三、命令函数

长任务阈值：200ms。超过阈值的命令必须返回任务句柄，并提供进度查询与取消函数。

| 序号 | 命令函数 | 完整签名 | 成功时 data | 错误码 | 说明 |
|---|---|---|---|---|---|
| 1 | start_connection | `start_connection(device_id: str, access_code: str) -> CommandResult` | `{"task_id": str, "total": int}` | `DEVICE_NOT_FOUND`、`ACCESS_CODE_INVALID`、`SESSION_BUSY` | 发起连接，立即返回任务句柄；`total` 为连接阶段总数 |
| 2 | get_connection_progress | `get_connection_progress(task_id: str) -> CommandResult` | `{"completed": int, "total": int, "status": str, "cancelable": bool}` | `TASK_NOT_FOUND` | `status` 取 pending、running、canceled、done、failed |
| 3 | cancel_connection | `cancel_connection(task_id: str) -> CommandResult` | `{"task_id": str, "status": str}` | `TASK_NOT_FOUND` | 取消连接任务；已完成或已失败时返回失败文案 |
| 4 | stop_session | `stop_session(session_id: str) -> CommandResult` | `{"session_id": str, "status": str}` | `SESSION_NOT_FOUND` | 停止已建立的会话 |
| 5 | get_session_status | `get_session_status(session_id: str) -> CommandResult` | `{"state": str, "transport_kind": str, "quality_profile": str, "peer_device_id": str}` | `SESSION_NOT_FOUND` | `state` 取 idle、connecting、connected、degraded、closed；`transport_kind` 取 direct、relay、none |
| 6 | list_sessions | `list_sessions() -> CommandResult` | `{"items": list}` | 无 | 返回当前会话摘要列表 |
| 7 | apply_quality_profile | `apply_quality_profile(session_id: str, profile: str) -> CommandResult` | `{"session_id": str, "quality_profile": str}` | `SESSION_NOT_FOUND`、`PROFILE_UNSUPPORTED` | profile 直连档取 4K60、1440p60、1080p60、1080p30（地板）与 auto；900p30、720p30、540p30、720p15 仅用于中继降档，直连路径下发视为 `PROFILE_UNSUPPORTED` |
| 8 | get_session_metrics | `get_session_metrics(session_id: str) -> CommandResult` | `{"p50_ms": float, "p95_ms": float, "fps": float, "bitrate_kbps": int, "dropped_frames": int, "ssim": float 或 None, "quality_profile": str, "gpu_percent": float, "rss_mb": float, "switches_used": int, "switches_limit": int, "quality_events": list[dict]}` | `SESSION_NOT_FOUND` | 指标为当前会话快照；`quality_events` 每条含 `ts`、`from`、`to`、`reason`、`metric`、`value`、`threshold`、`window_ms`，只保留最近 32 条，切换记录不得丢弃或改写 |
| 9 | start_hosting | `start_hosting(display_name: str) -> CommandResult` | `{"device_id": str, "access_code": str, "expires_at": str}` | `PERMISSION_DENIED`、`HOSTING_ALREADY_ACTIVE` | 启动被控；`access_code` 只在 data 中返回，界面层不得写入日志 |
| 10 | stop_hosting | `stop_hosting() -> CommandResult` | `{"device_id": str, "status": str}` | 无 | 停止被控 |
| 11 | refresh_access_code | `refresh_access_code() -> CommandResult` | `{"access_code": str, "expires_at": str}` | `HOSTING_NOT_ACTIVE` | 刷新一次性口令 |
| 12 | open_settings | `open_settings() -> CommandResult` | `{"section": str}` | 无 | 打开设置页；界面层据 data 呈现 |
| 13 | apply_relay_policy | `apply_relay_policy(endpoint: str, egress_mbps: int, degrade_profile: str) -> CommandResult` | `{"policy": dict}` | `POLICY_INVALID`、`RELAY_UNREACHABLE` | 仅本机管理员可用；带宽与档位写配置，不写协议常量 |

错误码口径：

- `DEVICE_NOT_FOUND`：设备标识不存在，界面应提示重新输入或刷新设备列表。
- `ACCESS_CODE_INVALID`：一次性口令错误或过期，界面应回到口令输入。
- `SESSION_BUSY`：对端已有会话或本机连接数达上限，界面应提示稍后重试。
- `TASK_NOT_FOUND`：任务标识不存在或已过期，界面应刷新任务列表。
- `SESSION_NOT_FOUND`：会话不存在或已关闭，界面应回到会话列表。
- `PROFILE_UNSUPPORTED`：档位超出当前通路预算，界面应恢复为 auto。
- `PERMISSION_DENIED`：系统权限不足，界面应提示具体权限名称。
- `HOSTING_ALREADY_ACTIVE`：已处于被控状态，界面应提示先停止。
- `HOSTING_NOT_ACTIVE`：未处于被控状态，界面应提示先启动被控。
- `POLICY_INVALID`：中继策略不合法，界面应提示检查配置。
- `RELAY_UNREACHABLE`：中继不可达，界面应提示检查端点或稍后重试。

其余失败只给 `message`，不得为整齐逐条编号。

### 3.2 账号、授权与屏幕墙命令（v1.2 与 v1.3 追加）

V1 首版范围内新增 18 个命令，原 13 个命令签名与语义不变。变更依据：契约-202611-003（v1.2）与契约-202611-004（v1.3）。

| 序号 | 命令函数 | 完整签名 | 成功时 data | 错误码 |
|---|---|---|---|---|
| 14 | register_account | `register_account(account: str, password: str) -> CommandResult` | `{"account_id": str, "created_at": str}` | 无专用码，账号已存在只给 `message` |
| 15 | login_account | `login_account(account: str, password: str) -> CommandResult` | `{"account_id": str, "device_token": str, "session_token": str, "expires_at": str}` | `ACCOUNT_NOT_FOUND`、`CREDENTIAL_INVALID` |
| 16 | refresh_session_token | `refresh_session_token(device_token: str) -> CommandResult` | `{"session_token": str, "expires_at": str}` | `TOKEN_EXPIRED`、`TOKEN_REVOKED` |
| 17 | bind_device | `bind_device(device_id: str, access_code: str) -> CommandResult` | `{"device_id": str, "bound_at": str}` | `DEVICE_NOT_FOUND`、`ACCESS_CODE_INVALID`、`PERMISSION_DENIED` |
| 18 | unbind_device | `unbind_device(device_id: str) -> CommandResult` | `{"device_id": str, "status": str, "kicked_sessions": int}` | `DEVICE_NOT_FOUND`、`PERMISSION_DENIED` |
| 19 | list_devices | `list_devices() -> CommandResult` | `{"items": list}`，每项含 `device_id`、`display_name`、`bound_at`、`online`、`require_confirm`、`owner` | 无 |
| 20 | list_grants | `list_grants(device_id: str) -> CommandResult` | `{"items": list}`，每项含 `grant_id`、`subject`、`scope`、`expires_at`、`transferable`、`one_time`、`revoked_at`、`active` | `DEVICE_NOT_FOUND` |
| 21 | revoke_grant | `revoke_grant(grant_id: str) -> CommandResult` | `{"grant_id": str, "status": str}` | `GRANT_INVALID`、`PERMISSION_DENIED` |
| 22 | request_access | `request_access(device_id: str, scope: str, reason: str) -> CommandResult` | `{"request_id": str, "status": str}` | `DEVICE_NOT_FOUND`、`PERMISSION_DENIED` |
| 23 | approve_access | `approve_access(request_id: str, ttl_seconds: int, one_time: bool) -> CommandResult` | `{"grant_id": str, "expires_at": str, "one_time": bool}` | `REQUEST_NOT_FOUND`、`PERMISSION_DENIED` |
| 24 | reject_access | `reject_access(request_id: str, reason: str) -> CommandResult` | `{"request_id": str, "status": str}` | `REQUEST_NOT_FOUND`、`PERMISSION_DENIED` |
| 25 | list_access_requests | `list_access_requests(direction: str) -> CommandResult` | `{"items": list}`；`direction` 取 `incoming`、`outgoing` | 无专用码，非法 direction 只给 `message` |
| 26 | get_audit_log | `get_audit_log(device_id: str, since: str) -> CommandResult` | `{"items": list}`，每项含 `ts`、`event`、`actor`、`target`、`result`、`detail` | `DEVICE_NOT_FOUND`、`PERMISSION_DENIED` |
| 27 | set_trust_policy | `set_trust_policy(device_id: str, require_confirm: bool) -> CommandResult` | `{"device_id": str, "require_confirm": bool, "updated_at": str}` | `DEVICE_NOT_FOUND`、`PERMISSION_DENIED` |
| 28 | start_wall | `start_wall(layout: int) -> CommandResult` | `{"wall_id": str, "layout": int, "cells": list, "degraded": bool}` | `WALL_BUDGET_EXCEEDED` |
| 29 | set_wall_layout | `set_wall_layout(wall_id: str, layout: int) -> CommandResult` | `{"wall_id": str, "layout": int, "cells": list, "degraded": bool}` | `WALL_NOT_FOUND`、`WALL_BUDGET_EXCEEDED` |
| 30 | focus_wall_cell | `focus_wall_cell(wall_id: str, cell: int) -> CommandResult` | `{"wall_id": str, "cell": int, "session_id": str, "quality_profile": str}` | `WALL_NOT_FOUND`、`WALL_BUDGET_EXCEEDED` |
| 31 | close_wall | `close_wall(wall_id: str) -> CommandResult` | `{"wall_id": str, "status": str}` | `WALL_NOT_FOUND` |

新增错误码：

- `ACCOUNT_NOT_FOUND`：账号不存在，界面应提示检查账号或去注册。
- `CREDENTIAL_INVALID`：口令错误或未登录，界面应回到登录/口令输入。
- `TOKEN_EXPIRED`：令牌过期，界面应重新登录换取设备令牌。
- `TOKEN_REVOKED`：令牌被吊销（解绑、改密、主动撤销），界面应提示并回到登录。
- `GRANT_INVALID`：授权不存在或已失效，界面应刷新授权列表。
- `REQUEST_NOT_FOUND`：申请不存在或已处理，界面应刷新申请列表。
- `WALL_BUDGET_EXCEEDED`：屏幕墙超出解码预算或格号越界，界面应提示降级方案。
- `WALL_NOT_FOUND`：屏幕墙不存在或已关闭，界面应回到屏幕墙列表。

### 3.3 同账号免登录与申请制的判定顺序（硬性）

任何实现不得调整以下先后：

1. 会话已建立（含屏幕墙预览流）→ 走既有会话令牌校验。
2. 同一账号且设备 `require_confirm=false` → **免确认直连**：免输连接码、免逐次确认；仍走完整握手、密钥协商、鉴权与审计。
3. 存在覆盖本次 `scope` 的生效授权（未过期、未撤销、未用尽）→ 按授权建立；一次性授权使用时立即标记已用。
4. 以上都不满足 → 进入**申请制**，等待设备所有者或被控端本机审批；审批通过生成有时限、可一次性授权。
5. 审批被拒或超时 → 按拒绝处理并留痕，不得静默放行。

免确认开关默认开启（`require_confirm=false`），被控端可对单台设备关闭；开关变更进审计 `trust_policy.change`。免登录**不得**降低加密与鉴权要求：禁止明文、禁止固定密钥、禁止跳过证书或令牌校验。

### 3.4 屏幕墙硬约束（v1.3）

- 布局仅 `1`、`4`、`9`、`16`。
- 每格默认预览档 `preview_480p15`（854×480、15fps、≤300 kbps）；预览档不属于画质档位表。
- 点选某格升级为聚焦档，走直连四档；同一时刻**只允许 1 格**聚焦。
- 解码预算：预览格 1 单位、4K60 聚焦格 4 单位；默认预算 12 单位。
- **严禁 N 路 4K60 同时解码**：任何时刻 4K60 解码路数不得大于 1，违反即由命令层拒绝。
- 超限降级顺序固定为：减格 → 预览降 `360p10` → 暂停未聚焦格；每次只做一级，结果写入 `degraded` 与审计 `wall.degrade`。
- 屏幕墙预览流复用与正式会话相同的信任链与令牌校验，不因「预览」降低鉴权。

### 3.5 剪贴板、拖拽与文件传输命令（v1.4 追加）

| 序号 | 命令 | 签名 |
|---|---|---|
| 32 | 开始传输 | `start_transfer(source_path: str, destination_dir: str, rate_limit_kbps: int, link_kbps: int) -> CommandResult` |
| 33 | 查询传输进度 | `get_transfer_progress(job_id: str) -> CommandResult` |
| 34 | 暂停传输 | `pause_transfer(job_id: str) -> CommandResult` |
| 35 | 续传 | `resume_transfer(job_id: str) -> CommandResult` |
| 36 | 取消传输 | `cancel_transfer(job_id: str) -> CommandResult` |
| 37 | 剪贴板开关 | `set_clipboard_enabled(enabled: bool) -> CommandResult` |
| 38 | 推送剪贴板 | `push_clipboard(origin: str, kind: str, content: str) -> CommandResult` |
| 39 | 拉取剪贴板 | `pull_clipboard(target: str) -> CommandResult` |
| 40 | 剪贴板状态 | `get_clipboard_status() -> CommandResult` |

契约错误码追加：

- `TRANSFER_NOT_FOUND`：传输任务不存在，界面应重新选择源文件。
- `TRANSFER_PERMISSION_DENIED`：目标端权限不足，界面须原样提示并允许改目录重试。
- `TRANSFER_HASH_MISMATCH`：整体哈希不一致，任务判定失败，必须重传。
- `CHUNK_CHECKSUM_MISMATCH`：分块回读校验失败，任务判定失败，必须重试。
- `TRANSFER_IO_ERROR`：源文件不存在或读写失败。
- `TRANSFER_RATE_INVALID`：限速或带宽参数非法。
- `CLIPBOARD_DISABLED`：同步开关关闭，双向命令一律拒绝。
- `CLIPBOARD_UNSUPPORTED_KIND`：只支持 `text` 与 `image`。
- `CLIPBOARD_TOO_LARGE`：图片超过 4 MiB 上限，取消本次同步。
- `CLIPBOARD_EMPTY`：没有可拉取的内容。
- `CLIPBOARD_DECODE_ERROR`：图片 base64 非法。

硬约束：

- **剪贴板双向同步**：文本按 UTF-8 传输，上限 256 KiB，超限截断且 `message` 必须含「已截断」提示；图片按 base64 传输，上限 4 MiB，超限返回 `CLIPBOARD_TOO_LARGE` 且不得截断图片；内容与摘要均不得以明文写入日志，只允许记录类型、字节数与摘要。
- **防环回与去重**：记录最近来源与摘要；同来源同摘要不得回写，跨来源同摘要视为重复推送并抑制，返回 `applied=false`。
- **拖拽传输**：界面把本地文件路径交给 `start_transfer`，不得在界面层做分块、校验或限速；未建立会话时不得开始传输，必须给出明确提示。
- **文件传输**：分块 256 KiB；每块写入后回读比对 SHA-256；全部完成后按相对路径与内容计算整体哈希并比对。支持目录递归、暂停/续传（从 `sent_bytes` 继续）、取消。
- **低优先级通道**：传输可用带宽 = 链路带宽 − 视频地板档预留（默认 2500 KB/s）；请求限速超过可用带宽时收敛，进度中 `clamped=true`。传输不得使视频流跌破地板档。
- **进度语义**：`get_transfer_progress` 返回 `state`（running/paused/completed/failed/canceled）、`percent`、`speed_kbps`、`eta_seconds`、`current_file`、`channel`、`rate_limit_kbps`、`digest`、`error_code`。当前内存实现由轮询推进分块，真实实现改为后台线程，字段与状态机不变。

### 3.6 界面落地约束（v1.4）

- 界面层只调用命令函数，不写会话、传输、加密逻辑；命令返回的任务句柄由轮询或回调更新进度。
- 每个页面必须能在无后端时构造并显示明确的「未连接 / 未登录」状态，不得崩溃。
- 没有对应命令的按钮必须置灰并给出原因，不得保留点击无行为的死按钮。
- 屏幕墙预览格必须显示设备名与在线状态；未绑定设备显示「未绑定设备」。

### 3.7 账号授权补强、在线状态与屏幕墙审计（v1.5 追加）

变更依据：契约-202611-006（v1.5）。原 1 至 40 条命令签名与语义不变，本节追加 7 条命令与 4 个错误码。

| 序号 | 命令函数 | 完整签名 | 成功时 data | 错误码 |
|---|---|---|---|---|
| 41 | create_grant | `create_grant(device_id: str, subject: str, scope: str, ttl_seconds: int, one_time: bool, transferable: bool) -> CommandResult` | `{"grant_id": str, "subject": str, "scope": str, "expires_at": str, "one_time": bool, "transferable": bool}` | `DEVICE_NOT_FOUND`、`PERMISSION_DENIED`、`SCOPE_INVALID` |
| 42 | transfer_grant | `transfer_grant(grant_id: str, subject: str, ttl_seconds: int, one_time: bool, scope: str = "") -> CommandResult` | `{"grant_id": str, "parent_grant_id": str, "subject": str, "scope": str, "expires_at": str, "one_time": bool, "transferable": bool}` | `GRANT_INVALID`、`GRANT_NOT_TRANSFERABLE`、`GRANT_SCOPE_EXCEEDED`、`PERMISSION_DENIED` |
| 43 | logout_account | `logout_account() -> CommandResult` | `{"account_id": str, "status": str, "revoked_at": str, "kicked_sessions": int}` | 无专用码；未登录只给 `message` |
| 44 | revoke_device_token | `revoke_device_token(device_id: str) -> CommandResult` | `{"device_id": str, "status": str, "revoked_tokens": int, "kicked_sessions": int, "revoked_at": str}` | `DEVICE_NOT_FOUND`、`PERMISSION_DENIED` |
| 45 | stop_wall_focus | `stop_wall_focus(wall_id: str) -> CommandResult` | `{"wall_id": str, "layout": int, "cells": list, "focused_cell": null, "quality_profile": "preview_480p15", "degraded": bool, "degrade_steps": list, "units_used": int, "units_budget": int}` | `WALL_NOT_FOUND` |
| 46 | get_wall_audit_log | `get_wall_audit_log(wall_id: str, since: str = "") -> CommandResult` | `{"wall_id": str, "items": list}`，每项含 `ts`、`event`、`actor`、`target`、`result`、`detail` | `WALL_NOT_FOUND`、`PERMISSION_DENIED` |
| 47 | issue_preview_token | `issue_preview_token(wall_id: str, cell: int) -> CommandResult` | `{"token": str, "wall_id": str, "cell": int, "scope": "view", "expires_at": str, "single_use": false}` | `WALL_NOT_FOUND`、`WALL_BUDGET_EXCEEDED`、`PERMISSION_DENIED` |

新增错误码：

- `SCOPE_INVALID`：scope 不在 `view`、`control`、`full` 之内，界面应提示重新选择。
- `GRANT_NOT_TRANSFERABLE`：原授权未开启转授，界面应置灰转授入口。
- `GRANT_SCOPE_EXCEEDED`：转授 scope 高于原授权或到期晚于原授权，界面应回退到合法值。
- `PREVIEW_TOKEN_INVALID`：预览令牌过期、被重签吊销、墙已关闭或格已不存在；由预览流接入侧返回，界面应重新调用 47 换取。

scope 层级：

- 三层逐级包含：`view` < `control` < `full`。
- `view`：只读画面、画面指标与屏幕墙预览。
- `control`：含 `view` 全部，另含键鼠输入、剪贴板、拖拽与文件传输。
- `full`：含 `control` 全部，另含设备管理（绑定、解绑、免确认开关）、授权管理（第 41、42、21 条）与审计读取（第 26、46 条）。
- 生效判定按「覆盖」：授权 scope 等于或高于本次请求层级才算覆盖；不满足即按 §3.3 第 4 步进入申请制。
- 转授上限：scope 不得高于原授权，到期不得晚于原授权；`transferable=false` 一律拒绝；`one_time=true` 只能转授为 `one_time=true`；V1 只允许一层，子授权 `transferable` 固定为假，不接受链式转授。

设备在线状态（presence）：

- 来源为设备与信令服务之间的认证连接心跳：每 30 秒一条 `presence`，90 秒未收到判离线。
- `list_devices` 每项追加 `last_seen_at`（ISO 8601）与 `presence_state`（`online`、`offline`、`unknown`）；既有 `online` 保留，且必须满足 `online == (presence_state == "online")`。
- 未接入心跳的旧版本设备返回 `unknown`，界面显示「在线状态未知」，不得当作离线拦截操作。
- presence 只用于界面显示与选路提示，不得作为鉴权、免确认或授权判定依据；判定顺序仍按 §3.3。

屏幕墙审计与预览令牌：

- 固定事件集合：`wall.start`、`wall.layout`、`wall.focus`、`wall.focus.stop`、`wall.close`、`wall.degrade`、`wall.preview_token.issue`、`wall.preview_token.reject`。
- 墙关闭后记录至少保留 30 天，`close_wall` 不得删除历史；保留期内 46 必须可读，单次最多返回最近 1000 条并按时间升序。
- 从未存在或超出保留期的 `wall_id` 返回 `WALL_NOT_FOUND`。
- 预览令牌：有效期 60 秒，绑定 `wall_id`、`cell`、账号与设备，scope 固定 `view`，非一次性；同一墙同一格同一账号重新签发即吊销旧令牌。
- 预览令牌不替代身份鉴权：仍走 TLS、设备鉴权与 §3.3 判定，不得跨格、跨墙、跨账号复用，不得写入日志；墙关闭、格不存在或到期即失效。

多账号规则（V1 默认授权共享）：

- 一台设备可被多个账号控制，唯一路径为 grant：41 直接签发或 23 审批通过。
- 不引入成员、组织、角色模型，不新增成员命令；`list_devices` 的 `owner` 仍表示归属账号，非所有者账号只看到被授权的设备。
- 是否增加成员模型列为待用户表态项，未表态前按本节执行。

界面约束：

- 无权限的转授、吊销、审计入口必须置灰并说明原因，不得保留点击无行为的死按钮。
- 转授成功、吊销成功、预览令牌失效均需给出用户可见 `message`；预览令牌与设备令牌不得出现在界面文案或日志中。

### 3.8 文件传输接入与恢复（v1.6 追加）

变更依据：契约-202611-007。第 32 至 36 条签名不变，实现统一绑定 `rdc.files`，旧传输实现删除，不留双轨。

| 序号 | 命令函数 | 完整签名 | 成功时 data | 错误码 |
|---|---|---|---|---|
| 48 | recover_transfers | `recover_transfers(destination_dir: str = "") -> CommandResult` | `{"destination": str, "recovered": int, "items": list}`，每项含 `job_id`、`source`、`destination`、`state`、`sent_bytes`、`total_bytes`、`percent`、`rate_limit_kbps`、`channel` | `TRANSFER_IO_ERROR`、`TRANSFER_PERMISSION_DENIED`、`SCOPE_INSUFFICIENT` |
| 49 | set_transfer_scope | `set_transfer_scope(scope: str) -> CommandResult` | `{"scope": str}` | `SCOPE_INVALID` |

- 进程重启后，第 32 至 36 条留下的断点日志由 48 扫描并恢复为暂停态，用户再调 35 续传；无待恢复任务时 ok 为真且 `recovered=0`。
- 49 把当前会话授权范围写入传输门禁；装配默认 `control`，`view` 一律拒绝并返回 `SCOPE_INSUFFICIENT`。
- 新增错误码 `SCOPE_INSUFFICIENT`：取值合法但层级不足；`SCOPE_INVALID` 只表示取值不在三层之内。
- 线上消息见 protocol-v1 v1.3 第三节：`file_offer`、`file_accept`、`file_chunk`、`file_ack`、`file_complete`、`file_abort`；剪贴板 `clipboard_update`、`clipboard_request`、`clipboard_ack`。

### 3.9 账号与屏幕墙收口（v1.7 追加）

变更依据：契约-202611-008。

- 第 41、42 条错误码补充 `ACCOUNT_NOT_FOUND`：授权对象账号不存在；42 同时补 `SCOPE_INVALID`（显式 scope 非法）。
- 第 45 条 data 与第 28 至 31 条统一：允许且仅允许附加只读预算字段 `concurrent_4k60`、`max_concurrent_4k60`、`focus_units`；45 的完整字段为 `wall_id`、`layout`、`cells`、`focused_cell`、`quality_profile`、`degraded`、`degrade_steps`、`units_used`、`units_budget` 加上述三个。
- 第 46 条身份判定：签名不增加调用方身份参数，实现从当前登录账号取身份，要求对该墙全部设备具备 full 级管理权（设备所有者、被控端本机或 full 授权持有者）；未登录、非所有者、无 full 授权一律 `PERMISSION_DENIED`；未接线账号时默认拒绝，不得视为安全能力已就绪。
- `validate_preview_token` 是预览流接入侧内部 API，不是用户动作，不入命令编号表；固定签名为 `validate_preview_token`，参数依次为 token、wall_id、cell、account_id，返回 `CommandResult`，失败返回 `PREVIEW_TOKEN_INVALID`；只允许预览流接入侧调用，界面不得直接调用。
- presence 在线表由信令服务维护，命令层只经 `list_devices` 读取；信令线接入前 `presence_state` 保持 `unknown`，不得伪造在线。

## 四、长任务与确认

- 长任务命令返回 `data.task_id` 与 `data.total`；`message` 必须同时给出任务标识或总量，不得只写「已开始」。
- 进度查询返回 `completed`、`total`、`status`、`cancelable` 四字段；`status` 为枚举，不用整数。
- 需确认的动作由命令函数返回 `confirm`，含 `prompt` 与 `token` 两项；界面层询问后调用配套确认函数，不自行拼造令牌。
- 命令函数不得弹窗、不得阻塞界面线程、不得用打印充当用户提示。

## 五、模块端口

端口签名只使用原始值、枚举、值对象与句柄；端口异常在命令边界统一转换。

| 序号 | 端口 | 完整签名 | 返回 | 错误码 |
|---|---|---|---|---|
| 1 | CaptureSource.open | `open(params: CaptureParams) -> CaptureHandle` | CaptureHandle | `CAPTURE_PERMISSION_DENIED`、`CAPTURE_UNAVAILABLE` |
| 2 | CaptureSource.read | `read(handle: CaptureHandle) -> Frame` | Frame | `CAPTURE_TIMEOUT` |
| 3 | CaptureSource.close | `close(handle: CaptureHandle) -> None` | 无 | 无 |
| 4 | Encoder.open | `open(params: EncoderParams) -> EncoderHandle` | EncoderHandle | `ENCODER_UNAVAILABLE` |
| 5 | Encoder.encode | `encode(handle: EncoderHandle, frame: Frame) -> list[EncodedPacket]` | list[EncodedPacket] | `ENCODE_FAILED` |
| 6 | Encoder.close | `close(handle: EncoderHandle) -> None` | 无 | 无 |
| 7 | Decoder.open | `open(params: DecoderParams) -> DecoderHandle` | DecoderHandle | `DECODER_UNAVAILABLE` |
| 8 | Decoder.decode | `decode(handle: DecoderHandle, packet: EncodedPacket) -> Frame 或 None` | Frame 或 None | `DECODE_FAILED` |
| 9 | Transport.connect | `connect(candidates: list[Candidate], policy: TransportPolicy) -> TransportSession` | TransportSession | `DIRECT_FAILED`、`RELAY_UNAVAILABLE` |
| 10 | Transport.send | `send(session: TransportSession, packet: bytes) -> None` | 无 | `TRANSPORT_CLOSED` |
| 11 | Transport.recv | `recv(session: TransportSession, timeout_ms: int) -> bytes 或 None` | bytes 或 None | `TRANSPORT_CLOSED` |
| 12 | Transport.close | `close(session: TransportSession) -> None` | 无 | 无 |
| 13 | InputInjector.inject | `inject(event: InputEvent) -> None` | 无 | `INPUT_PERMISSION_DENIED` |
| 14 | QualityController.on_feedback | `on_feedback(feedback: NetworkFeedback) -> QualityDecision` | QualityDecision | 无 |
| 15 | RelayAllocator.allocate | `allocate(request: RelayRequest) -> RelayAllocation` | RelayAllocation | `RELAY_FULL`、`TOKEN_INVALID` |
| 16 | SignalingClient.open | `open(endpoint: str, identity: DeviceIdentity) -> SignalingSession` | SignalingSession | `SIGNALING_UNREACHABLE` |
| 17 | SignalingClient.request_session | `request_session(session: SignalingSession, peer_device_id: str, proof: bytes) -> SessionOffer` | SessionOffer | `PEER_OFFLINE`、`AUTH_FAILED` |
| 18 | ConfigStore.load | `load() -> AppConfig` | AppConfig | `CONFIG_INVALID` |

数据模型字段、类型与约束见项目设计书第六节，本文件不重复；字段变更必须同时更新设计书与契约版本。

## 六、版本与变更

- 当前版本 v1.7，冻结日期 2026-11-10；产品对外只有 V1 首版，契约只做内部小版本增量（v1.1、v1.2、v1.3、v1.4、v1.5、v1.6、v1.7），变更记录逐条留痕。
- 破坏性变更在本文件内升次版本并在变更记录中单列移除项；不得出现对外的「v2」说法。
- 非破坏性变更可在 v1 内进行：新增可选字段、新增命令、新增错误码。
- 任何改动先开契约变更记录，经 M03 批准后由对应岗位落地；未进契约的实现只能作为临时实验，不得进入发布。
