# IPv6 直连与 UDP 打洞 · 1.0.0 施工规格

| 项目 | 内容 |
|---|---|
| 规格编号 | spec-ipv6-punch-20261111 |
| 适用版本 | 1.0.0 |
| 状态 | 已定稿（C1/C2 已裁决「不改」；仅规格，不含产品代码） |
| 依据 | 本文件第 1 节已实测事实、[desktop-v1](../../contracts/desktop-v1.md)、[protocol-v1](../../contracts/protocol-v1.md)、[connect-v1](../../design/connect-v1.md) |
| 产出物 | 本文件；实现代码不在本次范围 |

## 0. 结论摘要

1. 被控端监听改为**双栈双 socket、同端口 27888、`IPV6_V6ONLY=1`**；IPv4 监听路径与今天逐字节一致。
2. **IPv6 直连不新增数据面**：复用现有 TCP 会话与 AEAD 帧；`IPv6 无 NAT，不需要打洞`，打洞只服务 IPv4。
3. IPv4 打洞新增 UDP 数据面封装：16 字节头、1200 字节数据报、1184 字节分片，原样承载现有 AEAD 帧；控制帧可靠重传，视频帧不重传。
4. 安卓 1.0.0 做四件事：IPv6 地址语法、输入框长度 40→64、TLS 从硬编码改为配置驱动、**手机完整参与 IPv6 直连 + IPv4 打洞**。
5. **范围变更（撤销旧的「手机打洞进 1.0.1」）**：1.0.0 手机必须能打洞；安卓与桌面使用**同一套** L1/L2/L3 预算、候选优先级、UDP 帧格式与判据，不允许出现「手机版口径」。手机仍不做 TURN、UPnP、IPv6 UDP 打洞（IPv6 无 NAT，直连走 TCP）。
6. **契约裁决（已定，不再留待裁决）**：C1/C2 两处追加按**不改**执行，`desktop-v1` 保持冻结、零变更；IPv6/打洞的证据只落结构化日志与验收取证文件，1.0.1 再评估是否入契约。

## 1. 已确认事实（直接引用，不再重复验证）

