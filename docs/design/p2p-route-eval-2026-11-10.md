# P2P 打洞路线技术评估（2026-11-10）

- 状态：评估结论，可直接拍板；**未修改** `src/rdc/connect/`、未改 `docs/contracts/`、未改客户端各端代码
- 范围：ZeroTier 嵌入、成熟 ICE 替换、现有栈增强三条路线；含换路由器验收清单与当前网络 UPnP-IGD 实测
- 基线文档：`docs/design/connect-v1.md`（L1 IPv6 → L2 P2P UDP → L3 中继）
- 证据目录：`evidence/router-acceptance/20261110-route-eval/`
- 口径：文中所有“实测”均指 2026-11-10 在当前家宽（小米路由器 HD R3D，固件 2.28.17）上的观测；“推算”与“未验证”单独标注，不混写

---

## 一、结论先行

### 1. 拍板建议

**主路线选路线 3（现有栈增强），保留现有 L3 中继作为兜底；不采用 ZeroTier；ICE 只作为后续互操作选项，不在本轮引入。**

具体组合：

| 项 | 结论 |
| --- | --- |
| 主传输 | 现有 UDP 打洞栈 + **UPnP-IGD / NAT-PMP 主动映射** + 多 STUN + 同时打洞 |
| 兜底 | 现有自建 TLS 1.3 中继（1080p30 / 2800 kbps 已验证） |
| ZeroTier | **不采用**（许可、虚拟网卡/系统权限、Android VpnService 冲突、controller 依赖、无法替代第二出口） |
| 成熟 ICE | **暂不替换**；当需要与浏览器/WebRTC 互操作，或需要现成 TURN 生态时再评估 |
| 端口预测/生日扫描 | **默认关闭**，只在“双对称且用户显式允许”时作为实验开关，且必须限速 |
| TCP 回退 | 不新增 P2P TCP 打洞；UDP 被封时**直接走既有 TLS 1.3 中继**（本质就是 TCP 回退） |

### 2. 三个关键判断

1. **UPnP-IGD 在当前网络实测“可用”**，外部主机可正向收到映射端口的数据包（30/30），删除后 0/30。它应当进入 P0 实现，而不是停留在“可选增强”。
2. **当前瓶颈不是打洞算法，而是“第二个独立公网出口”和 IPv6**。路线 1 和路线 2 都不能凭空制造第二个出口，也都不改变“双对称 / CGNAT 必须中继”的数学边界。
3. **ZeroTier 在双对称 NAT 与 CGNAT 下仍然必须中继**，官方文档明确写“Behind CG-NAT or Restrictive Firewalls: Connections will be forced through relay servers”。它也**不能替代**我们用于打洞验证的第二个独立公网出口。

---

## 二、现状基线（评估的起点）

### 1. 已有实现

| 文件 | 作用 |
| --- | --- |
| `src/rdc/connect/policy.py` | 决策顺序、候选结构、18 项共享参数、`decide()`、运行期降级 |
| `src/rdc/connect/stun.py` | RFC 5389 Binding、XOR-MAPPED-ADDRESS、端口池、800 ms / 1.5 s 预算 |
| `src/rdc/connect/punch.py` | 双向 UDP 打洞探测（`RDCP1` + Q/R + 令牌），100 ms 发包 |
| `src/rdc/connect/native.py` | 地址枚举（Windows `GetAdaptersAddresses`）、全局 v6 过滤、IPv6 自检 |
| `src/rdc/connect/relay.py` | TLS 1.3 中继信令、设备鉴权、`relay_allocate` |

### 2. 2026-11-10 实测基线

| 项 | 实测值 | 证据 |
| --- | --- | --- |
| WAN IPv4 | `115.150.84.112`，公网、非 CGNAT、单一出口 | `result.json` → `wan` |
| 运营商 | AS4134 中国电信（此前 Cymru 反查） | `connect-v1.md` 第八节 |
| IPv6 | **不可用**：仅 3 个 `fe80::`，无全局地址；`native_ipv6_probe()` 返回“无全局 IPv6 地址” | `result.json` → `ipv6` |
| NAT 映射行为 | **endpoint-independent（锥形）**：同一 socket、2 个 STUN、3 轮，唯一映射 1 个 | `result.json` → `nat_mapping` |
| 端口保持 | **6/6 保持**：本地 50120–50125 → 公网同端口，delta 全 0 | `result.json` → `port_preservation` |
| STUN RTT | 本次两服务 31 / 47 ms；映射轮次 p50 32.0 ms | `result.json` |
| UPnP-IGD | **可用**，`MiniUPnPd/2.0`，`WANIPConnection:1` | 见第九节 |
| NAT-PMP | 可用：外部地址查询与 MAP 均成功 | `result.json` → `natpmp_pcp` |
| PCP | ANNOUNCE 成功（`result=0`）；PCP MAP 返回 NAT-PMP 格式 `result=3`，**不能用于映射** | `result.json` + `raw/pcp-map.txt` |
| hairpin | **不支持**：10 发 0 收 | `result.json` → `hairpin` |
| 锥形↔锥形打洞 | **仍未验证**：虚机与本机共用同一公网 IP，退化为同出口回环 | `connect-v1.md` 第八节 |
| 对称 NAT | 已实测不可穿，走中继 | `connect-v1.md` 第八节 |

**结论：**当前网络是“公网 IPv4 + 锥形 + 端口保持 + UPnP 可用 + 无 hairpin + 无 IPv6”。这套条件已经是家用网络里较好的一档；真正缺的是对端侧的第二出口与 IPv6。

---

## 三、路线 1：嵌入 ZeroTier

### 1. 许可与商业使用限制（按版本核实）

| 组件/版本 | 许可 | 对闭源商业产品的影响 |
| --- | --- | --- |
| ZeroTier One **1.16.2**（2026-05-28 发布，当前最新） | 分组件许可，见下 | 需逐组件判断 |
| `node/`、`include/`（核心）与 `service/`、`osdep/`（服务） | **MPL-2.0** | 允许链接进闭源产品；但对 MPL 文件的修改必须以 MPL 公开，文件级 copyleft |
| `controller/`（位于 `nonfree/`） | **ZeroTier Source-Available License 1.0** | **非商业免费；任何商业使用（含企业内部生产、SaaS、打包进产品）需单独商业许可**；评估用途仅限 30 天 |
| 旧版（如 1.4.4） | **BSL 1.1**，Change Date **2025-01-01**，Change License **Apache-2.0** | 1.4.4 已过 Change Date，可按 Apache-2.0 使用；但版本老、缺少后续修复 |
| `libzt`（PyPI 最新 **1.8.4**，2022-01-03） | PyPI 标注 **BUSL 1.1**，“Free for non-commercial use” | **闭源商业产品不能直接用**，需商业许可；且 2022 年后无新发布 |

