# 连接策略 v1：IPv6 直连 → P2P 打洞 → 中继兜底

- 状态：策略定稿（只定义口径与判据，未改 `docs/contracts/` 正文、未改 `src/`、未改 `android/`）
- 适用：Windows / macOS / Linux / Android 四个平台包，同一套顺序与阈值
- 实测依据：`docs/验收/P2P打洞可行性验证-2026-11-10.md`；本文件只引用，不重复展开

## 一、一条规则

建立会话时按 **L1 IPv6 直连 → L2 P2P 打洞 → L3 中继** 依次尝试，任一级双向成功即停止；会话建立后**只允许单向降级，不允许回切**。

## 二、决策流程

```
建连开始
  │
  ├─ 并行：收集 L1/L2 候选（v6 host、v4 host、STUN srflx）
  │
  ├─ L1 IPv6：双方都存在可用 v6 host 候选？
  │     否 ─────────────────────────────► L2
  │     是 → 双向连通检查（预算 1.5 s）
  │            成功 → transport=direct(kind=ipv6)  ──► 运行期监控
  │            失败 → L2（记 reason=ipv6_unreachable）
  │
  ├─ L2 P2P：对称型 NAT 判定命中？
  │     是 ─────────────────────────────► L3（不试打洞）
  │     否 → 候选排序 + 双向打洞（预算 5 s）
  │            双向成功 → transport=direct(kind=udp) ──► 运行期监控
  │            预算耗尽 → L3（记 reason=punch_timeout）
  │
  ├─ L3 中继：relay_allocate → relay_allocated(budget_kbps, quality_profile)
  │            成功 → transport=relay ──► 运行期监控（本会话不再回切）
  │            失败 → 会话失败（reason=relay_unavailable）
  │
运行期监控（仅 direct）
  ├─ 3 s 窗口丢包 > 20% ────────────────► 切 relay（单向）
  ├─ RTT p95 > 300 ms 持续 5 s ─────────► 切 relay（单向）
  ├─ 连续 2 s 无可解码帧 ───────────────► 切 relay（单向）
  └─ 关键帧请求 3 次仍无关键帧 ─────────► 切 relay（单向）
```

## 三、共享参数（唯一来源，四端取值必须一致）

| 参数 | 值 | 判据/来源 |
| --- | --- | --- |
| `ipv6_probe_timeout_ms` | 800 | 单个 v6 目标建连超时 |
| `ipv6_probe_targets` | 2 个 2400: 段目标 | 全失败才判本端 v6 不可用 |
| `ipv6_budget_ms` | 1500 | L1 总预算，超时即降 L2 |
| `stun_timeout_ms` | 800 | 单次 Binding 请求 |
| `stun_total_ms` | 1500 | 两个 STUN 服务并行 |
| `check_interval_ms` | 100 | 连通性检查发包间隔（实测 1 个周期可打通） |
| `candidate_budget_ms` | 2000 | 单个候选对的检查预算 |
| `direct_budget_ms` | 5000 | L2 总预算 |
| `success_min_responses` | 2 | 单个候选双向各收到 ≥2 个响应 |
| `success_min_rtt_samples` | 3 | 单向收到不算成功 |
| `relay_prefetch_at_ms` | 2000 | L2 进行到 2 s 未成功即并行发 `relay_allocate` |
| `degrade_loss_pct` | 20 | 3 s 窗口丢包率 |
| `degrade_loss_window_s` | 3 | 同上 |
| `degrade_p95_ms` | 300 | 持续 5 s 触发 |
| `degrade_p95_window_s` | 5 | 同上 |
| `degrade_no_frame_s` | 2 | 连续无可解码帧（对应公网中继实测最长 2.0 s 不可解码） |
| `keyframe_retry_limit` | 3 | 关键帧请求次数 |
| `reconnect_retry` | 1 | 仅新会话重建时，同一映射不原地重试 |

直连画质档位与中继档位沿用现有实现，不在本策略内重定义：

- 直连：`4K60 / 1440p60 / 1080p60 / 1080p30`，1080p30 为地板；
- 中继：`900p30 / 720p30 / 540p30 / 720p15`，直连会话下发这些档位应被拒。

