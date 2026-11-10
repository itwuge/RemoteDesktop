# 被控端 answer 候选下发与主动打洞（S11）

- 状态: 待评审
- 关联规格: `docs/specs/ipv6-punch/spec.md` §3（候选模型）、§4.1（L1 预算）、§5/§6（打洞与数据面）、§7（信令）
- 上游进展: 安卓 S9/S10 已完成，控制端已能解析 answer 里的 `candidates`
- 问题现象: 被控端 answer 只回 `chosen_candidate`（relay），控制端拿不到 v6 host / srflx，稳定回落中继；同时被控端只被动应答 RDCP1，控制端 `backward` 恒为 0，双向判据无法成立

## 一、目标与非目标

### 目标

1. 被控端 answer 下发全量候选：v6 host（真实会话监听端口）、v4 host（打洞端口）、srflx、relay。
2. `chosen_candidate` 原样保留，旧客户端不受影响。
3. 被控端用现有 STUN 能力在打洞 socket 上做一次 IPv4 映射，拿不到 srflx 时只发 host + relay，不造值。
4. 下发前剔除 link-local 与 hairpin（与对端出口相同的候选）。
5. 被控端主动向控制端 UDP 候选发 RDCP1 探测，保证控制端 `backward > 0`，同时打开本端 NAT 映射。
6. 全树六项门禁通过，补回归测试，不加 noqa / 豁免 / 拆字符串。

### 非目标

- 不改 `desktop-v1` 命令签名，不新增命令字段。
- 不改 UDP 数据面封装（16 字节头、1200 字节报、分片、ACK、心跳不动）。
- 不改安卓代码。
- 不实现 IPv6 UDP 打洞；IPv6 仍走 L1 TCP 直连，`candidates` 里的 v6 host 只服务 TCP。
- 不改中继协议与 `chosen_candidate` 语义。

## 二、契约与兼容