要点：

1. **只嵌入 agent（MPL-2.0）**在许可上可行，但自建 controller 才是关键分歧点：`controller/` 已不再是开源许可，商业自托管必须付费。
2. 使用 ZeroTier Central 托管 controller 属于外部服务依赖，受其套餐与条款约束；不能把“自建 controller”当作零成本方案。
3. 如果为了避开新许可而锁死 1.4.4（已转 Apache-2.0），等于放弃 2025–2026 年的安全修复与 TCP relay / pylon 等能力，不建议。
4. 结论：**许可风险高于收益**。即使技术可行，也需要法务先确认；在当前阶段不值得为它承担这条依赖。

依据：
- ZeroTier One 1.16.0 Release Notes（2025-08-21）：“core (`node/` and `include/`) and the service (`service/` and `osdep/`) are now under MPL”；“controller (`controller/`) is now under a commercial source-available license”；`make ZT_NONFREE=1`
- `nonfree/LICENSE.md`：Source-Available License 1.0，Commercial Use 定义覆盖“for-profit company / 生产、预发、开发环境 / 打包进任何产品”，商业使用需单独许可
- 仓库根 `LICENSE.txt`：仍保留 BSL 1.1 文本（Licensed Work 1.4.4，Change Date 2025-01-01，Change License Apache-2.0）

### 2. 跨平台可行性（重点 Android / macOS）

| 平台 | 形态 | 权限/驱动 | 与现有客户端整合 |
| --- | --- | --- | --- |
| Windows | ZeroTier One 服务 + TAP 虚拟网卡（`zttap300`） | 安装需管理员；驱动需签名 | 应用内多一层 L3 网卡；路由、MTU、DNS 需避让 |
| macOS | 系统级安装 + 网络扩展（Network Extension） | 安装需管理员；扩展需签名/公证与用户批准；不适合 Mac App Store 沙箱 | 分发与公证成本上升；与用户已有 VPN 可能冲突 |
| Linux | `tun` 设备 + 守护进程 | 需 root 或 `CAP_NET_ADMIN` | 相对简单，但仍多一层网卡 |
| Android（ZeroTier One App 形态） | **VpnService** 建立 TUN | **不需要 root**，但需用户授权、常驻通知、**独占系统 VPN 槽位** | 与用户其他 VPN 互斥；可 per-app，但会接管所选应用全部流量 |
| Android（libzt 形态） | 用户态网络栈（`zts` socket） | **不需要 root、不需要虚拟网卡** | 媒体收发必须改成 libzt socket；现有 UDP/TLS 栈要重写；AAR 停留在 2022 年 |

结论：

- 桌面三端可行，但都要装驱动/系统扩展，**不是“加个库”**。
- Android 两条路都不轻松：VpnService 会抢占系统 VPN 槽位并改变流量边界；libzt 虽无 root，但要求把传输层整体换成用户态 socket，等于重写媒体通道。
- 我们的客户端已经有自己的 UDP 打洞与 TLS 中继，再套一层虚拟网卡会带来双重封装、双重拥塞控制与排障盲区。

### 3. NAT 场景覆盖率

| 场景 | ZeroTier 行为 | 说明 |
| --- | --- | --- |
| 锥形 ↔ 锥形 | 直连 | 与现有栈相同 |
| 锥形 ↔ 对称 | 多数可直连 | 锥形侧可学习对称侧的 per-destination 映射；这是 ZeroTier 相对“见到对称就跳过”的优势 |
| 对称 ↔ 对称 | **中继** | 官方博客：“if both hosts are behind symmetric NAT, hole punching can't work… at least not reliably” |
| CGNAT | **强制中继** | 官方 Enterprise Deployment Guide：“Behind CG-NAT or Restrictive Firewalls: Connections will be forced through relay servers” |
| 双栈 | 不解决 IPv6 缺失 | ZeroTier 可走 UDP 443/TCP 443，提升 UDP 受限网络可达性；但不能让没有 IPv6 的线路获得 IPv6 |

### 4. 延迟与带宽开销

- **直连**：ZeroTier 自身加密 + 用户态/虚拟网卡转发。桌面端额外延迟通常在亚毫秒到数毫秒量级；1080p30 / 2800 kbps 的带宽占用对它不是问题。
- **中继**：额外一跳（到 root 或自建 relay）的 RTT；官方 TCP relay 文档明确说“slow”，且 TCP 承载实时流会出现队头阻塞，抖动比 UDP 中继更差。
- **双重加密**：ZT 一层 + 我们媒体层一层，CPU 与密钥管理都翻倍；Android 上更明显。

### 5. 运维与排障成本

- 自建 controller 只有 JSON API，无官方 Web UI；成员授权、网络配置、身份吊销都要自己写工具。
- 默认仍依赖 ZeroTier 的 root servers（planet）；要完全自持需自定义 planet/moon，并把配置文件分发到所有客户端，轮换成本高。
- TCP relay 可自建（pylon），但官方免费 relay 慢；自建又多一个需要监控的公网服务。
- 排障入口是 `zerotier-cli info/peers/listnetworks` 或本地 JSON API，**不是我们的 `connect_level_result` 事件**；路径是 direct 还是 relay 只能间接看，且与我们的 L1/L2/L3 语义不对应。

### 6. 与现有 L1/L2/L3 决策链、测速/质量监控的整合代价

**高，且会破坏现有可观测性。**

- 接入后传输变成“虚拟网卡通/不通”，`transport_kind` 无法区分 IPv6 / UDP 打洞 / 中继；`decide()` 的三层顺序被折叠成一层。
- 现有 `QualitySample`（丢包、RTT p95、无解码帧、关键帧）基于我们自己的 UDP socket；换成虚拟网卡后采样点要重写，或只能读网卡计数器。
- 中继降级由 ZeroTier 自己决定，我们的 `degrade_*` 阈值失效；`relay_allocate.budget_kbps` 与 `quality_profile` 语义丢失。
- `connect_level_result` / `transport_changed` 事件无法如实填写，跨端统计与回归对比会断档。

### 7. 安全面