### 实测参数（2026-11-10，ECS STUN 已就绪后复测）

STUN 端点：`<ECS-PUBLIC-IP>:3478`（UDP）。本机连续 5 次 Binding，全部成功，响应 44 B / type `0x0101`：

| 指标 | 实测值 | 证据 |
| --- | --- | --- |
| 成功率 | 5/5 | `evidence/p2p/20261110-1730-ecs-stun-x5/stun.json` |
| 映射结果 | `115.150.84.112:49360` × 5，唯一值 1 个 | 同上 |
| 端口保持 | 是（本地 49360 → 公网 49360） | 同上 |
| RTT min / p50 / p95 / max | 38.118 / 39.612 / 40.115 / 40.115 ms | 同上 |
| 映射行为 | endpoint-independent（锥形） | 同上 |

因此 `stun_timeout_ms=800` 对当前线路余量充足（p95 40 ms，约 20 倍余量）；`stun_total_ms=1500` 足够覆盖两次并行查询。

第二轮改用此前被丢弃段的端口 `49160`（服务器已把放行段扩到完整 **49152–65535**）复测：5/5 成功，映射仍为 `115.150.84.112:49160`（端口保持），RTT 38.317–44.685 ms，p50 39.942 ms，判 endpoint-independent。证据 `evidence/p2p/20261110-1755-ecs-stun-x5-49160/stun.json`。

**候选端口池**：客户端本地源端口取 **49152–65535** 全段，本机在该段内映射保持原端口，可直接用作打洞候选端口。

**锥形↔锥形打洞复测（用 49152+ 段端口）**：

| 组 | 主机 srflx | 对端 srflx | 结果 | 说明 |
| --- | --- | --- | --- | --- |
| 主机 ↔ 虚机（本地 49372） | `115.150.84.112:49372`（端口保持） | `115.150.84.112:56610`（VMware NAT 重映射，两个 STUN 一致 → EIM） | **失败**，双向 0 包 | 两端同一公网 IP，退化为同出口回环 |
| 主机 ↔ 虚机（本地 49160） | `115.150.84.112:49160`（端口保持） | `115.150.84.112:56763`（EIM，端口重映射） | **失败**，双向 0 包 | 同上，换端口段后结论不变 |

证据：`evidence/p2p/20261110-1745-vm-cone-punch/summary.json`、`evidence/p2p/20261110-1800-vm-cone-punch-49160/summary.json`。

结论：**虚机不是对称型 NAT**（两个 STUN 映射一致），但它与主机共用 `115.150.84.112`，无法构成「两个不同公网出口」的锥形↔锥形对照。该组合的失败原因是同出口回环被路由器丢弃（对照：同网段 host 候选 0% 丢包），不是对称型映射。真正的锥形↔锥形仍需第二个独立公网出口（例如另一条宽带或手机 Wi-Fi 回到锥形出口），**保持未验证**。



## 四、每级行为

### L1 IPv6 直连

1. **本端自检**（不依赖对端）：枚举非 `fe80::/10`、非 `::1` 的 v6 地址；有全局地址（`240e::/20`、`2408::/20`、`2409::/20`、`2001:db8` 除外的一般全局单播）后再对 `ipv6_probe_targets` 各建一次 TCP 连接，任一条成功才算「本端 v6 可用」。
   - 只有地址、连不通 → 判不可用，**不进入 L1**。本机当前就是这一类（见第八节）。
2. **双端判定**：双方各自自检通过，并把 v6 地址作为 host 候选写入 `offer.candidates`，才进入 L1；任一端不可用直接降 L2。
3. **连通检查**：v6 无 NAT，但仍需**双向**先发包——Windows 防火墙与 Android 默认丢弃未请求的入站包，因此 L1 与 L2 用同一套「双向探测 + 状态化放行」逻辑，不能只让一端主动连。
4. **失败判据**：`ipv6_budget_ms` 内未满足成功判据（双向各 ≥2 响应、≥3 个 RTT 样本），或连接被立即拒绝（防火墙/无路由），降 L2，记 `reason=ipv6_unreachable` 与具体错误码。
5. **ULA 处理**：`fc00::/7` 地址只在双方同会话且无全局 v6 时作为后备候选，优先级低于 `srflx`。