`answer.payload` 新增只增字段 `candidates`，类型为候选对象数组，字段与 `desktop-v1` 候选 schema 一致：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kind` | string | `host` / `srflx` / `relay`，不新增枚举 |
| `family` | int | 4 / 6；推断失败为 0，仅 relay 允许 0 |
| `address` | string | 不带方括号的裸地址 |
| `port` | int | 1..65535 |
| `priority` | int | 0..100，按 §3.2 表 |
| `related` | string | 关联地址，可为空 |
| `scope` | string | `global` / `ula` / `private` / `link` |
| `origin` | string | `host` / `stun` / `relay` |
| `proto` | string | `tcp` / `udp` |

兼容约束：

- 只对 `accepted=true` 的 answer 携带 `candidates`；拒绝的 answer 保持原样。
- `chosen_candidate` 继续固定为 relay（`kind=relay`、本机出口地址、会话端口），旧客户端只读它仍可走中继。
- 序列化统一走 `rdc.connect.relay.candidate_to_payload`，禁止手写字典绕过 `normalize_candidate`。
- 接收侧继续用 `payload_to_candidate` 容错：未知字段忽略、非法数字回默认、不抛异常。

## 三、候选组成

被控端在 `_on_offer` 通过准入校验后、发 answer 前构造候选：

1. **v6 host**：`native_host_candidates(v4_port=udp_port, v6_port=config.port)` 产出的全局/ULA 地址。
   - 端口必须是会话 TCP 监听端口 `config.port`（当前 27888），服务 L1 TCP 直连。
   - link-local 不发布（`is_publishable` 已有规则）。
2. **v4 host**：同一调用的 IPv4 地址，端口必须是打洞 socket 的 UDP 端口。
   - 打洞 socket 绑定 `config.port`；没有打洞 socket（绑定失败）时 `v4_port=0`，不发布 v4 host，避免宣告没人监听的端口。
3. **srflx**：在打洞 socket 上复用 `rdc.connect.stun.discover_srflx` 做一次 IPv4 映射。
   - 必须复用同一 socket，保证映射端口 = 打洞端口，否则控制端打过来的包收不到。
   - 预算 1500 ms；失败返回空候选，只发 host + relay，不伪造地址或端口。
4. **relay**：`kind=relay`、`family=4`、`address=local_ip()`、`port=config.port`、`priority=10`、`origin=relay`、`proto=tcp`。
5. 统一排序去重：按 `-priority, family, address, port` 排序，重复（kind/family/address/port）保留高优先级。

## 四、过滤规则

新增纯函数 `filter_candidates(items, egress=())`，语义与安卓 `removeHairpin` 对齐：

1. `is_publishable` 不通过的候选剔除（含 v6 host link-local、family=0 的非 relay）。
2. `is_link_local` 为真的候选剔除（IPv6 `fe80::/10`、IPv4 `169.254.0.0/16`）。
3. hairpin：取 `egress` 里 `kind=srflx`（`mapped` 归一后同）的地址集合；`items` 中 `kind` 属于 `host/srflx/relay` 且地址命中该集合的候选剔除，并计数。
4. 返回 `(保留候选, 剔除的 hairpin 数)`，保留项已排序去重。

应用位置：

- **下发 answer 前**：`filter_candidates(本地候选, 控制端候选)`，剔除本端对控制端不可用的同出口候选。
- **主动探测前**：`filter_candidates(控制端候选, 本端候选)`，剔除 link-local 与同出口 hairpin，避免向无效目标打洞。

打洞目标只取 `family=4` 且 `kind in {host, srflx}` 的候选；relay 不参与打洞。

## 五、主动探测

`PunchResponder` 从"只应答"扩展为"应答 + 主动发包"，但保持所有权与交接语义不变：

- 新增 `set_targets(targets, budget_s=8.0)`：去重后保存目标，重置待回复表与探测截止时间。
- 新增 `exclusive()` 上下文管理器：持有 socket I/O 锁并让出当前 socket，供 STUN 等一次性收发使用。
- `poll(wait_s)` 在锁内先按 100 ms 节奏向目标发 `Q`（新 token 记入待回复表），再 `select` 读包并处理。
- 收到 `R` 且 token 命中待回复表：`forward += 1`、刷新 `peer` 与 `last_seen`，**不**计入 `ignored`。
- 收到 `Q`：照旧回 `R`、`replies += 1`、刷新 `peer`。
- 非 RDCP1 数据报：仍计入 `ignored`，`_punch_loop` 的"切数据面"判据保持不变。
- 探测预算默认 8 s（覆盖控制端 L2 5 s 预算与握手余量），超时后停止主动发包但仍继续应答。
- 所有 socket 收发（`poll`、`set_targets`、`release`、`close`、`exclusive`）共用一把 `threading.Lock`，避免 STUN 响应被轮询线程抢走。
- 被控端轮询粒度从 200 ms 调整为 100 ms，匹配控制端 `CHECK_INTERVAL_MS`。

## 六、STUN 双栈发送

被控端打洞 socket 是 `AF_INET6` 双栈（`V6ONLY=0`），直接 `sendto` IPv4 元组会报地址族错误。`rdc.connect.stun` 新增 `_server_endpoint(sock, server)`：

- `AF_INET` socket：原样返回。
- `AF_INET6` socket：IPv6 字面量原样返回；IPv4 字面量转 `::ffff:a.b.c.d`；主机名先按 `AF_INET` 解析再转 v4-mapped。
- `query_server` 用该函数发送，源端口保持打洞端口，映射结果仍是 IPv4 地址，`discover_srflx` 现有解析逻辑不变。

## 七、文件与函数变更

| 文件 | 变更 |
| --- | --- |
| `src/rdc/connect/policy.py` | 新增 `sort_candidates(items)`、`is_link_local(item)`、`filter_candidates(items, egress=())`；复用现有 `normalize_candidate` / `is_publishable` / `dedupe_candidates` / `_candidate_sort_key` |
| `src/rdc/connect/stun.py` | 新增 `_server_endpoint(sock, server)`；`query_server` 改用它发送 |
| `src/rdc/connect/punch.py` | 新增常量 `RESPONDER_PROBE_INTERVAL_S=0.1`、`RESPONDER_PROBE_BUDGET_S=8.0`；`PunchResponder` 增加 `_io_lock` / `_targets` / `_pending` / `_next_probe` / `_probe_deadline` / `sent` / `forward`、`set_targets`、`exclusive`、`_send_probes`；改造 `poll`、`_drain`、`release`、`close` |
| `src/rdc/session/host.py` | 新增常量 `ANSWER_CANDIDATE_BUDGET_MS=1500`，`PUNCH_POLL_S` 改 0.1；新增 `_answer_candidates()`、`_peer_candidates(payload, local)`；`_on_offer` 组装候选、过滤、下发、设置探测目标；`_answer` 增加 `candidates` 参数并写入 payload；`punch_stats` 增加 `sent` / `forward` / `targets` |
| `tests/test_connect_policy.py` | 新增候选过滤与排序用例 |
| `tests/test_connect_runtime.py` | 新增主动探测与双栈 STUN 端点用例 |
| `tests/test_ipv6_listen.py` | 新增 answer 候选组成、answer 载荷、offer 候选过滤用例 |

## 八、日志与可观测

- `punch.targets`：`peer=<目标数>`、`parsed=<原始候选数>`、`hairpin_blocked=<剔除数>`，便于现场确认是否拿到控制端候选。
- `punch.plane_attached` 保持原字段，新增 `sent` / `forward`。
- `status().punch` 增加 `sent`、`forward`、`targets`，旧字段不变。

## 九、验证

1. 新增单测覆盖：
   - 候选组成：v6 host 端口 = 会话端口，v4 host 端口 = 打洞端口，srflx 存在，relay 优先级 10；STUN 失败时只有 host + relay。
   - answer 载荷：`chosen_candidate` 仍为 relay；`candidates` 每项含 `family/scope/origin/proto`；不含 `kind=mapped`。
   - 过滤：v6 link-local、v4 link-local、同出口 srflx 均被剔除；局域网 host 保留；hairpin 计数正确。
   - 主动探测：回环两个 UDP socket，`set_targets` 后 `poll` 能收到 `Q`；对端回 `R` 后 `forward=1` 且 `peer` 正确；无目标时不发包。
   - 双栈 STUN：`AF_INET6` 双栈 socket 向回环 IPv4 假 STUN 服务查询，能解析出映射地址与端口。
2. 全树门禁：`python scripts/gate.py` 六项全绿，证据落 `D:\RDC-Build\evidence\gate\<时间戳>`。
3. 不做真机 NAT 验收；真机 IPv6 直连与打洞仍由 S6 验收脚本取证。

## 十、风险与回滚

- STUN 在信令读线程内最多阻塞 1500 ms，answer 会相应延后；控制端等待预算足够，若现场反馈超时，可把 `ANSWER_CANDIDATE_BUDGET_MS` 降到 800 ms。
- 双栈 socket 向 v4-mapped 地址发送在个别 Windows 版本可能被策略拦截；失败时 `discover_srflx` 返回空，自动降级为 host + relay，不影响中继兜底。
- 主动探测期间若控制端已切数据面，最多多收 100 ms 的 `Q`；`ignored` 判据不变，不会误判交接。
- 回滚：删除 `host._on_offer` 中的候选组装与 `set_targets` 调用即可恢复旧 answer；`candidates` 为只增字段，旧端无感。

## 十一、审批

请确认以下三点后进入实现：

1. 被控端 answer 候选采用"v6 host(TCP) + v4 host(UDP) + srflx + relay"四类，端口分别取会话端口与打洞端口。
2. hairpin 过滤口径与安卓 `removeHairpin` 对齐：以对端/本端 srflx 地址集合为出口判据，命中即剔除 `host/srflx/relay`。
3. 被控端主动探测 100 ms 一次、预算 8 s，`R` 回复不计入 `ignored`。