| 维度 | 新增风险 |
| --- | --- |
| 信任根 | 新增 controller 成员授权与节点身份；用 Central 时信任 ZeroTier Inc；自建时 controller 成为单点与新的信任根 |
| 加密边界 | ZT 端到端加密套在我们的媒体加密之外；身份私钥需落盘保护（Android 应进 Keystore） |
| 攻击面 | 虚拟 L2/L3 网络对同网成员可达，需要 ACL；桌面需装内核驱动/系统扩展；Android VpnService 接管流量 |
| 供应链 | C++ 代码 + 平台驱动/扩展，升级与 CVE 响应依赖上游 |

### 8. 路线 1 结论

**不采用。** 它没有带来“双对称/CGNAT 可直连”的新能力，却引入许可、驱动/系统权限、Android VPN 槽位、外部 controller 依赖与可观测性倒退。

### 9. ZeroTier 能否替代“第二个独立公网出口”

**不能。**依据如下：

1. **覆盖层不改变底层 NAT 映射。** 打洞验证需要两个**不同的公网 IPv4 出口**，观察各自的映射端口是否可被对方命中。ZeroTier 提供的是虚拟地址，不提供新的公网 IPv4，也不在你家路由器上生成可被第三方探测的映射。
2. **ZT 的 direct/relay 判定会掩盖真实结果。** 即使 `zerotier-cli peers` 显示 direct，也只能证明 ZT 自己找到了路径（可能是 IPv6、可能是它自己的 relay），不能证明我们的 STUN/打洞算法在两个 NAT 之间成功。用它做验证等于“验证 ZeroTier”，不是“验证我们的实现”。
3. **同出口问题依然存在。** 如果第二台主机与本机共用同一条宽带/同一个 CGNAT，ZeroTier 也无法制造第二个出口；我们已实测该形态下 hairpin 10 发 0 收，ZT 同样要面对这个 NAT。
4. **外部探针需要真实公网 IP。** 我们验证 UPnP 映射是否生效，是从 ECS 公网 IP 主动向映射端口发包（30/30）。ZT 虚拟 IP 不能被第三方 STUN/探针使用。
5. **第二出口的现实选项**只有：另一条宽带、手机热点/5G CPE（不同运营商）、或云主机 + 4G/5G 上行。ECS 是“公网主机（无 NAT）”，只能验证“公网 ↔ NAT”，不能验证“NAT ↔ NAT”。

补充：ECS 仍然是必要的——它是合法的**外部观察点**与中继宿主；它不能替代“第二个 NAT 出口”，但可以替代“外部探针”。

---

## 四、路线 2：换用成熟 ICE 实现

### 1. 许可

| 实现 | 语言 | 许可 | 商业使用 |
| --- | --- | --- | --- |
| Pion（ICE/TURN/STUN） | Go | **MIT** | 可直接商用 |
| libnice | C / GLib | **LGPL-2.1**（部分文件另附 MPL-1.1） | 可商用；动态链接 + 合规声明；静态链接需注意 |
| webrtc-rs | Rust | **MIT / Apache-2.0** 双许可 | 可直接商用 |

许可本身不是障碍，真正的成本在集成。

### 2. 跨平台可行性

| 实现 | Windows | macOS | Linux | Android | 备注 |
| --- | --- | --- | --- | --- | --- |
| Pion | Go 库/子进程 | 同左 | 同左 | 需 `gomobile bind` 生成 AAR，**无官方封装** | 需要自建 JNI 生命周期与打包流程 |
| libnice | 可构建 | 可构建 | 可构建（GStreamer 生态） | NDK 交叉编译 + JNI，**无官方 AAR**；GLib 依赖体积大 | 桌面成熟，Android 工作量大 |
| webrtc-rs | Rust 库 | 同左 | 同左 | `cargo-ndk` + JNI，无官方封装 | 与 Python 主栈语言不通，需 FFI 或 sidecar |

我们的主栈是 Python，客户端含四端；引入任何 ICE 实现都意味着新增一门语言运行时或一个 sidecar 进程，并把它打进四个平台的安装包。

### 3. NAT 场景覆盖率

ICE 的增强点：

- 多 STUN / 多 TURN；
- 候选对（host、srflx、prflx、relay）与连通性检查状态机；
- 同时打洞、aggressive nomination；
- 单侧对称时利用 prflx 学习对端映射。

但**数学边界不变**：

| 场景 | 仅 ICE（无 TURN） | ICE + TURN |
| --- | --- | --- |
| 锥形 ↔ 锥形 | 可穿 | 可穿 |
| 锥形 ↔ 对称 | 可穿（prflx） | 可穿 |
| 对称 ↔ 对称 | **不可穿** | 走 TURN |
| CGNAT | 通常不可穿 | 走 TURN |
| 双栈 | 取决于系统 IPv6 | 同左 |

也就是说：**ICE 不会比我们多穿一类 NAT；它把“打洞状态机 + TURN 客户端”做成标准件。** 真正的能力提升来自 TURN，而 TURN 就是中继——我们已经有自建 TLS 1.3 中继。

### 4. 延迟与带宽

- 候选收集 + 连通性检查通常几百毫秒，能与现有 5 s 预算兼容。
- 纯 ICE 不改变媒体封装，不增加带宽；若引入完整 WebRTC（DTLS-SRTP、拥塞控制、jitter buffer），改动与开销都会显著上升。
- TURN 中继多一跳，带宽成本与现有中继相同或更高（TURN 通常还要分配端口与配额）。

### 5. 运维与排障

- 需要 TURN 服务（如 coturn）与凭据体系（长期/短期），多一套公网服务要监控。
- 需要信令层交换 SDP/候选；我们已有 offer/answer 候选交换，但字段模型不同。
- Android 封装与崩溃排查链路长：Python → JNI/AAR → Go/Rust/C，出问题很难定位。

### 6. 与现有栈整合代价

中等偏高：

- 可以只取 ICE agent 替换 `stun.py` + `punch.py`，保留 L1 与 L3；候选需要映射到 `Candidate`，`LevelResult.detail` 需要补 ICE 状态。
- 成功后仍是普通 UDP 五元组，现有 `QualitySample` 与四条降级阈值可以继续使用；这一点明显优于 ZeroTier 的虚拟网卡方案。
- 但收益主要是“少维护打洞状态机”，不是“多穿 NAT”。
- 若目标是 WebRTC 互操作（浏览器直接看画面），ICE 是必需项；当前没有这个需求。

### 7. 安全面