### L2 P2P 打洞

1. **候选收集**：STUN 两个服务并行取 srflx（共用同一 socket，保证映射一致性判断有效）+ 枚举 v4 host。
2. **对称型判定**：两个 STUN 返回的映射端口不一致 → 判 address-or-port-dependent，**跳过打洞直接 L3**（实测 16 s 内 0 包，重试无意义）。
3. **同出口判定**：双方 srflx 公网地址相同 → 不尝试公网回环（实测路由器丢弃），只走 host 候选。
4. **候选排序**：host（同网段）> srflx（锥形）> relay；v6 host 属于 L1，不参与 L2 排序。
5. **成功判据与超时**：见第三节参数；单向收到一律判失败。

### L3 中继

1. `relay_allocate` 的预算与档位直接采用 `relay_allocated.budget_kbps` 与 `quality_profile`，不自行猜测。
2. 中继建立后本会话**不再回切直连**，避免抖动与二次切换失败。
3. 中继档位上限由 `RelayPolicy` 决定，客户端不得越级下发直连档位。

### 运行期降级

1. 四条判据任一满足即切中继，切换时同步 `session_start.transport_kind=relay` 并附 `relay_allocated` 结果。
2. 切换后锁定到会话结束；重新建连才重新走完整三层。

## 五、四端分工

### 共享（必须跨端一致，建议由同一份常量表生成各端配置）

| 项 | 说明 |
| --- | --- |
| 参数表 | 第三节全部参数，四端读同一份定义，改动走一处 |
| 判定语义 | 对称型判定、双向成功判据、降级阈值、切换不可逆 |
| 候选排序 | host > srflx > relay，同类型按 RTT |
| 事件与字段 | `connect_level_result`、`degrade` 事件的字段名与单位 |
| 日志口径 | 每级耗时、失败 reason 枚举值保持一致，便于跨端统计 |

### 各端原生实现（不可共享代码，只能对齐行为）

| 平台 | 必须原生实现的部分 | 关键 API / 注意点 |
| --- | --- | --- |
| Windows | v6 地址枚举与前缀判断、防火墙放行、UDP socket 复用 | `GetAdaptersAddresses`；入站默认丢弃，需双向发包建立状态；`IP_UNICAST_IF`/`IPV6_UNICAST_IF` 选网卡 |
| macOS | 同上 + 多网卡（Wi-Fi/有线/雷雳）切换 | `getifaddrs`；`SCNetworkReachability` 监听网络变化；`IPV6_PKTINFO` 收包带目的地址 |
| Linux | 同上 + 策略路由与多出口 | `getifaddrs`/netlink；`IPV6_RECVPKTINFO`；避免只按默认路由选源地址 |
| Android | 网络选择（Wi-Fi/移动数据同时在线）、后台限制、socket 绑定网络 | `ConnectivityManager` + `Network.bindSocket`；检测所选 `Network` 是否具备 v6；Doze/省电策略下需前台服务；不可依赖 `getifaddrs` 判断「当前用哪条网」 |

共性约束：**socket 复用**（同一 UDP 端口完成 STUN 与打洞，否则映射判定失效）四端都得各自实现，无法共享代码。

## 六、接口签名（跨端对齐的最小集合）

```python
@dataclass(frozen=True)
class Candidate:
    kind: str          # "host" | "srflx" | "relay"
    family: int        # 4 | 6      ← 契约缺口，见第九节
    address: str
    port: int
    priority: int      # 数值越大越优先
    related: str       # srflx 时的本地地址, 其余为空

@dataclass(frozen=True)
class LevelResult:
    level: str         # "ipv6" | "p2p" | "relay"
    ok: bool
    duration_ms: float
    reason: str        # 见第七节枚举
    detail: dict       # 每级自带指标, 字段见下
```

实现版签名（`src/rdc/connect/policy.py`）：网络动作全部改为注入，单测用桩替换，判定口径不变。