| 事实 | 证据位置 |
|---|---|
| 被控端监听为 `AF_INET`，仅 IPv4 | [server.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/relay/server.py#L603) |
| 被控端 TCP 端口默认 27888，`bind` 默认 `""`（仅 v4 通配） | [host.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/session/host.py#L61-L88) |
| 安卓输入框上限 40 字符 | [MainActivity.kt](file:///c:/Users/Administrator/Desktop/RemoteDesktop/android/app/src/main/java/com/rdc/remote/MainActivity.kt#L395-L404) |
| 安卓用 `split(":")` 取前两段，IPv6 必被截断 | [ControlContract.kt](file:///c:/Users/Administrator/Desktop/RemoteDesktop/android/app/src/main/java/com/rdc/remote/ControlContract.kt#L94-L109) |
| 安卓只发一条 `kind=relay` 候选、`priority=1` | [LanSession.kt](file:///c:/Users/Administrator/Desktop/RemoteDesktop/android/app/src/main/java/com/rdc/remote/LanSession.kt#L234-L250) |
| 安卓 TLS 仅在 `host == "47.109.32.249"` 时启用 | [LanSession.kt](file:///c:/Users/Administrator/Desktop/RemoteDesktop/android/app/src/main/java/com/rdc/remote/LanSession.kt#L356-L368) |
| 安卓无 STUN、无 UDP socket、无 IPv6 候选、无直连/中继回退 | 全仓检索无命中 |
| 客户端 endpoint 解析用 `rpartition(":")`，裸 IPv6 会被切错 | [service.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/commands/service.py#L310-L316) |
| Python 已有 IPv6 直连 → 打洞 → 中继策略层 | [policy.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/connect/policy.py)、[runtime.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/connect/runtime.py) |
| 候选线格式只有 `kind/address/port/priority`，无 `family` | [relay.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/connect/relay.py#L234-L253) |
| AEAD 帧为 `4 字节长度前缀 + 4 字节前缀 + 8 字节计数 + 密文`，重放窗口 64 | [crypto.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/session/crypto.py) |
| AEAD 计数器与重放窗口天然适配 UDP（乱序可解） | 同上 |
| 视频帧实测最大 90000 字节（1080p 关键帧），必须分片 | [视频帧封装v2实测报告](file:///c:/Users/Administrator/Desktop/RemoteDesktop/docs/验收/视频帧封装v2实测报告-2026-11-10.md) |

网络侧事实（用户实测）：双层 NAT、映射 endpoint-independent、端口保留偏移 0、映射 120 秒不漂移；UPnP / NAT-PMP / PCP 全不可用；主机与虚机均有 `240e` 全局地址与默认路由。

## 2. 路径定义

| 路径 | 组成 | 数据面 | 1.0.0 是否新增实现 |
|---|---|---|---|
| **T1 直连** | 控制端 → 被控端 `[v6]:27888` 或 `v4:27888` | 现有 TCP + AEAD | 仅监听与选路 |
| **T2 打洞** | 信令交换候选 → 双向 UDP 探测 → UDP 会话 | 新增 UDP 封装承载同一 AEAD | 是（桌面↔桌面、手机↔桌面同口径） |
| **T3 中继** | 现有 47.109.32.249:8750 | 现有 TLS 中继 | 否 |

选路层级沿用 connect-v1 的 L1/L2/L3，但把 L1 的探测手段改为 TCP 建连（理由见 §4.2）。

## 3. 候选对象 schema

### 3.1 字段

| 字段 | 类型 | 必填 | 取值范围 | 说明 |
|---|---|---|---|---|
| `kind` | string | 是 | `host` / `srflx` / `relay` | **线上枚举不得新增**；`mapped` 为本地态，不直接上线（见 §3.4） |
| `address` | string | 是 | IP 字面量或主机名 | **不带方括号**；IPv6 形如 `240e:370:6303:cb0::1` |
| `port` | int | 是 | 1–65535 | TCP 或 UDP 端口，由路径决定（T1 为 TCP，T2 为 UDP） |
| `priority` | int | 是 | 0–100，大为先 | 见 §3.2 |
| `family` | int | 否 | `4` / `6` / `0` | 新增；`0` 表示非字面量（主机名），只能走中继 |
| `scope` | string | 否 | `global` / `ula` / `link` / `private` / `""` | 新增；仅 IPv6 有意义 |
| `origin` | string | 否 | `host` / `stun` / `mapped` / `relay` | 新增；本地态 `mapped` 经此字段上线 |
| `related` | string | 否 | IP 字面量或 `""` | 新增；srflx 的本地基地址，仅诊断用 |
| `proto` | string | 否 | `tcp` / `udp` / `""` | 新增；缺省按 `kind` 推断（relay=tcp，其余=udp，host 双栈时由路径决定） |

约束：

- `kind=relay` 时 `family` 必须为 `4`，`priority` 固定 10。
- `kind=host` 且 `family=6` 时 `scope` 必须为 `global` 或 `ula`；`link` 候选不得上线（无 zone id 不可达）。
- `kind=srflx` 必须带 `related`；`origin=stun`。
- 同一 `(kind, address, port)` 只允许出现一次；重复按优先级高者保留。

### 3.2 优先级

| 候选 | family | scope | priority |
|---|---|---|---|
| IPv6 全局 host | 6 | global | 100 |
| IPv4 私网 host（同网段可达） | 4 | private | 96 |
| 映射外部地址（UPnP/PMP，本网不可用） | 4 | private | 95 |
| 映射回读地址 | 4 | private | 92 |
| srflx | 4 | — | 90 |
| IPv4 公网 host | 4 | — | 85 |
| IPv6 ULA host | 6 | ula | 70 |
| relay | 4 | — | 10 |

- 本网 UPnP/NAT-PMP/PCP 全不可用，`origin=mapped` 的候选预期为空，代码路径保留但不得因缺失而报错。
- 排序键沿用 `(-priority, family, address, port)`；同分时 family 6 不特殊化，保持现有 `_candidate_sort_key` 行为，避免无关变更。

### 3.3 序列化

信令 `offer.candidates[]` 与 `answer.candidates[]` 元素均为 JSON 对象：

```json
{
  "kind": "host",
  "address": "240e:370:6303:cb0:15f2:306a:ed29:2e5d",
  "port": 27888,
  "priority": 100,
  "family": 6,
  "scope": "global",
  "origin": "host",
  "related": "",
  "proto": "tcp"
}
```

```json
{
  "kind": "srflx",
  "address": "106.12.251.193",
  "port": 49188,
  "priority": 90,
  "family": 4,
  "scope": "",
  "origin": "stun",
  "related": "192.168.1.23",
  "proto": "udp"
}
```

文本形态（仅界面/日志，不上线）：`[240e::1]:27888`；裸 IPv6 在界面中等价于 `[240e::1]:27888`。

### 3.4 兼容规则（老客户端不得崩）

1. **只增不改**：不删除、不重命名字段，`kind` 与 `priority` 语义不变。
2. 接收方 MUST 忽略未知键；Python 用 `dict.get`，Kotlin 用 `JSONObject.opt*`，不得用严格反序列化（不得因未知键抛异常而中断会话）。
3. 新字段全部可选；旧 offer 无 `family` 时，新接收方按 `ipaddress.ip_address` 推断，失败则 `family=0`，仅参与中继。
4. `answer.chosen_candidate` 保持原结构，可携带新可选字段；旧客户端忽略即可。
5. `mapped` 候选上线时改写为 `kind="srflx"` + `origin="mapped"` + `related`，旧端按公网反射候选处理，语义正确；**禁止**出现 `kind="mapped"`。
6. `protocol-v1` 的 `v` 不升版；本次不新增消息类型。
7. `answer` 新增可选 `candidates` 数组，供打洞双向使用；缺失时退回单候选逻辑（旧行为）。

## 4. 路径选择

### 4.1 顺序与预算

| 层 | 顺序 | 每候选超时 | 层总预算 | 并发 |
|---|---|---|---|---|
| L1 IPv6 直连 | 按 §3.2 排序，仅 `family=6` | 800 ms/次，最多 2 次 | 1500 ms | 与 L2 不并发 |
| L2 IPv4 打洞 | 按 §3.2 排序，`family=4` | 2000 ms/候选对 | 5000 ms | 进入 L2 2 s 后并行预取中继 |
| L3 中继 | 现网 8750 | 8000 ms | 8000 ms | 预取命中则直接切换 |

- L1 与 L2 串行；L3 预取与 L2 并行，预取失败不影响 L2 继续。
- 总建连预算：L1+L2 = 6500 ms；含中继最坏 14500 ms。
- `get_connection_progress.total` 维持 4 阶段（采集候选 / IPv6 / 打洞 / 中继），不因新增流程改契约数据。

### 4.2 每层判据

**L1（IPv6 直连）**

- 判据：TCP 三次握手成功 1 次即通过（SYN/SYN-ACK 已证明双向可达），不套用打洞的「≥2 响应、≥3 样本」。
- 理由：IPv6 无 NAT，不存在映射；对端防火墙由本次出站 SYN 建立 conntrack。TCP 探测与最终会话同协议同端口，不会出现「UDP 通但 TCP 不通」的假阳性。
- 失败原因码：`ipv6_no_global`（本机无 v6 全局地址）、`ipv6_unreachable`（全部候选建连失败/超时）、`ipv6_disabled`（配置关闭）。
- 本机无 v6 地址时直接跳过，耗时 0。

**L2（IPv4 打洞）**

- 沿用 `UdpPunchChecker` 双向判据：每方向 ≥2 个响应、RTT 样本 ≥3。
- 优先 `srflx` 对 `srflx`；其次任一方 srflx 对另一方 host；同私网 host 对 host 先于公网。
- 排除：双端公网地址相同（同一出口 NAT）时剔除该对，记 `hairpin_blocked`。
- 失败原因码：`no_candidate`、`symmetric_nat`（映射非 EIM 且无 mapped）、`single_direction`、`punch_timeout`。

**L3（中继）**

- 既有分配成功即建立；失败返回既有 `RELAY_UNAVAILABLE` 文案，不新增错误码。

### 4.3 回落中继的判据

满足任一即进入下一层：

1. 当前层预算耗尽且无候选通过。
2. 双向判据不达标（打洞）。
3. 通路建立后 3 s 内无首个 AEAD 帧（会话握手超时）。
4. 运行期劣化触发（沿用集中阈值：丢包/时延/连续解码失败），单向切换，不回落。

### 4.4 日志与可断言字段

必须输出结构化事件（JSON lines，UTF-8，不含访问码/令牌）：

```json
{"event":"connect.level_result","level":"L1","transport":"direct_ipv6","ok":true,"reason":"ok","candidates":2,"tried":1,"elapsed_ms":214,"rtt_ms":[27.4]}
{"event":"connect.path_selected","transport":"direct_ipv6","peer_address":"240e:370:6303:cb0:15f2:306a:ed29:2e5d","peer_family":6,"peer_port":27888,"relay_used":false,"local_port":53422,"reason":"ok"}
```

断言规则（验收脚本据此判定，不靠肉眼）：

- `transport == "direct_ipv6"`；
- `peer_address` 以 `240e:` 开头且为合法 IPv6 字面量（无方括号）；
- `relay_used == false`；
- 会话期间进程的连接表中**不存在**到任何中继端点（`47.109.32.249:8750`、STUN `:3478`）的已建立连接；
- 上述四项全部成立才输出 `{"assert_direct_ipv6":{"pass":true,...}}`，任一不成立即 `pass:false` 并注明实际值。

## 5. 数据面

### 5.1 决策：IPv6 直连走 TCP，打洞只服务 IPv4

**论证**：IPv6 端到端无地址转换，全局地址可直接寻址，不存在「外网端口与内网端口映射」问题；打洞解决的是 NAT 映射不可预知，而该问题在 IPv6 上不存在。若强行让 IPv6 也走 UDP，只会额外引入分片、重传、拥塞控制三套复杂度，收益为零。

**取舍**：

| 方案 | 优点 | 代价 | 结论 |
|---|---|---|---|
| IPv6 走现有 TCP | 零新增数据面、零回归风险、可直接复用 AEAD 与会话状态机 | 头部阻塞、抗丢包弱于 UDP | **采用** |
| IPv6 也走 UDP | 统一数据面 | 新增分片/重传/拥塞，视频与握手全部重测 | 不采用（1.1 再评估） |
| IPv4 打洞后仍走 TCP | 无新封装 | NAT 下 TCP 直连不可达，打洞无意义 | 不成立 |

因此：T1 使用现有 TCP/AEAD；T2 使用本节定义的 UDP 封装；T3 不涉及。

### 5.2 UDP 帧格式

每个 UDP 数据报最大 1200 字节，头部固定 16 字节：

| 偏移 | 长度 | 字段 | 说明 |
|---|---|---|---|
| 0 | 1 | `ver_type` | 高 4 位版本=1，低 4 位类型：1=DATA，2=PING，3=PONG，4=ACK |
| 1 | 1 | `flags` | bit0=末分片；bit1=可靠帧（需 ACK）；bit2=控制帧（握手 JSON） |
| 2 | 2 | `frag_index` | 从 0 开始，大端 |
| 4 | 2 | `frag_count` | 本帧分片总数，大端 |
| 6 | 2 | `frag_len` | 本片负载字节数，大端 |
| 8 | 4 | `frame_id` | 发送方单调递增，同一帧所有分片相同 |
| 12 | 4 | `frame_len` | 整帧字节数，用于预分配与上限校验 |

- 负载上限 = 1200 − 16 = **1184 字节**。
- 单帧上限 6 MiB（6291456），对应最大分片数 5314，`frag_count` 超过 5314 直接丢弃并计数。
- 载荷 = 现有 AEAD 帧去掉 4 字节长度前缀后的字节（TCP 的长度前缀在 UDP 由 `frame_len` 表达）。握手完成前为明文 JSON 控制帧，完成后为密文。
- 发送端 1.0.0 **不得交错**两个帧的分片；接收端仍须支持最多 8 个 `frame_id` 并发重组以保证健壮性。

### 5.3 MTU 与分片

- 固定数据报 1200 字节，不做 PMTUD；IPv6 最小 MTU 1280、典型 v4 1500，1200 对两者均安全。
- 不依赖 IP 层分片：IPv6 路由器不分片，任何超过路径 MTU 的数据报会被丢弃；应用层分片是唯一可靠方案。
- 实测 90 KB 关键帧 → 77 个分片，属正常量级；重组表按 `frame_id` 键控，容量 64 MiB。
- 发生 `EMSGSIZE` 时按 1100 字节重试一次并记 `udp_mtu_downgrade`，仍失败则该帧丢弃。

### 5.4 重传

| 类型 | 可靠性 | 策略 |
|---|---|---|
| 视频帧（DATA，flags bit1=0） | 不可靠 | 不重传；缺片即整帧丢弃，不阻塞后续帧 |
| 控制帧（握手、剪贴板、文件元数据，bit1=1） | 可靠 | 接收方回 ACK（`frame_id`）；RTO 120/240/480 ms，最多 3 次；3 次未 ACK → 判通路失效 → 中继 |
| PING/PONG | 不可靠 | 1 s 一次；PONG 回显 `frame_id` 与时间戳 |

- 重组超时 **100 ms**（≥3 个 30fps 帧间隔）；超时清理并 `udp_reasm_drop += 1`。
- 连续丢 2 帧或丢失关键帧 → 走既有「请求关键帧，上限 3 次」逻辑；仍失败 → 中继。

### 5.5 心跳与失效

- 每 1 s 发 PING；3 s 内未收到任何合法数据报（含 DATA/PONG）判 `path_dead` → 切换中继。
- 心跳同时用于维持 NAT 映射：映射实测 120 s 不漂移，1 s 心跳远小于该窗口；若重新 STUN 得到的外部端口变化，立即判 `mapping_changed` 并按 §9.2 处理。
- UDP socket 需 `SO_RCVBUF`/`SO_SNDBUF` ≥ 4 MiB，避免 1080p 突发被内核丢弃。

### 5.6 乱序与重放

- 分片级：按 `frag_index` 定址重组，天然容忍乱序与重复分片（重复覆盖同槽位）。
- 帧级：重组完成的整帧交给现有 `AeadSession` 解密；其 4 字节前缀 + 8 字节计数器 + 64 帧重放窗口已能接受乱序。
- UDP 适配层对「计数器越界/重放」**只丢帧并计数**（`udp_replay_drop`），不关闭会话；连续 100 帧全部失败才判通路失效。TCP 路径行为不变（仍按原逻辑断开）。
- 安全：UDP socket 建立后立即 `connect()` 锁定对端，内核过滤其他源地址；握手完成前只接受本端候选列表内的地址；握手完成后丢弃一切明文 DATA。

### 5.7 安全边界

- 打洞探测包（`RDCP1`）不携带凭据，仅用于开路；会话握手仍在 UDP 内完成，沿用访问码证明与 AEAD 密钥派生。
- 会话建立后，未通过 AEAD 校验的数据报一律丢弃并计数。
- 日志只记录地址与端口，不记录访问码、令牌、密钥。

## 6. 双栈监听

### 6.1 方案对比

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| 单 socket 双栈 | AF_INET6 + `IPV6_V6ONLY=0`，bind `[::]:27888` | 一个 accept 循环 | v4 对端变为 `::ffff:a.b.c.d`，需全链路归一化；依赖 OS 默认 `bindv6only`，Linux 可被 sysctl 改掉；v4 行为不再与今天一致 |
| 双 socket 同端口 | AF_INET6（`V6ONLY=1`）绑 27888 + AF_INET 绑 27888 | v4 路径逐字节不变；v6 失败不影响 v4；地址族清晰 | 两个 accept 循环 |

### 6.2 决策

**采用双 socket、同端口 27888、`IPV6_V6ONLY=1`。**

- 两个 socket 都设 `SO_REUSEADDR`；v6 socket 必须显式 `setsockopt(IPPROTO_IPV6, IPV6_V6ONLY, 1)`，不依赖默认值。
- `HostConfig` 新增 `bind_v6: str = "::"` 与 `enable_ipv6: bool = True`；`bind` 语义不变，仅作用于 v4。
- `RelayConfig` 对应新增 `bind_v6`、`enable_ipv6`（带默认值，追加在字段末尾，不破坏现有构造）。
- 两个 listener 共用同一连接处理函数与会话注册表；会话记录增加 `peer_family`。
- 端口同号的收益：安卓与桌面只需记一个端口；防火墙规则一条。

### 6.3 失败隔离

- v6 bind 失败（EADDRINUSE、EAFNOSUPPORT、无 v6 栈）→ 记 `relay.ipv6.bind_failed`，**继续以 v4 提供服务**，`start_hosting` 不得失败。
- v4 bind 失败 → 维持现有行为（启动失败并报错）。
- 健康检查端口维持回环 v4，不改。

## 7. 安卓端

> **范围（已变更）**：1.0.0 手机必须能做 IPv6 直连与 IPv4 打洞，与桌面同一套口径；手机仍不做 TURN / UPnP / IPv6 UDP 打洞。

### 7.1 地址语法

输入框接受三种形态，解析顺序固定：

| 输入 | 解析结果 | 默认端口 |
|---|---|---|
| `[240e:370::1]:27888` | host=`240e:370::1`，port=27888 | 显式 |
| `[240e:370::1]` | host=`240e:370::1`，port=27888 | 27888 |
| `240e:370::1`（≥2 个冒号、无方括号） | host=`240e:370::1`，port=27888 | 27888（**不是 8750**） |
| `192.168.1.10:27888` | 现状不变 | — |
| `192.168.1.10` | 现状不变 | 8750（中继默认） |

- 方括号仅用于分隔端口，解析后必须剥离；`address` 字段不带方括号。
- 拒绝 `%zone`（如 `fe80::1%wlan0`）：1.0.0 不支持链路本地。
- 解析失败提示文案沿用现有「地址格式不正确」，不新增文案。

### 7.2 输入框

- `InputFilter.LengthFilter(40)` → **64**（最长 IPv6 字面量 45 + `[]` + `:65535` = 52，留余量）。
- 键盘类型与等宽字体不变；不做自动补全。

### 7.3 TLS 策略（直连与中继分开）

现状 `host == "47.109.32.249"` 改为按「传输类型 + 端点配置」判定，不再对直连端点误开 TLS：

| 传输 | TLS | 依据 |
|---|---|---|
| `relay_tcp` | 按 `TlsPolicy`：`AUTO`/`ON` 开，`OFF` 关 | 现有 `RelayTls`（TLS 1.3 + SNI） |
| `direct_tcp`（IPv6 / 局域网） | **不开** | 被控端无证书；会话由 AEAD 保护 |
| `direct_udp`（打洞） | **不开** | UDP 无 TLS 层；会话由同一 AEAD 保护 |

```kotlin
enum class TlsMode { AUTO, ON, OFF }

class TlsPolicy(private val relayHosts: Set<String>, private val mode: TlsMode) {
    // 只有中继传输可能启用 TLS; 直连端点一律不走 TLS
    fun shouldUseTls(transport: Transport, host: String): Boolean = when {
        transport != Transport.RELAY -> false
        mode == TlsMode.OFF -> false
        mode == TlsMode.ON -> true
        else -> host in relayHosts
    }
}
```

- `relayHosts` 来自设置项，默认由默认中继端点派生（默认仍为 `47.109.32.249`），可增删。
- 直连时即使 `mode=ON` 也不启用 TLS，并记 `connect.tls_ignored_direct`（仅日志，不阻断）。
- 不新增 UI 开关；沿用现有设置读写。
- 焦点：直连握手为明文访问码证明，媒体/输入由 AEAD 保护；风险与桌面 §9.1 同一条，由 S01 复核。

### 7.4 offer / answer 候选编码与容错

Kotlin 数据类（字段与 §3.1 一一对应，全部有默认值）：

```kotlin
data class Candidate(
    val kind: String = "relay",        // host / srflx / relay（线上不出现 mapped）
    val address: String = "",
    val port: Int = 0,
    val priority: Int = 0,
    val family: Int = 0,               // 4 / 6 / 0=非字面量
    val scope: String = "",
    val origin: String = "",
    val related: String = "",
    val proto: String = "",
)
```

- **发送**：offer 携带全部本地候选（host v4、srflx、relay），字段按 §3.3 编码；IPv6 host 候选 `kind=host/family=6/priority=100`；srflx `kind=srflx/priority=90`；relay `priority=10`。
- **接收**：只用 `optString` / `optInt` / `optJSONArray`，未知键忽略，缺失键取默认值；**禁止** `getString` 严格读取。
- `family` 缺失时按地址字面量推断（`InetAddress` 解析失败则为 0，仅参与中继）。
- `answer.candidates` 缺失或为空 → 退回现有「单候选」逻辑，用 `chosen_candidate` 建连，不报错。
- `kind` 未知值（含 `mapped`）→ 视为 `relay` 处理并记日志，不崩溃。
- `answer.chosen_candidate.kind != relay` 时走 P2P 会话握手；`== relay` 时走现有 relay_allocate + relay_hello 流程。
- `transport_kind` 取 `direct`/`relay` 两个既有值，不新增枚举。

### 7.5 STUN 与候选采集（安卓）

- 采集发生在信令 offer 之前，预算 **1500 ms**；超时则只发 relay 候选。
- 单个 `DatagramSocket`（IPv4，端口由系统分配）贯穿 STUN → 打洞 → 数据面，保证映射端口不变。
- 网络绑定：通过 `ConnectivityManager` 取当前 `Network`，用 `network.bindSocket(socket)` 绑定；注册 `NetworkCallback`，网络切换时中止当前尝试、重新采集并重连（不静默沿用旧 socket）。
- STUN 按 RFC 5389：Binding Request、事务 ID 随机 96 位、解析 `XOR-MAPPED-ADDRESS`；超时 500 ms、重试 3 次，两个不同 IP 的 STUN 服务器并行（取配置，默认与桌面一致）。
- **映射行为判定**：分别向两个服务器取映射端点，相同 → `EIM`，可打洞；不同 → `SYMMETRIC`，跳过 L2 直接中继，不重试。
- host 候选：枚举 `NetworkInterface`，排除回环、链路本地与未绑定网络；v4 私网 96 / 公网 85，v6 全局 100（仅用于 TCP 直连候选）。
- 不做 UPnP / NAT-PMP / PCP / TURN。
- 需要 `ACCESS_NETWORK_STATE` 权限（`INTERNET` 已有）。

### 7.6 UDP 打洞（安卓）

- 探测帧、节奏与判据与桌面完全一致：`RDCP1` 每 100 ms 一次；每候选对 2000 ms；层总预算 5000 ms；每方向 ≥2 个响应、RTT 样本 ≥3 才判成功。
- 候选对顺序按 §3.2；双端公网地址相同（同一出口）剔除，记 `hairpin_blocked`。
- 控制端发起，被控端只应答但同样向对端候选发包（双向开路）；成功后 `socket.connect(peer)` 锁定对端。
- 通路成立后先发可靠 `punch_ok`，随后在 UDP 上跑 `e2e_hello`/`e2e_hello_ack`（与 TCP 同一套 JSON 与 AEAD 派生）；**P2P 路径不做 relay_allocate、不使用 relay token**。
- 失败原因码与桌面一致：`no_candidate`、`symmetric_nat`、`single_direction`、`punch_timeout`。

### 7.7 UDP 数据面（安卓）

线格式**不得与桌面分叉**，逐字段对齐 §5.2：

| 项 | 值 |
|---|---|
| 头长 / 数据报 / 分片负载 | 16 B / 1200 B / 1184 B |
| 单帧上限 / 最大分片 | 6 MiB / 5314 |
| 重组超时 | 100 ms |
| 控制帧重传 | RTO 120 / 240 / 480 ms，最多 3 次 |
| 视频帧 | 不重传，缺片整帧丢弃 |
| 心跳 / 失效 | 1 s / 3 s |
| 重放窗口 | 64 帧 |
| 帧上限并发重组 | 8 个 `frame_id` |

- 安卓在 1.0.0 仅作控制端（不接受被控），因此可靠性归属固定：上行输入/剪贴板可靠、下行视频不可靠。
- 实现为 `UdpSessionPlane`，向上暴露 `InputStream` / `OutputStream` 门面：写侧识别 4 字节长度前缀后分片，读侧重组后补回长度前缀，使现有 `readLoop` 与 `writeFrame` **零改动**。
- 复用现有 `AeadChannel`（4 字节前缀 + 8 字节大端计数 + ChaCha20-Poly1305 + 64 帧窗口），不改密码学实现。
- 重放/窗口越界在 UDP 层只丢帧并计数，不关闭会话；连续 100 帧失败才判 `path_dead`。
- socket 收发缓冲各 ≥4 MiB；发送侧按画质档码率做令牌桶限速，避免突发丢包。
- 单写线程串行分片，读线程只做重组与投递；重组表按 `frame_id` 淘汰，容量 64 MiB。
- 后台/锁屏：1.0.0 只在应用前台建立与维持会话；进入后台导致的丢包按 §7.8 回退，不做保活服务。

### 7.8 选路与回退（安卓）

与桌面同一张预算表，不另设阈值：

| 层 | 判据 | 每候选 | 层预算 |
|---|---|---|---|
| L1 IPv6 直连 | TCP 建连成功 1 次 | 800 ms ×2 | 1500 ms |
| L2 IPv4 打洞 | 双向 ≥2 响应、≥3 RTT 样本 | 2000 ms | 5000 ms |
| L3 中继 | relay_allocate 成功 | — | 8000 ms |

- 顺序固定 L1 → L2 → L3；进入 L2 2 s 后并行预取中继。
- 回退判据与桌面一致：预算耗尽、双向不达标、通路建立后 3 s 无首个 AEAD 帧、运行期劣化。
- 成功后不回头；网络切换导致 `path_dead` 时从 L1 重新开始。
- 直连失败必须自动回退，不再要求用户改填中继地址（撤销旧范围中的「失败即报错」）。

### 7.9 日志与可断言字段（安卓）

- 统一使用 logcat tag `RDC_CONNECT`，每行一个 JSON，字段名与桌面 §4.4 完全一致。
- 必带：`event`、`transport`、`peer_address`、`peer_family`、`relay_used`、`reason`、`elapsed_ms`；打洞另带 `candidates`、`tried`、`rtt_ms`。
- 断言示例（验收脚本用 `adb logcat -s RDC_CONNECT:I` 抓取后解析）：

```json
{"event":"connect.path_selected","transport":"direct_udp","peer_address":"240e:370:6303:cb0::2","peer_family":6,"relay_used":false,"reason":"ok","elapsed_ms":2860}
```

- 判定 `peer_address` 与 `relay_used` 的规则同 §4.4：地址为合法 IPv6 字面量（无方括号）、`relay_used==false`，且信令日志在打洞成功后不再出现 `relay_allocate`。

### 7.10 明确不做（1.0.0）

- 不做 TURN、UPnP、NAT-PMP、PCP；不做 IPv6 UDP 打洞（IPv6 无 NAT，L1 走 TCP）。
- 不改连接页 UI（含「授信设备 / 最近内容」居中与对齐问题，归在跑的安卓连接页线），本规格不触碰 `MainActivity.kt` 的布局与 `RdcTheme.kt`。
- 不做后台保活/前台服务；不做证书固定与直连 TLS（1.1 议题）。

## 8. 任务拆分

一个功能一个模块一个负责人；同一文件不得被两个模块并行修改，按依赖顺序串行。

### 8.1 模块与负责人

| 模块 | 功能 | 负责人 | 依赖 |
|---|---|---|---|
| S0 | 契约澄清与变更记录 | M03 架构师 | 无 |
| S1 | 候选模型、优先级、序列化与兼容 | P04 Python | S0 |
| S2 | 双栈监听与失败隔离 | 线1（被控双栈监听，P04） | **不重复派单**：与在跑线同批文件，本规格只作验收口径 |
| S3 | L1 IPv6 选路、endpoint 解析、日志断言 | P04 Python | 线1、S1 |
| S4 | UDP 打洞数据面（封装/重组/重传/心跳） | P04 Python | S1、线1 |
| S5 | 安卓地址语法、输入长度、TLS 配置化、连接页对齐 | 线2（H01） | **整块归线2**：含「授信设备 / 最近内容」居中问题；本规格只补 `TlsPolicy` 的传输矩阵 |
| S6 | 验收取证脚本与证据归档（桌面） | T01 验收测试 | 线1、S3（IPv6）；S4（打洞） |
| S7 | 安全复核：TLS 判据、未知字段、日志脱敏、UDP 反欺骗 | S01 安全审查 | S4、S10 |
| S8 | 安卓 STUN 与候选采集 | H01（P04 复核线格式） | S0、S1（字段口径） |
| S9 | 安卓打洞与 UDP 数据面 | H01（P04 复核线格式） | S4（常量冻结）、S8 |
| S10 | 安卓选路、回退、日志断言 | H01 | S3（判据口径）、S9、线2 |
| S11 | 安卓端到端取证（adb） | T01 验收测试 | S10、S6 |

> S1/S3/S4 均为 Python，同一语言专家排期串行；S3 与 S4 均触碰 `connect/` 目录但文件不同，可并行。S1 与 S3 都改 `policy.py`，必须 S1 完成后 S3 再动。S8–S10 为 Kotlin，统一由 H01 串行实现，P04 只做线格式复核、不持有写权限。

### 8.2 文件清单

| 模块 | 文件 | 动作 |
|---|---|---|
| S0 | `docs/contracts/desktop-v1.md`（**不改**）、`docs/contracts/protocol-v1.md`（只增字段）、`docs/contracts/契约变更记录-202611-ipv6-punch.md`（按 `.team/templates/契约变更记录.md` 新建，记录「C1/C2 不改」裁决） | 改/建 |
| S1 | [policy.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/connect/policy.py) | 改：`Candidate` 增可选字段、优先级表、`mapped` 上线改写 |
| S1 | [relay.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/connect/relay.py) | 改：`candidate_to_payload` 输出 `family/scope/origin/related/proto`；`payload_to_candidate` 容错推断 |
| S1 | [native.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/connect/native.py) | 改：v6 host 候选带 `family/scope`；v4 host 优先级对齐 §3.2 |
| S1 | `tests/test_connect_policy.py` | 改：新旧字段双向用例 |
| S2 | [server.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/relay/server.py)、[host.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/session/host.py)、`tests/test_relay_dual_stack.py` | 由线1 落地，本规格不另派；S6 按 §6.2 验收 |
| S3 | [policy.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/connect/policy.py) | 改：L1 改 TCP 建连判据，独立于打洞阈值（S1 完成后改） |
| S3 | [runtime.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/connect/runtime.py) | 改：层级预算、`connect.path_selected` 事件、断言对象 |
| S3 | [service.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/commands/service.py) | 改：`_live_address` 支持 `[v6]:port` 与裸 v6；`transport_kind` 取值 |
| S3 | `tests/test_connect_runtime.py` | 改：L1 通过/失败/跳过三态 |
| S4 | `src/rdc/connect/udp_plane.py` | 建：帧编解码、重组、重传、心跳、`UdpSessionPlane` |
| S4 | [punch.py](file:///c:/Users/Administrator/Desktop/RemoteDesktop/src/rdc/connect/punch.py) | 改：增被控端 `PunchResponder`，探测与数据面复用同一 socket |
| S4 | `tests/test_connect_udp_plane.py` | 建：分片/乱序/丢片/重传/越界/上限 |
| S5 | [MainActivity.kt](file:///c:/Users/Administrator/Desktop/RemoteDesktop/android/app/src/main/java/com/rdc/remote/MainActivity.kt)、[ControlContract.kt](file:///c:/Users/Administrator/Desktop/RemoteDesktop/android/app/src/main/java/com/rdc/remote/ControlContract.kt)、[LanSession.kt](file:///c:/Users/Administrator/Desktop/RemoteDesktop/android/app/src/main/java/com/rdc/remote/LanSession.kt) | 由线2 落地；本规格只补 `TlsPolicy.kt`（若线2 未建）与候选 `kind/family`、`transport_kind` |
| S6 | `scripts/e2e_ipv6_punch.py`、`docs/验收/IPv6直连与打洞-2026-11-xx.md` | 建 |
| S7 | 无代码产出，出复核意见 | — |
| S8 | `android/.../StunClient.kt`、`CandidateGatherer.kt`、`Candidate.kt`，`AndroidManifest.xml`（`ACCESS_NETWORK_STATE`），`test/.../StunClientTest.kt` | 建（不改 `LanSession.kt`） |
| S9 | `android/.../PunchClient.kt`、`UdpSessionPlane.kt`（帧编解码/重组/重传/心跳）、`test/.../UdpSessionPlaneTest.kt` | 建；仅在 `LanSession.kt` 追加集成段（线2 合入后） |
| S10 | `android/.../PathSelector.kt`、`ConnectLog.kt`，`LanSession.kt`（P2P/中继分支） | 建 + 改；`LanSession.kt` 与线2 串行 |
| S11 | `scripts/e2e_android_punch.py`、`docs/验收/安卓IPv6与打洞-2026-11-xx.md` | 建 |

### 8.3 依赖顺序

```
S0 ──> S1 ──┬──> S3（需线1）──┐
            └──> S4 ──────────┼──> S6 ──> S11
线1（S2）─────────────────────┘            ↑
S1 ──> S8 ──> S9 ──> S10（需线2、S3）──────┘
S4 ────────────────────────────────────────┘
S10 ──> S7
```

实际排期：S0 → S1 → S3 → S4 → S6；安卓线 S8 → S9 → S10 在 S1 之后即可开工（S9 需 S4 常量先冻结）；S11 在 S6 与 S10 之后；S7 在 S10 之后收口。线1、线2 已在跑，不占本规格排期。

### 8.4 触碰 `desktop-v1` 的项

| 项 | 命令/消息 | 变更 | 性质 | 处置 |
|---|---|---|---|---|
| C1 | `get_session_status` | 追加可选只读字段 `peer_address`、`peer_family`、`path_reason` | 只增、只读、非破坏 | **不改（已裁决）**：证据落 §4.4 日志与 §10.3 取证，1.0.1 再评估 |
| C2 | `start_hosting` | 追加可选 `listen: string[]`（如 `["0.0.0.0:27888","[::]:27888"]`） | 只增、非破坏 | **不改（已裁决）**：同上 |
| C3 | `start_connection` / `get_connection_progress` | 无 | — | 阶段数维持 4，签名与错误码不变 |
| C4 | `protocol-v1` `offer.candidates[]` / `answer.candidates[]` | 追加 `family/scope/origin/related/proto`；`answer` 增可选 `candidates` | 只增，`v` 不升版 | 属协议层，不由 desktop-v1 约束；需 protocol-v1 变更记录 |
| C5 | 安卓 `startLanConnection`（App 内部入口） | 签名与返回值不变；内部新增候选/打洞分支 | 不改契约 | 由线2 与 S10 串行落地，禁止并行改 `LanSession.kt` |

结论：**`desktop-v1` 保持冻结、零变更**；C1/C2 按「不改」执行，证据只落日志与取证文件，1.0.1 再评估。命令行与错误码均无新增；安卓打洞只在协议候选字段内扩展。

## 9. 风险与回退

### 9.1 风险

| 风险 | 触发条件 | 影响 | 缓解 |
|---|---|---|---|
| 老被控端只监听 v4 | 用户未升级被控端 | 安卓填 v6 地址必然失败 | 错误提示区分「对端不支持 IPv6，请升级被控端」；发布说明写明 |
| 老客户端收到新候选字段 | 新版被控端向旧安卓/旧桌面发候选 | 旧端解析崩溃 | §3.4 只增不改；发布前用旧版二进制做一次互通用例 |
| 打洞失败 | 对称 NAT、双端同出口、运营商阻断 UDP | 建连慢 5 s 后落中继 | 预算固定 5 s；失败原因码入库；中继并行预取 |
| EIM 假设不成立 | 实测环境变化（换网络/换路由） | 打洞成功率下降 | 运行时按 STUN 结果判定映射类型；`origin=mapped` 全缺且 srflx 打不通即判 `symmetric_nat`，直接中继，不做无效重试 |
| 映射漂移 | NAT 刷新导致外部端口变化 | 会话中断 | 1 s 心跳维持；端口变化判 `mapping_changed`，10 s 内重打一次，失败即中继 |
| IPv6 公网暴露 | 被控端直接暴露 27888 | 扫描与爆破面增大 | 依赖访问码 + AEAD + 既有 5 次失败锁定；S01 必须复核；直连 TLS 列为 1.1 议题，1.0.0 不宣称已加密握手 |
| UDP 分片被中间设备丢弃 | 1200 字节仍超路径 MTU | 大帧失败 | `EMSGSIZE` 降级 1100；连续失败切中继 |
| 双栈端口冲突 | 其他进程占用 v6 27888 | 仅 v6 不可用 | 失败隔离，v4 继续服务 |
| 安卓网络切换（Wi‑Fi↔蜂窝） | 采集/打洞/会话中途切换 | socket 失效、路径断 | `NetworkCallback` 立即中止并重采；从 L1 重连，不静默沿用旧 socket |
| 安卓 Doze / 后台限制 | 锁屏或切后台 | UDP 丢包、心跳超时 | 1.0.0 只保证前台会话；超时按 §7.8 回退中继；不做保活服务 |
| 手机侧为对称 NAT / CGNAT | 双 STUN 映射不一致 | 打洞必失败 | 采集阶段判 `SYMMETRIC`，跳过 L2 直接中继，不浪费 5 s 预算 |
| Kotlin 与桌面线格式漂移 | 两侧各自实现 | 重组失败、无法互通 | 共用 §5.2 常量；S9 必须跑跨端互通用例；P04 复核线格式 |
| 手机公网 IPv6 直连暴露 | 蜂窝 v6 直连被控端 | 扫描/爆破面增大 | 同「IPv6 公网暴露」条；S01 复核 |

### 9.2 开关与回退

| 开关 | 默认 | 作用 |
|---|---|---|
| `connect.ipv6_enabled` | true | false 时 SKIP L1，行为等同 1.0.0 之前 |
| `connect.punch_enabled` | true | false 时 SKIP L2，直接中继 |
| `host.enable_ipv6` | true | false 时只开 v4 listener |
| `android.punch_enabled` | true | false 时安卓跳过 L2（STUN 照常采集但不用），直接中继；用于线上快速回滚 |

回退方式：相应开关置 false，无需数据迁移，无契约移除；回退后行为与当前版本一致。回退判据：IPv6 直连成功率 <90%，或打洞引入的崩溃/卡顿在桌面或安卓任一侧验收中不可接受（以 T01 的 `summary.json` 为准）。

### 9.3 老版本互通矩阵

| 控制端 | 被控端 | 结果 |
|---|---|---|
| 新（v6 地址） | 旧（仅 v4） | 失败，提示升级被控端 |
| 新（v4/中继） | 旧 | 正常，走中继；新字段被忽略 |
| 旧 | 新（双栈） | 正常；旧端只发单候选，新端按单候选处理 |
| 新 | 新 | 全部路径可用 |

## 10. 验收口径

只列判据，不在本规格执行。

### 10.1 正向

| 编号 | 场景 | 判据 |
|---|---|---|
| A1 | 双栈监听 | `ss -lntH` 同时出现 `0.0.0.0:27888` 与 `[::]:27888`；`sysctl net.ipv6.bindv6only` 任意取值均不影响结果 |
| A2 | IPv6 直连（关无线、蜂窝 v6） | `connect.path_selected.transport=direct_ipv6`；`peer_address` 以 `240e:` 开头；`relay_used=false`；进程连接表无 `47.109.32.249` |
| A3 | 安卓 IPv6 输入 | `[240e::1]:27888` 与裸 `240e::1` 均建连成功；`transport_kind=direct`；TLS 未启用 |
| A4 | 安卓 TLS 配置化 | 中继地址启用 TLS；把中继地址从配置移除后不再启用；新增自定义 TLS 主机生效 |
| A5 | 打洞成功 | 双端 `connect.path_selected.transport=direct_udp`；双向 RTT 样本 ≥3；1080p 连续 60 s 丢帧率 <5%；期间无 8750 已建立连接 |
| A6 | 分片正确性 | 90 KB 单帧分 77 片可完整重组；随机丢 1 片时整帧丢弃且不阻塞后续帧 |
| A7 | 回退 | 关闭 v6 后落 L2/L3；关闭打洞后落 L3；原因码正确 |
| A8 | 安卓候选采集 | 蜂窝下 1500 ms 内产出 ≥1 个 `kind=srflx`；Wi‑Fi 下产出 v4 host；候选 JSON 的 `family/priority` 与 §3.2 一致 |
| A9 | 安卓打洞 | logcat `transport=="direct_udp"`、`peer_address` 与对端 srflx 一致、`relay_used==false`；双向 RTT 样本 ≥3；1080p 60 s 丢帧率 <5% |
| A10 | 安卓直连 | 蜂窝 v6 下 `transport=="direct_ipv6"`、`peer_address` 以 `240e:` 开头、无 `relay_allocate` |
| A11 | 安卓 TLS 矩阵 | 中继 TLS 开；IPv6 直连与打洞均不开 TLS；`mode=ON` 选直连时记 `tls_ignored_direct` 且不阻断 |

### 10.2 负向对照（缺一不可）

| 编号 | 场景 | 判据 |
|---|---|---|
| N1 | 打洞预算耗尽 | 5 s 内切中继，且不早于 5 s（防止未尝试即判失败） |
| N2 | 单方向通 | 不判成功；原因码 `single_direction` |
| N3 | 映射漂移 | 判 `mapping_changed` 并重打一次，不静默沿用旧端口 |
| N4 | 旧客户端收新字段 | 旧版二进制不崩、会话可建立 |
| N5 | 非候选源地址注入 UDP | 数据报被丢弃且计数，不影响会话 |
| N6 | 安卓网络切换 | 切换后旧路径判 `path_dead`，重新采集并从 L1 开始；不出现沿用旧 socket 的日志 |
| N7 | 安卓收到未知候选字段/未知 kind | 不崩溃，未知 kind 按 relay 记录并继续 |
| N8 | 安卓打洞预算耗尽 | 5000–6000 ms 内切中继，原因码 `punch_timeout`；不得提前判失败 |

### 10.3 证据

- 结构化日志：`connect.level_result`、`connect.path_selected`、`assert_direct_ipv6`。
- 系统侧：`ss -lnt`、`ss -lun`、连接表快照、抓包（仅地址/端口/时间戳，不含载荷）。
- 安卓侧：`adb logcat -s RDC_CONNECT:I` 原始日志（JSON 行）、`adb shell dumpsys connectivity` 网络快照、被控端侧 `ss -lun` 对照。
- 归档目录：`docs/验收/IPv6直连与打洞-2026-11-xx/` 与 `docs/验收/安卓IPv6与打洞-2026-11-xx/`，含原始日志、快照与结论表。

## 11. 已裁决与剩余未决

### 11.1 已裁决（不再讨论，不再留待裁决）

1. C1/C2 两处契约追加：**不改**。`desktop-v1` 保持冻结，零字段、零签名、零错误码变更；证据只落结构化日志与验收取证文件；1.0.1 再评估。
2. 安卓 1.0.0 范围（**已变更，撤销旧「手机打洞进 1.0.1」**）：手机必须支持 IPv6 直连与 IPv4 打洞，与桌面同一线格式、同一预算、同一判据；仍不做 TURN、UPnP、IPv6 UDP 打洞。

### 11.2 剩余未决（不阻塞定稿，实现前由 S01/M03 给结论）

1. 公网 IPv6 直连的握手是否必须在 1.0.0 上 TLS：本规格定为否，风险列 §9.1，交 S01 复核。
2. 打洞是否支持双方同时发起：本规格定为控制端单向发起、被控端只应答。

## 12. 引用

- [desktop-v1 契约](file:///c:/Users/Administrator/Desktop/RemoteDesktop/docs/contracts/desktop-v1.md)
- [protocol-v1 契约](file:///c:/Users/Administrator/Desktop/RemoteDesktop/docs/contracts/protocol-v1.md)
- [connect-v1 设计](file:///c:/Users/Administrator/Desktop/RemoteDesktop/docs/design/connect-v1.md)
- [IPv6 直连能力实测](file:///c:/Users/Administrator/Desktop/RemoteDesktop/docs/验收/IPv6直连能力实测-2026-11-11.md)
- [视频帧封装 v2 实测报告](file:///c:/Users/Administrator/Desktop/RemoteDesktop/docs/验收/视频帧封装v2实测报告-2026-11-10.md)

## 附录 A 交接摘要（S0–S11）

### A.1 模块、依赖与机器可断言判据

| 模块 | 负责人 | 依赖 | 机器可断言判据（脚本直接判定，不靠肉眼） |
|---|---|---|---|
| S0 | M03 | 无 | ① `git diff --exit-code -- docs/contracts/desktop-v1.md` 退出码 0；② `protocol-v1.md` 删除行数为 0（`^-` 且非 `^---`）；③ 变更记录 9 个必填段齐全 |
| S1 | P04 | S0 | ① `pytest tests/test_connect_policy.py` 全绿；② 未知键输入不抛异常且 `family` 由地址推断；③ `kind=="mapped"` 输入 → 输出 `kind=="srflx" and origin=="mapped"`；④ 优先级 8 项与 §3.2 全等 |
| S2（线1） | 线1 | 无 | ① `ss -lntH "sport = :27888"` 同时含 `0.0.0.0:27888` 与 `[::]:27888`；② v6 socket `getsockopt(IPV6_V6ONLY)==1`；③ 预占 v6 端口后启动：进程存活、v4 可连、日志含 `relay.ipv6.bind_failed` |
| S3 | P04 | 线1、S1 | ① `_live_address("[240e::1]:27888")==("240e::1",27888)`，裸 v6 同，`"1.2.3.4:27888"` 不变；② 日志 `transport=="direct_ipv6" and peer_address.startswith("240e:") and relay_used==false`；③ 连接表无 `47.109.32.249`；④ 无 v6 时 `reason=="ipv6_no_global" and elapsed_ms<10`；⑤ 不可达时 `elapsed_ms<=1500 and reason=="ipv6_unreachable"` |
| S4 | P04 | S1、线1 | ① 90000 B 帧 `frag_count==77`；② 分片反转投递后重组 sha256 与原帧相同；③ 丢 1 片：100 ms 后返回 None 且重组表 `size==0`；④ 可靠帧不 ACK：重传 3 次、间隔 120/240/480 ±20 ms、随后 `path_dead`；⑤ `frag_count==5315` 或 `frame_len>6291456`：丢弃且不分配；⑥ 3 s 无合法包 `path_dead`；⑦ 计数器越界：`udp_replay_drop+1`、异常 0、连接不关闭 |
| S5（线2） | 线2 | 无 | ① `parse("[240e::1]:27888")`、`parse("[240e::1]")`、`parse("240e::1")` 均 `==("240e::1",27888)`；`parse("1.2.3.4")==("1.2.3.4",8750)`；`parse("1.2.3.4:27888")` 不变；② `LengthFilter` 上限 `==64`；③ `shouldUseTls(RELAY, relayHost)` 在 AUTO/ON 为 true；`shouldUseTls(DIRECT_*, *)` 恒为 false；`OFF` 对中继为 false；④ 直连会话 `transport_kind=="direct"`；⑤ 连接页「授信设备 / 最近内容」居中与对齐回归通过 |
| S6 | T01 | 线1、S3、S4 | ① `summary.json` 含 A1–A7、N1–N5 共 12 条，每条有 `pass:boolean` 与 `evidence` 路径；② N1 切中继耗时 ∈ [5000,6000] ms；③ A5 满足 `drop_pct<5` 且双向 `rtt_samples>=3`；④ 12 条全 `pass==true` 才判通过 |
| S7 | S01 | S4、S10 | ① 报告 0 高危、0 中危；② 每条发现含 `file:line` 与复现步骤；③ 未知字段用例覆盖 2 种旧端 |
| S8 | H01 | S0、S1 | ① STUN 已知向量解析出正确 `XOR-MAPPED-ADDRESS`；② 双服务器映射一致 → `EIM`，不一致 → `SYMMETRIC`；③ 采集耗时 ≤1500 ms；④ 候选 JSON 字段/优先级与 §3.2 全等；⑤ 候选不含回环/链路本地 |
| S9 | H01 | S4、S8 | ① 90000 B → 77 片；② 乱序重组 sha256 相同；③ 丢片 100 ms 后 `null` 且表清空；④ 不 ACK 时重传 3 次、间隔 120/240/480 ±20 ms；⑤ 3 s 无包 `path_dead`；⑥ 跨端互通（Kotlin 编码 → Python 解码，反向亦然）逐字节相等 |
| S10 | H01 | S3、S9、线2 | ① logcat `transport=="direct_udp" and relay_used==false`；② `peer_address` 与对端 srflx 完全一致；③ 打洞成功后信令无 `relay_allocate`；④ 预算耗尽 5000–6000 ms 切中继；⑤ 网络切换后从 L1 重连 |
| S11 | T01 | S10、S6 | ① `summary.json` 覆盖 A1–A11、N1–N8 共 19 条，每条含 `pass` 与 `evidence`；② 原始 logcat 与 `ss` 快照归档；③ 全绿才通过 |

### A.2 与在跑两条线的文件冲突

| 在跑线 | 占用文件 | 冲突模块 | 处置 |
|---|---|---|---|
| 线1 被控双栈监听 | `src/rdc/relay/server.py`、`src/rdc/session/host.py`（含新测试） | S2 完全同体 | **不派单**；S3/S4/S6 只消费其结果，不改这两文件 |
| 线2 安卓地址语法、40→64、TLS、连接页对齐 | `MainActivity.kt`、`ControlContract.kt`、`LanSession.kt`、`RdcTheme.kt` | S5 完全同体；S8/S9/S10 抢 `LanSession.kt` | S5 **不派单**；S8 只建新文件（不碰 `LanSession.kt`）；S9/S10 **必须等线2 合入后**再改 `LanSession.kt`，且只追加集成段；`MainActivity.kt` 与 `RdcTheme.kt` 一律不碰（含居中问题） |
| 无 | `src/rdc/connect/policy.py` | S1 与 S3 同文件 | 同负责人串行：S1 完成后再动 S3 |
| 无 | `src/rdc/commands/service.py` | 仅 S3 | 与两条线无重叠，可并行 |

其余文件无冲突：S1 的 `native.py`、`relay.py`，S4 的 `punch.py` 与新增 `udp_plane.py`，S8 的 `StunClient.kt`/`CandidateGatherer.kt`/`Candidate.kt`，S9 的 `PunchClient.kt`/`UdpSessionPlane.kt`，S10 的 `PathSelector.kt`/`ConnectLog.kt`，S6/S11 的 `scripts/` 与 `docs/验收/`。

### A.3 阅读顺序

先读 §0（范围与裁决）→ §8（排期与契约触碰）→ 本附录（判据与冲突）；实现细节按模块查：S1 看 §3、S2 看 §6、S3 看 §4、S4 看 §5、S5/S8/S9/S10 看 §7、S6/S11 看 §10。