- 完整 WebRTC 会引入 DTLS 证书/指纹校验、SRTP 密钥管理，信任模型比现在复杂。
- 引入 TURN 后，TURN 凭据成为新的信任材料（需短期凭据与配额）；Pion/libnice/webrtc-rs 的 Android 封装与原生库属于新增供应链面。
- TURN 需要鉴权与配额，否则会被滥用为中继跳板。
- 新增 Go/Rust/C 依赖与原生库，CVE 面与升级责任增加。

### 8. 路线 2 结论

**暂不替换。** 保留为“需要浏览器/WebRTC 互操作”时的后手；可以借鉴其候选对与 prflx 处理，但不必现在引入一整门运行时。

---

## 五、路线 3：在现有栈上增强

**许可与商业限制：**全部为现有自研代码与 Python 标准库（`socket`/`http.client`/`ipaddress`），不新增第三方运行时与许可义务；UPnP/NAT-PMP/PCP 都是协议实现，无授权费用。

### 1. 增强项与优先级

| 优先级 | 增强项 | 理由 | 现状 |
| --- | --- | --- | --- |
| **P0** | UPnP-IGD / NAT-PMP 主动映射 | 当前网络实测可用且有外部可达证据；能把“碰运气打洞”变成“确定入站端口” | 产品栈无客户端；验收脚本已实现 |
| **P0** | 多 STUN（≥3 个不同网络/ASN） | 提升映射判定置信度，识别多出口/多层 NAT | 现有 2 个（ECS + 另一公网） |
| **P0** | 单侧对称仍尝试打洞 | 当前 `connect-v1.md` 是“本端对称即跳过”，会丢掉“本端对称 + 对端锥形”的可穿组合 | 需调整判定 |
| **P0** | 映射生命周期：租期 ≤120 s、续租、退出/崩溃清理 | 实测 10 s 租约会消失，120→60 可续；不清理会留残留 | 验收脚本已验证流程 |
| **P0** | UDP 出站被封的快速识别 | 避免在必然失败的 L2 上耗 5 s；直接走既有 TLS 中继 | 需补探测 |
| P1 | 同时打洞时序对齐 + 随机抖动 | 提升对称/严格 NAT 下首包命中率 | `punch.py` 已是双向，需加对齐 |
| P1 | mapped 候选优先级 | 静态映射比 srflx 稳定，且不受出向 SNAT 端口变化影响 | 需扩展候选类型 |
| P1 | 端口预测（顺序映射） | 仅在检测到“映射端口按固定步长变化”时有意义；当前网络端口保持，预测无意义 | 未实现；默认关闭 |
| P2 | 生日攻击式端口扫描 | 双对称时命中率低、易被判定为扫描；只做实验开关 | 不建议进主线 |
| P2 | PCP 映射 | ANNOUNCE 可用，但 MAP 返回 NAT-PMP 格式 `result=3`；当前固件不支持 PCP 映射 | 仅机会性尝试 |

### 2. 关于“TCP 回退”的明确判断

不建议实现 P2P TCP 打洞（TCP simultaneous open）：依赖 `SO_REUSEADDR`、TTL 技巧、系统差异大、运营商与防火墙经常直接阻断，成功率远低于 UDP。

**推荐口径：UDP 不可用 → 直接 L3 中继（TCP/TLS 443）。** 这已经是现成的“TCP 回退”，只是发生在中继层而不是 P2P 层。实现上只需：

1. STUN 全部超时 / 收到 ICMP 不可达 / UDP 发包持续无响应 → 标记 `udp_blocked`；
2. 跳过 L2，直接 `relay_allocate`；
3. 事件里记 `reason=udp_blocked`，便于统计。

### 3. NAT 场景覆盖率（增强后）

| 场景 | 覆盖率 | 说明 |
| --- | --- | --- |
| 锥形 ↔ 锥形 | **高** | 现有能力；配合同时打洞与多 STUN 更稳 |
| 锥形 ↔ 对称 | **中高** | 单侧对称可穿：锥形侧收到包后学习映射；需取消“见对称即跳过” |
| 对称 ↔ 对称 | **低** | 仅端口预测有边际收益；默认直接中继 |
| CGNAT | **低** | UPnP 在 CGNAT 后无效；PCP MAP 在本路由器不可用；同运营商 EIM 可短预算尝试，失败即中继 |
| 双栈 | **高（有条件）** | 路由器下发可用 IPv6 时，L1 直接生效 |

### 4. 延迟与带宽开销

- 多 STUN：与现有并行查询同一量级，受 `stun_total_ms=1500` 约束。
- UPnP/NAT-PMP：局域网内 SOAP/UDP，实测 AddPortMapping 15 ms、GetSpecificPortMappingEntry 31 ms；不占广域带宽。
- 同时打洞：仍按 100 ms 间隔，带宽可忽略。
- 端口预测/生日扫描：唯一有成本项。必须限速（例如 ≤200 pps、总包数 ≤2048、总时长 ≤2 s），并且默认关闭。
- 媒体带宽不变：增强只影响“能否直连”，不改变 1080p30/2800 kbps 的编码档位。

### 5. 运维与排障成本

- **无新增外部服务**：UPnP/NAT-PMP/PCP 都面向本地网关；失败静默降级。
- 需要注意的坑（均已实测）：
  1. 多网卡主机默认组播出口可能落在 VMware 虚拟网卡，SSDP 收到 0 响应；必须显式 `IP_MULTICAST_IF` 绑定物理网卡，并对网关 1900 做单播兜底。
  2. 局域网内可能有第二个设备也宣告 IGD（实测 `192.168.31.12`）；只接受**来源 IP == 默认网关**的 IGD。
  3. 出向 SNAT 不必然复用静态映射：先对内部端口发过 STUN，再 `AddPortMapping`，STUN 仍显示旧端口。**映射是否生效以 `GetSpecificPortMappingEntry` + 外部探针为准**，不能用出向 srflx 判断。
  4. 租期必须续租；实测 10 s 租约在 12 s 后条目消失（错误码 714）。
  5. 退出/崩溃要清理；建议租期 ≤120 s，进程崩溃后由租期自然过期兜底。

### 6. 与 L1/L2/L3 决策链、测速/质量监控的整合

**低侵入，推荐按以下方式接：**