```python
Prober         = Callable[[int], Ipv6Probe]                  # 入参 deadline_ms
Collector      = Callable[[int], Sequence[Candidate]]       # 入参 deadline_ms
Checker        = Callable[[Sequence[Candidate], int], CheckOutcome]
RelayRequester = Callable[[], Mapping[str, Any]]            # 发 relay_allocate, 返回 relay_allocated 原文

def ipv6_self_check(prober, deadline_ms=1500, clock=monotonic) -> LevelResult
    # 前置: 枚举本机全局 v6 并对外建连; 不读对端状态

def collect_candidates(collector, deadline_ms=1500) -> tuple[Candidate, ...]
    # 并行 STUN×2 + host 枚举; 必须复用同一 UDP socket

def classify_mapping(candidates) -> str
    # "endpoint-independent" | "address-or-port-dependent" | "unknown"

def run_connectivity_checks(peer, checker, budget_ms=5000, clock=monotonic) -> LevelResult
    # 双向探测; 内部按 check_interval_ms 发包, 满足 success_min_* 才算 ok

def decide(ipv6: LevelResult, p2p: LevelResult, mapping: str) -> Decision
    # Decision(transport, reason); transport 取 "direct_ipv6" | "direct_udp" | "relay"

def establish_session(prober, collector, checker, relay_requester, clock=monotonic) -> EstablishResult
    # 按 L1 -> L2 -> L3 编排, 一次返回三级结果与 relay_prefetched 标记

def watch_quality(sample: QualitySample) -> str | None
    # 返回 None 或 "relay"(降级), 判定见第三节阈值; sample 由调用方按窗口聚合
```

差异说明：`decide` 返回 `Decision(transport, reason)` 而不是裸字符串，reason 直接写入 `connect_level_result` / `transport_changed` 事件；`establish_session` 是本策略的编排入口，四端原生实现只需提供上面四个可调用对象。

字段口径（`LevelResult.detail`）：

| level | 必带字段 |
| --- | --- |
| ipv6 | `local_v6`、`probe_targets`、`probe_ok`、`rtt_p50_ms` |
| p2p | `mapping`、`candidates`、`first_packet_ms`、`rtt_p50_ms`、`rtt_p95_ms`、`recv_packets`、`sent_packets`、`loss_pct` |
| relay | `budget_kbps`、`quality_profile`、`connect_ms` |

## 七、事件与日志

- `connect_level_result{level, ok, duration_ms, reason, detail}`：每级一条，无论成败。
- `transport_changed{from, to, reason, metric, value, threshold, window_ms}`：切换事件，字段沿用现有 `QualityEvent`。
- `reason` 枚举固定为：`ok`、`ipv6_no_global`、`ipv6_unreachable`、`symmetric_nat`、`punch_timeout`、`single_direction`、`relay_unavailable`、`degrade_loss`、`degrade_p95`、`degrade_no_frame`、`degrade_no_keyframe`。

## 八、现状标注（实测，不伪造）

| 项 | 现状 | 证据 |
| --- | --- | --- |
| 本机 IPv6 | **无全局地址**；仅有 `fe80::`；IPv6 路由表无 `::/0` | `Get-NetIPAddress` / `Get-NetRoute -AddressFamily IPv6` |
| 本机 v6 是否被禁用 | 否：`ms_tcpip6` 三块网卡均 Enabled，RouterDiscovery/Dhcp 均 Enabled，无 `DisabledComponents` | 同上 |
| 家宽 v6 出站 | **不可用**：对 2 个 `2400:` 段目标各 2 个端口建 TCP 连接，4/4 失败（`WinError 10051` 网络不可达） | `evidence/p2p/20261110-v6-probe.json` |
| 上游路由 | 网关为小米路由器（`192.168.31.1`，登录页可达），邻居表中有 `fe80::4231:3cff:fedd:15fb`（Router 类型），但未产生全局地址与默认路由 | `netsh int ipv6 show neighbors` |
| 运营商 | AS4134 中国电信，公网 IPv4 非 CGNAT；是否下发 v6 前缀需登录路由器查看 WAN 侧，**未验证** | Cymru DNS 反查 |
| ECS IPv6 | 域名无 AAAA 记录；机器内部无法核对（无登录凭据）。需控制台开通，**未验证** | `Resolve-DnsName -Type AAAA` |
| ECS STUN | **已就绪**：5/5 响应、44 B、RTT p50 39.6 ms；映射端口与本地端口一致，判 endpoint-independent | `evidence/p2p/20261110-1730-ecs-stun-x5/stun.json`、`…-1755-…-49160/stun.json` |
| L1 端到端 | **未验证**（两端均无 v6） | — |
| L2 锥形↔锥形 | **未验证**：虚机与主机共用同一公网 IP `115.150.84.112`，不构成第二个出口；该组退化为同出口回环，实测双向 0 包 | `evidence/p2p/20261110-1745-vm-cone-punch`、`…-1800-vm-cone-punch-49160` |
| L2 对称型 | 已实测**不可穿** | 报告 4.5 |
| L2 同出口回环 | 已实测**不可用**（路由器不支持 hairpin），同网段 host 候选可用 | 报告 4.2 / 4.4 |