1. **候选扩展**：新增 `kind="mapped"`（或 `family` + `source="upnp"` 标注），表示“路由器静态映射的公网地址:端口”。排序建议：`host > mapped ≥ srflx > relay`。
2. **socket 复用**：先创建打洞 UDP socket，再把它当前本地端口作为 `NewInternalPort` 申请映射；**映射端口必须等于打洞端口**，否则对端发来的包进不到同一个 socket。
3. **能力探测缓存**：按“网关 MAC + 本机网段”缓存 UPnP/NAT-PMP 能力与 WAN 公网性，TTL 10 分钟；换网/换路由器自动失效。
4. **决策链不变**：仍是 L1 → L2 → L3；增强只影响 L2 的候选集合与“对称是否跳过”的判定。
5. **质量监控不变**：仍用现有 `QualitySample` 与四条降级阈值；映射不参与运行期判定，只在建连期使用。
6. **事件补充**：新增 `mapping_result{protocol, external_port, lease_s, ok, reason}`，便于跨端统计；不修改契约正文，先在客户端内部记录。

### 7. 安全面

UPnP 映射等于在路由器上开一个入站 UDP 口，必须按最小暴露实现：

- 只映射 **UDP**，不映射 TCP；不映射 1024 以下端口；外部端口随机高位。
- 映射描述固定可识别（如 `rdc-<device_id 前 8 位>`），便于用户审计与清理。
- 租期 ≤120 s，会话期间续租；会话结束、进程退出、异常捕获路径都要 `DeletePortMapping`。
- 入站包必须通过 `punch.py` 的令牌校验（现有 `RDCP1` + 16 位十六进制令牌），非本会话包直接丢弃；映射本身不提供业务数据入口。
- SSDP 只信默认网关，拒绝其他 IGD；不因收到未知设备宣告就发 SOAP。
- 日志记录映射创建/续租/删除与外部端口，便于排障与滥用排查。
- 在 UI 上把“允许自动端口映射”做成可关闭开关；企业环境默认关闭。

### 8. 路线 3 结论

**推荐为主路线。** 它在不改决策顺序、不引入新运行时、不新增外部依赖的前提下，把当前网络已经具备的能力（锥形、端口保持、UPnP 可用）转成确定的连通性收益；唯一需要克制的是端口预测/扫描类手段。

---

## 六、拍板：组合方式与分阶段落地

### 1. 组合

```
L1 IPv6（路由器支持后自动生效）
  ↓
L2 UDP：host / mapped(UPnP/NAT-PMP) / srflx 候选 + 同时打洞
  ↓ 失败或 udp_blocked
L3 自建 TLS 1.3 中继（现状保留）
```

- 不建议把 ZeroTier 作为任何一层。
- 不建议现在用 ICE 替换 L2；保留为 WebRTC 互操作后手。
- 端口预测/生日扫描只作为 L2 内部的实验开关，默认关闭。

### 2. 分阶段（按依赖顺序，不按时间承诺）

**阶段 P0（先做，直接决定连通率）**

1. 新增映射模块（建议 `src/rdc/connect/mapping.py`）：SSDP 发现（绑定物理网卡 + 网关单播兜底 + 只认默认网关）、`GetExternalIPAddress`、`AddPortMapping`、`GetSpecificPortMappingEntry`、`DeletePortMapping`、租期续租；NAT-PMP 作为备选。
2. 与 `runtime.py` 的 socket 复用：先建 socket，再映射其端口；映射失败不影响原流程。
3. 多 STUN：至少 3 个不同网络的服务，仍复用同一 socket；输出 `unique_mappings` 与 `public_ips`。
4. 调整对称判定：**只有双方都判为对称/未知时才跳过 L2**；单侧对称仍尝试，预算可缩短到 2 s。
5. UDP 被封快速识别：STUN 全失败或连续 ICMP 不可达 → 跳过 L2，直接 L3。
6. 事件与日志：`mapping_result`、`udp_blocked`、`symmetric_single_side`。

**阶段 P1（提升稳定性与可解释性）**

1. 同时打洞对齐：信令交换“开始时间戳 + 随机抖动”，两端在同一窗口内互相发包；保留 100 ms 间隔与令牌。
2. mapped 候选优先级与缓存（10 分钟 TTL，网络变化失效）。
3. 端口预测：仅当 `port_preservation` 显示固定步长时启用；限制 ≤2048 包 / ≤2 s。
4. 中继预取：L2 进行到 2 s 未成功即并行 `relay_allocate`（现有参数已定义，当前只是标记，未真实并行）。

**阶段 P2（可选/实验）**

1. 生日攻击扫描：默认关闭，需用户显式开启；限速并记录审计日志。
2. ICE 评估：仅在出现 WebRTC 互操作需求时启动。
3. PCP：ANNOUNCE 可用但 MAP 不可用（返回 NAT-PMP 格式 `result=3`），仅机会性尝试，不作为依赖。

---

## 七、最小 PoC 计划（本机 + 一台真实主机）

### 1. 目标与边界

- 目标 A：证明“本机 NAT + UPnP 映射”能被外部公网主机稳定命中（已部分完成）。
- 目标 B：证明“公网主机 ↔ 本机”的 UDP 双向数据面在 1080p30/2800 kbps 下可用。
- 目标 C（需要第二 NAT）：证明“锥形 ↔ 锥形”打洞成功，以及“对称/CGNAT ↔ 锥形”的行为符合预期。
- 边界：ECS（`47.109.32.249`）是公网主机、无 NAT，**不能**单独完成目标 C；目标 C 必须再加一个真实 NAT 出口（手机热点或第二条宽带）。

### 2. 拓扑

```
本机（家宽 115.150.84.112，锥形，UPnP 可用）
   │
   ├── ECS 47.109.32.249（公网，无 NAT；STUN / 外部探针 / UDP echo / 中继）
   │
   └── 第二 NAT（手机 5G 热点 或 第二家宽）——目标 C 才需要
```

### 3. 步骤、顺序与预期指标

| 顺序 | 动作 | 命令/入口 | 预期指标 | 失败判据 |
| --- | --- | --- | --- | --- |
| 1 | 基线验收 | `python scripts/router_acceptance.py --work evidence/router-acceptance/<时间戳> --map-test` | WAN 公网、EIM、端口保持、UPnP 可用、hairpin 不支持 | 任一项与基线不符，先查路由器 |
| 2 | UPnP 映射 + 外部探针 | 验收脚本建映射；ECS 向映射端口发 30 包 | ≥29/30 收到；未映射对照 0；删除后 0 | 收到 <29 → 映射未真正生效 |
| 3 | 本机 ↔ ECS UDP echo | ECS 监听 UDP，本机按 2800 kbps 发送 10 s | 丢包 <1%，RTT p95 ≤80 ms，无连续 2 s 无包 | 丢包 >3% 或 p95 >150 ms |
| 4 | ECS 主动入站到映射端口 | ECS 向 `115.150.84.112:<mapped>` 发包，本机回包 | 双向各 ≥2 响应，≥3 个 RTT 样本 | 单向成功即判失败 |
| 5 | 中继对照 | 现有 `relay.py`，1080p30 / 2800 kbps | 可解码帧连续 ≥10 s，RTT p95 ≤300 ms，丢包 <3% | 触发 `degrade_*` 任一阈值 |
| 6 | 目标 C：手机热点 ↔ 本机 | 手机 5G（通常 CGNAT/对称）作为对端 | 预期打洞失败并转中继；记录 `reason` | 若意外成功，记录映射与运营商 |
| 7 | 目标 C：第二家宽 ↔ 本机 | 第二条宽带（锥形）作为对端 | 双向成功，首包 ≤1 s，RTT p95 ≤100 ms | 双向 0 包 → 检查 hairpin/同出口 |