L1 前置条件：家宽开通 IPv6（路由器启用原生/DHCPv6 双栈，运营商侧下发前缀）。在此之前 L1 一律跳过，直接 L2。

## 九、契约缺口（只记录，不修改正文）

1. **`candidate` 无 `family` 字段**：契约只定义 `kind`（host/srflx/relay）与 `address`/`port`。IPv6 候选只能靠地址字符串猜族，候选排序与防火墙策略无法按族区分。建议 v2 增补 `family`（4/6）并说明 v6 候选不参与 srflx 语义。
2. **`chosen_candidate` 为单值**：无法表达「选中后又被替换」，运行中 L1→L2 或 direct→relay 的切换目前只能靠新的 `session_start`，契约未定义切换信令。
3. **`session_start.transport_kind` 只有 direct / relay / none**：`direct` 无法区分 IPv6 与 UDP 打洞直连。本策略暂时用 `LevelResult.level` 在客户端内部记录，不对外暴露。

## 十、代码落地与桌面端接入点（函数级）

已落地文件：

| 文件 | 内容 |
| --- | --- |
| `src/rdc/connect/__init__.py` | 包声明，满足目录结构要求 |
| `src/rdc/connect/policy.py` | 18 项共享参数、候选/结果数据类、`decide()` 与全部配套函数、`TransportMonitor`、中文文案映射 |
| `src/rdc/connect/native.py` | 真实本机能力：Windows `GetAdaptersAddresses` 枚举单播地址、全局 v6 过滤、UDP connect 验路由、IPv6 自检 |
| `src/rdc/connect/stun.py` | RFC 5389 Binding 请求/响应解析、端口池绑定、800 ms 单服务预算 / 1.5 s 总预算重试、srflx 收集 |
| `src/rdc/connect/punch.py` | 双向 UDP 打洞探测（`RDCP1` + `Q`/`R` + 令牌），100 ms 发包、20 ms 轮询，统计双向响应与 RTT |
| `src/rdc/connect/relay.py` | TLS 1.3 信令通道（信任锚 `deploy/relay/ca.crt`、SNI `relay.wuge.fun`）、设备鉴权、offer/answer 候选交换、`relay_allocate` |
| `src/rdc/connect/runtime.py` | 真实装配：端口池 socket 复用（STUN 与打洞同 socket）、候选交换、注入 `establish_session` |
| `src/rdc/commands/service.py` | 命令层接线：`connect_mode="real"` 时后台线程调用 `establish_real_session`，落 `transport_kind` 与中文原因 |
| `tests/test_connect_policy.py` | 29 个用例，全部注入桩，不建真实 socket |
| `tests/test_connect_runtime.py` | 15 个用例，覆盖 STUN 解析、探测包编解码、v6 过滤、中文文案、命令层接线与空候选不自打洞；全部注入桩 |

接入点（不改 UI 文件，只说明调用位置与触发时机）：