### 4. 判定口径

- **打洞成功**：双向各收到 ≥2 个响应，且 ≥3 个 RTT 样本（沿用 `connect-v1.md` 的 `success_min_responses=2`、`success_min_rtt_samples=3`）。
- **映射生效**：`GetSpecificPortMappingEntry` 返回正确内部端口/客户端，且外部主机正向收到 ≥95% 探测包。
- **直连可用**：10 s 内丢包 <2%，RTT p95 ≤100 ms，无连续 2 s 无可解码帧。
- **中继可用**：RTT p95 ≤300 ms，丢包 <3%，可解码帧连续 ≥10 s。

### 5. PoC 产出

- `evidence/poc/<时间戳>/`：验收 `result.json`、外部探针计数、UDP echo 统计、中继质量采样。
- 一份对比表：直连 vs 中继的 RTT p50/p95、丢包、首帧时间、码率稳定性。
- 结论只写“实测通过/未通过/未验证”，不把推算写成实测。

---

## 八、换路由器后的验收清单与探测方法

### 1. 单一入口（10 分钟内跑完）

```powershell
# Windows PowerShell，在仓库根目录执行
python scripts/router_acceptance.py --work "evidence/router-acceptance/$(Get-Date -Format yyyyMMdd-HHmmss)" --map-test
```

```bash
# macOS / Linux
python scripts/router_acceptance.py --work "evidence/router-acceptance/$(date +%Y%m%d-%H%M%S)" --map-test
```

说明：

- `--map-test` 会创建一个 **60 s 租期**的 UDP 临时映射，并在结束时 `DeletePortMapping`；不传则只做只读探测。
- `--gateway` 可省略，脚本默认按本机 `/24` 的 `.1` 推测，并同时向网关 1900 单播发现 IGD。
- 全流程约 20–40 s（SSDP 每组 2 s × 2 个 ST 类型，hairpin 2 s），远低于 10 分钟。
- 输出：`result.json`（结构化）+ 控制台中文摘要；建议同时保留控制台日志。

### 2. 五项探测：判定标准与脚本复用

| # | 探测项 | 判定标准 | 现有脚本复用 | 需补什么 |
| --- | --- | --- | --- | --- |
| 1 | WAN 是否公网 IPv4 | STUN 返回的地址不在 `10/8`、`172.16/12`、`192.168/16`、`100.64/10`、`169.254/16`、`127/8`、`0/8`、`224/4`、`240/4`；多个 STUN 返回**同一个** IP | `stun.py` 的 `bind_udp_in_pool` / `query_server` / `stun_servers` **可直接复用** | 多 STUN 公网 IP 需比对；脚本已输出 `public_ips`，不一致判“多出口/多层 NAT” |
| 2 | IPv6 前缀方式与可用性 | 有全局地址且能对 2 个目标建连才算可用；只有 `fe80::` 判不可用 | `native.py` 的 `enumerate_unicast_addresses` / `native_ipv6_probe` **可直接复用** | **PD 与 SLAAC 无法从主机侧区分**；需看路由器 WAN 状态页是否显示“前缀委派/DHCPv6-PD”，或抓 WAN 侧 DHCPv6 报文 |
| 3 | STUN 映射一致性与 NAT 类型 | 同一 socket、≥2 个 STUN、多轮：唯一映射 1 个 → endpoint-independent（锥形）；>1 个 → 对称型；公网 IP 不一致 → 多出口/多层 NAT | `stun.py` **可直接复用**；必须复用同一 socket | 建议扩到 ≥3 个不同网络的服务；脚本已支持 2 个并多轮 |
| 4 | UPnP / NAT-PMP / PCP 能否协商映射 | UPnP：收到网关 IGD 响应 + 存在 `WANIPConnection`/`WANPPPConnection` + `AddPortMapping` 成功 + `GetSpecificPortMappingEntry` 能读回；NAT-PMP：外部地址与 MAP 返回 `result_code=0`；PCP：ANNOUNCE 返回合法 PCP 头（`version=2`）且 MAP 返回 `version=2` 的合法映射（本机 MAP 失败） | **不能直接复用**：产品栈没有 UPnP/NAT-PMP/PCP 客户端 | 已在 `scripts/router_acceptance.py` 实现；产品化时抽到 `src/rdc/connect/mapping.py` |
| 5 | hairpin（内网回环） | 同一主机两个 socket：A 取 srflx，B 向 A 的公网映射发 10 包，收到 >0 判支持；0 判不支持 | `punch.py` **不能直接复用**（它需要真实对端候选与双向 Q/R）；`encode_probe`/`decode_probe` 可借用 | 验收脚本已用独立收发统计实现；如需更严格，应再从同网段另一台主机复测 |

### 3. 结果对应的策略调整

| 验收结果 | 策略结论 |
| --- | --- |
| WAN 公网 + EIM（锥形）+ IPv6 可用 | **回到 L1 优先**：IPv6 直连先行；L2 次之；L3 兜底 |
| WAN 公网 + EIM + 无 IPv6 | **维持 L2 优先**（当前策略）；L3 兜底 |
| WAN 公网 + 对称 + 对端锥形 | **仍尝试 L2**（单侧对称可穿，预算 2 s）；不要一刀切跳过 |
| 双方都对称/未知 | **直接 L3**（L2 预算缩到 0–1 s） |
| WAN 为 CGNAT（`100.64/10`） | **维持中继优先**：L2 只做 1.5 s 短预算尝试，失败立即 L3；不要依赖 UPnP（在 CGNAT 后无效） |
| 多个 STUN 返回不同公网 IP | 视为多出口/多层 NAT：**中继优先**，L2 只在明确同出口时尝试 |
| UDP 出站被封（STUN 全超时） | **跳过 L2，直接 L3**（TCP/TLS 443） |
| hairpin 不支持 | 同出口对端**只用 host 候选**，禁止用 srflx 回环（当前策略已如此） |
| UPnP 或 NAT-PMP 可协商 | 增加 `mapped` 候选，优先级 `mapped ≥ srflx`；映射与打洞同 socket；租期 ≤120 s 并续租 |
| 仅 PCP ANNOUNCE 有响应但 MAP 失败（NAT-PMP 格式 `result=3`） | 不纳入实现；保留机会性尝试 |