1. **建连入口（已接线）**：`rdc.commands.api.start_connection(device_id, access_code)` → `CommandService.start_connection`（`src/rdc/commands/service.py`）。`connect_mode="real"`（`commands/api.py` 默认值）或注入 `connector` 时，任务进入 `running` 并由后台线程执行 `establish_real_session`；`stub` 模式保持原行为，任务停在 `pending`，单测不建 socket。
2. **结果落库（已接线）**：`EstablishResult.decision.transport` 映射到会话状态——`relay` → `transport_kind="relay"` 且 `quality_profile="720p30"`，`direct_ipv6` / `direct_udp` → `transport_kind="direct"`、`quality_profile="auto"`；`relay_unavailable` 不建会话，任务判 `failed` 并写中文原因。进度经 `get_connection_progress` 的 `data.message` 返回。
3. **运行期监控**：会话采样聚合后（建议 200 ms 一次，窗口长度取 `degrade_loss_window_s` / `degrade_p95_window_s`）构造 `QualitySample` 交给 `TransportMonitor.observe`。返回 `relay` 时触发一次单向切换（新 `session_start` + `relay_allocate`），之后 monitor 锁定，后续采样一律忽略，不回切。
4. **画质联动**：`CommandService.apply_quality_profile` 已按 `transport_kind` 限制档位（relay 只放行 `RELAY_ONLY_PROFILES`），本策略不改它，只负责把 `transport_kind` 写正确。
5. **真实现已接线**：`prober` = `native_ipv6_probe`，`collector` = `collect_real_candidates`（host 候选 + 同 socket srflx），`checker` = `UdpPunchChecker`，`relay_requester` = `request_relay_allocation`（固定 720p30 档，`requested_kbps=2900`）；仍保留桩开关（构造 `CommandService(connect_mode="stub")` 或注入 `connector`），单测不建真 socket。

### 失败原因与中文文案

| 场景 | `reason` | 展示文案 |
| --- | --- | --- |
| IPv6 直连成功 | — | 已建立 IPv6 直连 |
| UDP 打洞成功 | — | 已建立 P2P 直连（UDP 打洞） |
| 直连失败、中继成功 | — | 直连不可用，已转中继（720p30，`budget` kbps） |
| 中继返回但无预算字段 | — | 直连不可用，已转中继（720p30，预算未返回） |
| 直连与中继都失败 | `relay_unavailable` | 直连与中继均不可用：中继申请失败，请检查网络后重试 |
| 后台线程异常（网络/配置/凭据） | — | 连接失败：`异常摘要`；请检查网络后重试 |
| 本机无全局 v6 | `ipv6_no_global` | 本机无可用全局 IPv6（日志级，触发下一层） |
| IPv6 不可达 | `ipv6_unreachable` | IPv6 直连不可达（日志级） |
| 对端对称型 NAT | `symmetric_nat` | 对端为对称型 NAT，无法打洞（日志级） |
| 只有单向可达 | `single_direction` | 打洞只有单向可达（日志级） |
| 打洞超时 | `punch_timeout` | 打洞超时（日志级） |
| 无对端候选 | `no_candidate` | 没有可用的对端候选（日志级） |

### 真机实测进度（2026-11-10）

| 项 | 状态 |
| --- | --- |
| 本机网卡枚举（`native.py`） | 实测：3 个 v4 单播地址 + `fe80::` 链路本地地址；无全局 v6 |
| IPv6 自检（`ipv6_self_check`） | 真机落在「无全局 IPv6 地址」，L1 端到端**未验证** |
| STUN srflx（`stun.py`） | 实测可用：`47.109.32.249:3478` 双服务 5/5 响应，44 B，RTT 37–41 ms，映射端口与本地端口一致 |
| 中继鉴权 + `relay_allocate`（`relay.py`） | 实测成功：TLS 1.3 握手、register→challenge→auth→auth_ok、返回 `budget_kbps=2900`、`quality_profile=L0`、`relay_token`、`endpoint` |
| 中继槽位回收 | 中继 `max_sessions=1`，成功后需 `session_end` 或等待授权过期才释放；本轮并发占用时真实返回「中继通道已满」 |
| 双向打洞（`punch.py`） | 代码就绪；锥形↔锥形仍**未验证**（缺第二个独立公网出口），同出口回环已实测不可用 |
| 命令层真实模式（`start_connection`） | 单测覆盖（注入 connector）；真机端到端在建连时走 `exchange_candidates`，无对端在线时按 `no_candidate` 转中继，属诚实路径 |