### 4. 换路由器后的 10 分钟流程

1. **0–2 min**：确认新路由器 WAN 拨号成功，记录 WAN IP 与是否获得 IPv6 前缀（看 WAN 状态页）。
2. **2–3 min**：跑单一入口（带 `--map-test`），保存 `result.json` 与控制台日志。
3. **3–5 min**：看五项结论。若 UPnP 为“被发现但不响应”，先重启路由器 UPnP 服务再跑一次；若为“被关闭”，在管理界面找到 UPnP 开关（不同厂商路径不同，以实际界面为准），开启后**只需重测** SSDP 发现、`AddPortMapping`、`GetSpecificPortMappingEntry` 三项。
4. **5–8 min**：若 NAT 类型从锥形变为对称，或 WAN 变为 CGNAT，按上表调整策略；不需要改代码，先改部署配置/预期。
5. **8–10 min**：把 `result.json` 归档到 `evidence/router-acceptance/<时间戳>/`，与本次基线对比。

### 5. 当前网络是否需要给出“小米固件 UPnP 开关位置”

**不需要。**本次实测结果为“可用”，未触发“被关闭”分支。为避免凭印象写错路径，本文不给出小米固件的具体菜单路径；如果未来某台路由器实测为“被关闭”，按以下通用方法处理：

1. 登录路由器管理页；
2. 在“高级设置 / 安全 / 防火墙 / 应用管理”一类菜单中查找 `UPnP` 或 `UPnP-IGD`；
3. 开启后重跑单一入口，确认从“被发现但不响应/无响应”变为“`AddPortMapping` + `GetSpecificPortMappingEntry` 成功”；
4. 若仍不生效，检查是否启用了“AP 模式/桥接模式”（此时 IGD 可能不存在）或运营商光猫还有一层 NAT。

---

## 九、当前网络 UPnP-IGD 实测（2026-11-10）

### 1. 结论

**可用（Available）。**不是“被关闭”，也不是“被发现但不响应”。

| 结论 | 判定依据 | 本次 |
| --- | --- | --- |
| **可用** | SSDP 有网关响应 + 设备描述含 `WANIPConnection`/`WANPPPConnection` + `AddPortMapping` 成功 + `GetSpecificPortMappingEntry` 能读回（+ 外部探针可达） | ✅ 命中 |
| 被关闭 | 组播与网关 1900 单播都无 SSDP 响应，或设备描述里没有 WAN 连接服务 | — |
| 被发现但不响应 | 能发现 IGD 与设备描述，但 SOAP 超时、返回 500/401，或 `AddPortMapping` 无有效响应 | — |

“被关闭”分支本次未触发，因此本文不凭印象写小米固件的菜单路径；通用处理见第八节第 5 小节。

- 设备：小米路由器 HD R3D，固件 2.28.17，网关 `192.168.31.1`
- IGD 实现：`MiWiFi/x UPnP/1.1 MiniUPnPd/2.0`，`LOCATION=http://192.168.31.1:5351/rootDesc.xml`
- 服务：`WANIPConnection:1`（控制 URL `/ctl/IPConn`），另有 `Layer3Forwarding:1`（`/ctl/L3F`）、`WANCommonInterfaceConfig:1`（`/ctl/CmnIfCfg`）
- 外部地址：`GetExternalIPAddress = 115.150.84.112`，与 STUN 反射地址**一致** → 单层 NAT，无 CGNAT，无双栈差异
- 映射：`AddPortMapping` 成功；`GetSpecificPortMappingEntry` 能读回内部端口/客户端/租期
- 外部可达：ECS `47.109.32.249` 向映射端口发 30 包，**30/30 收到**；未映射对照端口 **0/30**；删除映射后 **0/30**
- 租期：10 s 租约在 12 s 后条目消失（714）；120 s 续租为 60 s 生效
- hairpin：**不支持**，同一主机 10 发 0 收
- 清理：测试端口全部返回 714（NoSuchEntryInArray），无残留

### 2. 原始证据

| 证据 | 路径 |
| --- | --- |
| 验收结构化结果（本次） | `evidence/router-acceptance/20261110-route-eval/result.json` |
| 控制台输出 | `evidence/router-acceptance/20261110-route-eval/console.log` |
| SSDP 原始响应（默认网卡组播 0 条 + 网关单播 22 条） | `.../raw/ssdp-raw.json` |
| SSDP 显式网卡组播（28 条，含第二 IGD） | `.../raw/ssdp-multicast-explicit.json`、`.txt` |
| 设备描述 XML | `.../raw/device-description.xml` |
| `GetExternalIPAddress` 响应 | `.../raw/getexternalip.xml` |
| `AddPortMapping` 响应 | `.../raw/addportmapping.xml` |
| `DeletePortMapping` 响应 | `.../raw/deleteportmapping.xml` |
| 外部可达性（ECS 探针） | `.../raw/external-reachability.json` |
| 租期与续租 | `.../raw/lease-renewal.json` |
| PCP MAP 探测原始报文 | `.../raw/pcp-map.txt` |
| 清理核对 | `.../cleanup-check.txt` |

`GetExternalIPAddress` 原始响应：

```xml
<s:Envelope ...><s:Body><u:GetExternalIPAddressResponse
  xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">
  <NewExternalIPAddress>115.150.84.112</NewExternalIPAddress>
</u:GetExternalIPAddressResponse></s:Body></s:Envelope>
```

`AddPortMapping` 原始响应：

```xml
<s:Envelope ...><s:Body>
  <u:AddPortMappingResponse xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1"/>
</s:Body></s:Envelope>
```

外部探针关键数字（`external-reachability.json`）：

```json
{
  "mapped_hits": 30,
  "unmapped_control_hits": 0,
  "after_delete_hits": 0,
  "entry_after_delete_status": 500,
  "delete_ok": true
}
```

本次验收脚本的映射测试（`result.json` → `upnp`）：

```json
{
  "map_test": "added",
  "external_port_requested": 50213,
  "entry_ok": true,
  "entry_internal_port": "63138",
  "deleted": true,
  "deleted_verified": true
}
```

### 3. 实验中的关键坑（实现时必须避开）

1. **出向 SNAT 不复用静态映射。** 先对内部端口发过 STUN，再 `AddPortMapping`，STUN 仍显示旧端口；这不是映射失败。映射是否生效必须以 `GetSpecificPortMappingEntry` 和外部探针为准。
2. **组播 SSDP 默认出口错误。** 主机有 VMware `VMnet1/VMnet8`，默认组播出口落在虚拟网卡时组播 `ssdp:all` 收到 **0 条**（`ssdp-raw.json`）；显式 `IP_MULTICAST_IF=192.168.31.80` 后收到 **28 条**（`ssdp-multicast-explicit.json`）。实现必须绑定物理网卡，并对网关 1900 单播兜底。
3. **局域网存在第二个 IGD。** `192.168.31.12`（`Portable SDK for UPnP devices/2.0`，`gatedesc.xml`）也宣告 IGD；网关 `192.168.31.1` 甚至同时宣告 `:5351` 与 `:8200` 两个 `LOCATION`。必须只接受来源 IP == 默认网关、且服务列表含 `WANIPConnection`/`WANPPPConnection` 的 IGD。
4. **conntrack 残留会误导判断。** 复用刚发过包的端口做映射测试，容易看到旧映射；测试必须用“从未发包”的全新内部端口，并在结束后核对 714。
5. **租期必须真实续租。** 10 s 租约会过期；生产建议 120 s 并每 60 s 续租，退出时显式删除。
6. **hairpin 不能假设。** 本路由器不支持；同出口对端只能用 host 候选。

### 4. 对实现的影响

- **UPnP 进入 P0**：它能把“打洞碰运气”变成“路由器上确定的入站 UDP 端口”，在当前网络已被外部探针证实。
- **NAT-PMP 作为备选**：同一台路由器也支持 NAT-PMP（外部地址 + MAP 成功），可作为 UPnP 不可用时的第二条路。
- **PCP 不作依赖**：ANNOUNCE 返回合法 PCP 响应（`version=2, result=0`），但 PCP MAP 返回 NAT-PMP 格式 `00 81 00 03`（`result=3`），当前固件不支持 PCP 映射。
- **外部可达性验证不能省**：产品实现上线前，至少用一台公网主机做一次正向/对照/删除后三段验证；仅看 `AddPortMappingResponse` 不够。

---

## 十、未验证项、风险与边界

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 锥形 ↔ 锥形打洞 | **未验证** | 缺第二个独立公网出口；ECS 是公网主机，不是 NAT 出口 |
| 对称 ↔ 对称 | 已实测不可穿 | 走中继；端口预测只作为实验 |
| 单侧对称（本端对称、对端锥形） | **未验证** | 需要真实对称 NAT 对端；理论上可穿，建议按 P0 调整判定后实测 |
| CGNAT | **未验证** | 当前 WAN 非 CGNAT；手机 5G 可做对照 |
| IPv6 端到端 | **未验证** | 当前路由器无 IPv6；换路由器后按第八节验收 |
| UPnP 跨厂商兼容 | 仅小米 2.28.17 实测 | 其他固件可能“被发现但不响应”或映射不生效；需按验收清单复测 |
| PCP | ANNOUNCE 成功；MAP 不可用（`result=3`） | 不作为依赖 |
| ZeroTier 许可 | 已核实版本与条款 | 商业使用前仍需法务确认 |
| 端口预测命中率 | **未实测** | 当前网络端口保持，预测无意义；应在对称 NAT 环境单独实验 |
| 生日扫描 | **未实测** | 不建议进主线；如做实验必须限速并记录审计 |

风险提示：

1. 端口预测/扫描可能被运营商或企业 IDS 判定为扫描行为，导致限速或封禁；必须默认关闭、显式开关、严格限速。
2. UPnP 映射是入站口，必须配合令牌校验与租期清理；否则会扩大攻击面。
3. 所有“覆盖率”结论都受样本限制：目前只有一条家宽、一台路由器、一个 ECS；换网络后应按第八节重新验收。

---

## 十一、证据索引与复跑命令

### 1. 本次新增/使用的文件

| 文件 | 说明 |
| --- | --- |
| `scripts/router_acceptance.py` | 换路由器 10 分钟验收单一入口；复用 `rdc.connect.stun` / `native`；ruff 通过；未改 `src/` |
| `evidence/router-acceptance/20261110-route-eval/result.json` | 本次五项探测结构化结果 |
| `evidence/router-acceptance/20261110-route-eval/console.log` | 控制台摘要 |
| `evidence/router-acceptance/20261110-route-eval/raw/*` | SSDP/设备描述/SOAP/外部探针/租期原始证据 |
| `evidence/router-acceptance/20261110-route-eval/cleanup-check.txt` | 映射清理核对（714） |

### 2. 复跑

```bash
python -m ruff check scripts/router_acceptance.py
python scripts/router_acceptance.py --work evidence/router-acceptance/<时间戳> --map-test
python -m pytest tests/test_connect_policy.py tests/test_connect_runtime.py -q
```

### 3. 参考依据（外部）

- ZeroTier One 1.16.0 Release Notes（2025-08-21）：核心与服务改为 MPL-2.0，controller 改为商业 source-available
- ZeroTier `nonfree/LICENSE.md`：Source-Available License 1.0，商业使用需单独许可
- ZeroTier 仓库根 `LICENSE.txt`：旧版 BSL 1.1（1.4.4，Change Date 2025-01-01 → Apache-2.0）
- ZeroTier Enterprise Deployment Guide：CG-NAT/严格防火墙下“forced through relay servers”
- ZeroTier 博客《The State of NAT Traversal》：双对称 NAT 下打洞不可靠
- ZeroTier TCP Relay 文档：官方 relay “slow”，可自建
- PyPI `libzt` 1.8.4：License “Free for non-commercial use (BUSL 1.1)”，2022-01-03 后无新发布

---

## 十二、一句话总结

**把 UPnP/NAT-PMP、多 STUN、单侧对称也尝试打洞这三件事做进现有 L2，保留现有 TLS 中继兜底；不要为了“打洞”引入 ZeroTier 或一整门 ICE 运行时。ZeroTier 在双对称与 CGNAT 下仍要中继，也不能替代第二个独立公网出口。**