## 十一、桩覆盖 vs 实测对照（不得混写）

**桩测试只证明判定逻辑，不代表真机通路打通。** 本机当前无全局 IPv6，锥形↔锥形也未取得第二个独立公网出口，对应路径仍为「未验证」。

| 路径 | 代码级验证（注入桩） | 真机实测 | 口径 |
| --- | --- | --- | --- |
| L1 IPv6 直连判定与编排 | `test_only_ipv6_available_takes_l1`、`test_decide_prefers_ipv6` | **未验证**（本机无全局 v6） | 代码路径通过，真机通路未验证 |
| L1 本端自检四态 | `test_ipv6_self_check_requires_global_and_reachable`、`test_ipv6_probe_error_degrades` | 本机实测落在「无全局地址」分支；端到端**未验证** | 单测覆盖判定，真机仅观测到不可用 |
| L2 锥形↔锥形打洞 | `test_ipv6_unavailable_falls_to_punch` | **未验证**：虚机与主机共用公网 IP，退化为同出口回环，双向 0 包 | 桩覆盖，真机未验证 |
| L2 对称型跳过打洞 | `test_symmetric_nat_skips_punch` | 已实测**不可穿**（报告 4.5） | 实测 + 桩 |
| L2 打洞超时切中继 | `test_punch_timeout_falls_back_to_relay` | 中继通路有既有实测；打洞超时切换未做真机演练 | 桩覆盖 |
| 候选为空 | `test_empty_candidates_go_relay` | 未单独实测 | 桩覆盖 |
| STUN 超时 | `test_stun_timeout_goes_relay` | ECS STUN 5/5 成功（成功路径实测）；超时路径未实测 | 成功实测，超时桩覆盖 |
| 同出口回环判定 | `test_same_egress_detection` | 已实测**不可用**（报告 4.2 / 4.4） | 实测 + 桩 |
| 运行期劣化降级（4 阈值） | `test_watch_quality_triggers_on_each_threshold`、`test_watch_quality_stays_below_thresholds` | 未做真机劣化注入 | 桩覆盖 |
| 降级后不回切 | `test_monitor_degrades_once_and_never_rolls_back` | 未验证 | 桩覆盖 |
| 中继不可用 | `test_relay_unavailable_is_reported` | 实测：中继槽位被占用时返回「中继通道已满」，命令层转 `relay_unavailable` | 实测 + 桩 |
| 中继预取标记 | `test_relay_prefetch_flag_after_budget` | 未验证 | 桩覆盖（只记录 `duration_ms` 标记，不是真实并行预取） |
| 真实 STUN 响应解析 | `test_stun_response_parses_xor_mapped_address`、`test_stun_response_rejects_wrong_transaction` | 实测：ECS STUN 5/5 响应，44 B，端口保持 | 解析 + 成功路径实测；超时路径桩覆盖 |
| 打洞探测包编解码 | `test_probe_codec_roundtrip_and_rejects_foreign_packet` | 双向打洞真机**未验证**（缺第二出口） | 桩覆盖 |
| 全局 v6 过滤 | `test_global_v6_filter`（7 组地址） | 本机实测：仅有 `fe80::`，无全局 v6 | 单测 + 真机观测 |
| 命令层 real 模式落状态 | `test_real_mode_records_transport_and_chinese_message`、`test_real_mode_reports_relay_failure_in_chinese`、`test_real_mode_wraps_exception_with_retry_hint` | 单测注入 connector；真机无对端时转中继 | 桩覆盖，真机端到端未验证 |
| 空候选不自打洞 | `test_empty_peer_candidates_skips_punch_and_falls_to_relay` | 真机：无对端在线时 `exchange_candidates` 返回空，直接转中继 | 桩 + 真机观测 |

复跑命令：`python -m pytest tests/test_connect_policy.py tests/test_connect_runtime.py -q`（44 passed）。
