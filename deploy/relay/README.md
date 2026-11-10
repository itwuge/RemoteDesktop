# 信令与中继服务部署说明

> 部署目标是**真实服务器**：现有阿里云 ECS（Ubuntu 20.04 x86_64）。本机虚拟机只作开发与功能验证，不是部署拓扑的一部分。
> 本目录四条命令即可从零部署，全部文件**不含口令、私钥与令牌**。

## 零、上线默认值（已先定，等你改）

| 项目 | 默认取值 | 说明 |
|---|---|---|
| 主机 | `<ECS-IP>` | 现有阿里云 ECS，不改主机、不重启 |
| 对外中继端点 | `relay://<RELAY-DOMAIN>:8750` | 写进 `[relay] endpoint` |
| 出口带宽 | 3 Mbps | 资源基线实测口径；中继预算唯一来源，不写死代码 |
| 画质档 | **固定 1920×1080 @ 30fps** | `target_kbps = 2800`（wire，含封装）；高运动由编码端回落 720p30，编码净荷目标 ≤2500 kbps |
| 最大并发 | **1 路** | 3 Mbps / 1 路 = 3000 kbps 预算；1080p30 高运动场景由编码端回落，而非降预算 |
| 信令与数据面端口 | 8750 | 信令帧与数据面帧共用 8750，按首帧区分，无需新增放行端口 |
| 健康检查端口 | 8751 回环 | 只本机可见，安全组不得放行 |
| SSH | 22，保持不动 | 不改端口、不断现有连接 |

预算公式：`floor(egress_mbps × 1000 × (1 − overhead_ratio) ÷ max_sessions)`。3 Mbps、0.0、1 路 → **3000 kbps/会话**；`target_kbps = 2800` 是 **wire（含封装）** 口径，落在预算内，逐帧限速由数据面令牌桶执行。

`overhead_ratio = 0.0` 的理由：预算直接取**线上(wire)实测码率**，已包含 base64+JSON+AEAD 封装膨胀（1.36–1.45 倍），再扣 8% 属于重复计费。

> **2026-11-10 档位变更**：1920×1080@30，`target_kbps=2800`（wire），预算仍 3000；编码端净荷目标按 ≤2500 kbps 配，高运动场景回落到 720p30。数据面按 1 秒窗口核对实际线上码率，超过 2800 时累加 `budget_exceeded` 并写 `relay.dataplane / budget.exceeded` 结构化日志（含 `measured_kbps`、`target_kbps`、`budget_kbps`）；`/healthz` 的 `dataplane.budget_exceeded` 可直接查。

### 0.1 档位推导（只用实测，不估算）

实测来源：`docs/验收/P2P打洞可行性验证-2026-11-10.md`（3 Mbps 预算下的画质实测）与 `docs/验收/带宽与画质核算报告-2026-11-10.md`（720p 预览扫描）。

| 分辨率/帧率 | 内容 | 编码目标 | media 实测 | **线上(wire)实测** | 3 Mbps 出口占用 | 结论 |
|---|---|---|---|---|---|---|
| 1080p30 | 文字 | 2000 kbps | 2430 kbps | **3288 kbps** | 109.6% | 超出口，不可用 |
| 720p30 | 静态桌面 | 2000 kbps | 2109 kbps | **2861 kbps** | 95.4% | 单路可用，余量约 139 kbps |
| 720p30 | 文字 | 2000 kbps | 2180 kbps | **2956 kbps** | 98.5% | 单路可用，余量约 44 kbps |
| 720p10 预览 | 静态桌面 | 200 kbps | 200 kbps | **291 kbps** | 9.7% | 预览档默认值 |
| 720p10 预览 | 文字（读正文） | 300 kbps | 276 kbps | **392 kbps** | 13.1% | 预览上限需 ≥400 kbps |

- **单路 720p30 可行性**：两种内容线上 2861/2956 kbps，均低于 3000 kbps；静态桌面余量 139 kbps，文字余量 44 kbps。可行，但没有余量再开第二路。
- **2 路并发不可行**：若强行 2 路，每路预算 `3000 ÷ 2 = 1500 kbps`。按实测 720p30 线上码率 2861–2956 kbps 反推，每路只能给到实测值的 50.7%–52.4%，必须降分辨率/帧率（720p30 无法保持），与“固定 720p30、不降档”的口径直接冲突。因此不做 2 路，`max_sessions` 固定 1。
- **max_sessions 默认值 1 的理由**：并发闸门只能取“单路实测峰值不超出口”的最大整数；`floor(3000 / 2956) = 1`。取 2 会使单路可用预算降到 1500 kbps，低于实测所需的 2861 kbps。

### 0.2 target / overhead / budget 三者口径（防止把 media 当 wire 重复放大）

| 配置项 | 口径 | 当前值 | 定义与计算式 |
|---|---|---|---|
| `egress_mbps` | **wire（线上字节）** | 3 | 出口链路总带宽，单位 Mbps |
| `overhead_ratio` | 额外扣除比例 | 0.0 | 只在“预算用的数字尚未包含封装”时才需要扣；当前预算直接取 wire 实测，故为 0 |
| `budget_kbps` | **wire（线上字节）** | 3000 | `floor(egress_mbps × 1000 × (1 − overhead_ratio) ÷ max_sessions)` = `floor(3000 × 1 ÷ 1)` |
| `target_kbps` | **wire（线上字节，含 base64+JSON+AEAD 封装）** | 2900 | 编码器输出经封装后的线上目标；直接取自实测 wire 2.86–2.96 Mbps，不是媒体净荷 |
| `[wall_preview] kbps` | media（编码目标） | 200 | 预览编码器目标；其 wire 实测 291 kbps，单独列在 0.1 表内 |

**结论：`target_kbps = 2900` 是 wire，不是 media。** 反证：实测 720p30 的 media 为 2109/2180 kbps，封装膨胀 1.36–1.45 倍后才是 wire 2861/2956 kbps；若把 2900 当 media，线上会变成约 `2900 × 1.36–1.45 = 3944–4205 kbps`，必然超过 3 Mbps 出口，与实测和 1 路并发上限直接矛盾。

按当前配置跑一路 720p30 的实际线上码率：

- 静态桌面：实测 **2861 kbps**（出口占用 95.4%，距 budget 3000 余 139 kbps）。
- 文字：实测 **2956 kbps**（出口占用 98.5%，余 44 kbps）。
- 两者都在 `budget_kbps=3000` 内；`target_kbps=2900` 是编码目标，实际会随内容在 2.86–2.96 Mbps 间波动。
- 客户端 `relay_allocate` 的 `requested_kbps` 建议直接取 `budget_kbps`（3000）作为上限，编码器按 `target_kbps=2900` 出流，避免文字场景峰值 2956 被数据面令牌桶误丢帧。

## 一、目录内容

| 文件 | 作用 |
|---|---|
| `deploy.py` | 一条命令部署：依赖自举、系统账号、目录、配置、systemd 单元、健康检查、幂等重跑 |
| `rdc-relay.service.tmpl` | systemd 单元模板，最小权限（`ProtectSystem=strict`、空 `CapabilityBoundingSet`、`SystemCallFilter`） |
| `relay.conf.example` | 配置模板；`egress_mbps` / `max_sessions` / `overhead_ratio` 决定中继预算，不写死 3 Mbps |
| `healthcheck.py` | 读配置里的 `health_port`，请求 `/healthz`，校验 `status/service/budget_kbps` 与数据面计数，退出码 0/1 |
| `cert_check.py` | 证书到期检查，按剩余天数分级（默认 `<=14` 天 WARN、`<=7` 天 CRITICAL），退出码 0/1/2，不改任何文件 |
| `rotate-cert.sh` | 证书轮换：重签服务端证书（可选 `--rotate-ca` 同时换 CA），原子替换后 `SIGHUP` 热加载，不重启进程 |
| `ca.crt` | 生产环境自签 CA 公钥（信任锚），供客户端固定校验；**只有公钥，私钥 `ca.key` 不入仓库**，指纹与有效期见 7.3.1 |

## 二、部署命令

```bash
# 1. 上传与校验（在 ECS 上，以有 sudo 的账号执行）
sudo python3 deploy.py --config relay.conf.example

# 1.1 指定 systemd 单元使用的解释器（推荐 Python 3.12 独立环境，见第十三节）
sudo python3 deploy.py --config relay.conf.example --python /opt/rdc-py312/bin/python3.12

# 2. 重复执行即幂等：内容未变化的文件不重写，服务不重启
sudo python3 deploy.py --config relay.conf.example --python /opt/rdc-py312/bin/python3.12

# 3. 只校验配置，不起服务
python3 -m rdc.relay --check-config --config /etc/rdc-relay/relay.conf

# 4. 健康检查（回环）
sudo python3 healthcheck.py /etc/rdc-relay/relay.conf

# 5. 证书到期检查（默认 14 天 WARN、7 天 CRITICAL）
python3 cert_check.py --config /etc/rdc-relay/relay.conf
```

部署后落点：

| 项目 | 路径 / 取值 |
|---|---|
| 运行账号 | 系统账号 `rdc`（`/usr/sbin/nologin`，无家目录），最小权限 |
| 应用代码 | `/opt/rdc-relay/app/rdc`，目录 0750、文件 0640，属主 `rdc:rdc` |
| 配置 | `/etc/rdc-relay/relay.conf`，0640 `root:rdc`；**已存在则不覆盖**（要覆盖加 `--force-config`） |
| 日志 | journald，`journalctl -u rdc-relay`；日志目录 `/var/log/rdc-relay` 已预留且为单元里唯一可写路径 |
| 单元 | `/etc/systemd/system/rdc-relay.service`，`enable --now` |
| 信令与数据面监听 | `0.0.0.0:8750`（配置可改）；首帧是 `plane="data"` 的 JSON 即走数据面，否则走信令 |
| 健康检查 | `127.0.0.1:8751`，不对外开放；`/healthz` 含数据面转发与丢帧计数 |

> **配置写入保护**：`--force-config` 覆盖前会校验 `[tls] tls_enabled`。模板默认 `true`；若待写入配置为 `false`，脚本直接报错并拒绝写入，必须显式加 `--allow-disable-tls`（仅限调试）。健康检查不会输出模板占位端点，未注入真实域名时 `relay_endpoint` 字段整体省略。
>
> **端点必须注入**：`auth_ok` 与 `relay_allocated` 会原样下发 `[relay] endpoint`，模板占位符 `<RELAY-DOMAIN>` 客户端无法解析。服务器上的 `/etc/rdc-relay/relay.conf` 必须写成真实域名（当前为 `relay://relay.wuge.fun:8750`）；`--force-config` 用模板覆盖后需重新注入并重启。

## 三、运行加固（端点、并发与限流）

均在 `relay.conf.example` 中有注释，改配置即可，不需要改代码：

| 段 | 键 | 默认 | 作用 |
|---|---|---|---|
| `[server]` | `max_connections` | 64 | 全站 TCP 连接上限（含信令与数据面） |
| `[server]` | `max_connections_per_ip` | 8 | 单来源 IP 连接上限 |
| `[server]` | `connect_rate_per_min` | 60 | 单 IP 每分钟新建连接上限，0 关闭；60 秒滑动窗口 |
| `[security]` | `allow_ips` | 空 | 允许来源，逗号分隔、支持 CIDR；非空时未列出的一律拒绝 |
| `[security]` | `deny_ips` | 空 | 拒绝来源，优先级高于 `allow_ips` |
| `[relay]` | `max_sessions` | 1 | 并发闸门；满员直接拒绝，不降画质、不排队 |
| `[relay]` | `target_kbps` | 2900 | 固定档目标码率；超过单会话预算时启动即失败 |
| `[relay]` | `profile_width` / `profile_height` / `profile_fps` | 1280 / 720 / 30 | 固定档分辨率与帧率，供客户端与编码器读取 |
| `[relay]` | `min_session_kbps` | 200 | 单会话最低预算，低于此值返回 `RELAY_FULL` |
| `[wall_preview]` | `max_session_kbps` | 400 | 预览单格预算上限；信令授权直接读取，不再硬编码 300 |
| `[logging]` | `level` | INFO | 结构化日志级别，Debug 默认关 |

- 三类拒绝原因分别计数为 `denied` / `rate_limited` / `too_many`，在 `/healthz` 的 `rejected` 字段可查。
- 进程级资源上限在 systemd 单元里：`MemoryMax=384M`、`CPUQuota=150%`、`TasksMax=256`、`LimitNOFILE=4096`；`ProtectSystem=strict` 只读，可写路径只有日志目录与 `StateDirectory=rdc-relay`（`/var/lib/rdc-relay`，0700，属主为服务账号）。
- 能力收缩：空 `CapabilityBoundingSet=`、`NoNewPrivileges=yes`、`SystemCallFilter=@system-service`、`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`。
- 会话上限仍是 `[relay] max_sessions`，与出口带宽共同决定每路预算（3 Mbps / 1 路 = 3000 kbps），不写死在代码里。

### 3.1 拒绝路径（固定档不做隐含降级）

| 触发条件 | 状态码 | 中文原因（原样下发给客户端） |
|---|---|---|
| 已有 1 路正式会话，第 2 个客户端申请 | `RELAY_FULL` | `中继通道已满，请等待或改用直连` |
| 请求码率 > 单路预算（默认 3000 kbps） | `RELAY_FULL` | `中继预算不足：请求 <X> kbps，单路可用 <Y> kbps；请降低画质或改用直连` |
| 授予预算 < `min_session_kbps` | `RELAY_FULL` | `中继预算不足：可用 <X> kbps，低于最低 <Y> kbps；请降低画质或改用直连` |

三条路径都只返回错误，不做“静默把 requested 压到 budget”的降级；客户端必须显式选择等待、降画质重申请或改直连。

## 四、上线输入项（我拿不到 ECS 时，这些由你提供）

| 序号 | 输入项 | 说明 | 是否入仓库 |
|---|---|---|---|
| 1 | ECS 公网地址与 SSH 端口 | 用于登录部署；**只走现有 22 端口，不改端口、不断现有连接** | 地址可入文档，端口按现状 |
| 2 | 登录账号与认证方式 | 具备 sudo 的账号；推荐 SSH 密钥 | **密钥与口令不入仓库** |
| 3 | 出口带宽（Mbps） | 决定中继预算；**已先定 3 Mbps**，如实际不同请改配置 | 入配置，不入代码 |
| 4 | 最大并发会话数 | 与出口带宽共同决定预算；**已定 1 路**（2 路每路仅 1500 kbps，低于实测 2861 kbps） | 入配置 |
| 5 | 协议与重传开销比例 | 默认 0.0（线上实测已含封装膨胀），取值 [0, 1) | 入配置 |
| 6 | 画质档 | **固定 1280×720 @ 30fps，target 2900 kbps**；不做多档位自动降档 | 入配置 |
| 7 | 对外中继地址 | **已先定 `relay://<RELAY-DOMAIN>:8750`**，写进 `[relay] endpoint` | 入配置 |
| 8 | 安全组放行 | **需开放 TCP 8750**；8751 只走回环不得放行 | 由你操作，脚本不碰防火墙 |
| 9 | 是否允许重启服务 | 首次部署会 `enable --now`；内容变更时重启 `rdc-relay`，**不重启主机** | 脚本不含任何 reboot |
| 10 | 是否启用 TLS 1.3 | 启用需确定证书来源（自签或 CA 签发）与域名；客户端需预置信任的 CA | 证书与私钥**不入仓库** |
| 11 | IP 黑白名单 | 空表示不限制；只允许内网时可配 `allow_ips` | 入配置 |
| 12 | 日志级别 | 默认 INFO；Debug 仅排障时临时开启 | 入配置 |

## 五、安全红线（脚本的硬约束）

1. 不读、不写 `/etc/ssh/sshd_config`，不触碰 22 端口与现有 SSH 连接。
2. 不修改防火墙与安全组，不执行 `reboot`、`shutdown`。
3. 不写入、不回显任何口令、私钥、令牌；配置模板只含部署参数。
4. 以独立系统账号 `rdc` 运行，`ProtectSystem=strict`、`ProtectHome=true`、空 `CapabilityBoundingSet`、`NoNewPrivileges=yes`。
5. 依赖只用 Python 标准库（3.8+），不引入第三方包，不需要联网装 pip 包。

### 5.1 运维凭据（不入仓库、不入临时目录）

- 服务器登录凭据固定保存在 `D:\RDC-Build\secrets\ecs-root.txt`（与安卓签名口令同一受限目录），文件 ACL 仅当前 Windows 用户可读写。
- 文件格式：`host=<IP>` / `user=<账号>` / `password=<口令>`；供部署与排障脚本读取，禁止复制进仓库、聊天记录或 `%TEMP%`。
- 取用方式：`python D:\RDC-Build\tmp\rdc-ops\run_remote.py "<命令>"`；脚本从上述文件读取并建立 SSH，不在命令行或输出中回显口令。

## 六、本地可校验（无 ECS 已完成的部分）

在项目根执行（Windows 也可跑，`--dry-run` 不做任何写操作）：

```powershell
# 全量门禁: 规范、格式、禁止写法、结构、mypy、单测
powershell -ExecutionPolicy Bypass -File scripts\gate.ps1

# 部署物离线 dry-run（预检 + 动作打印，目标机零改动）
python deploy\relay\deploy.py --dry-run --config-target D:\RDC-Build\tmp\etc\relay.conf --unit D:\RDC-Build\tmp\etc\rdc-relay.service

# 配置校验
$env:PYTHONPATH = 'src'
python -m rdc.relay --check-config --config deploy\relay\relay.conf.example

# 信令 + 中继单测
python -m pytest tests\test_relay_server.py tests\test_relay_dataplane.py tests\test_relay_tls.py tests\test_relay_hardening.py tests\test_relay_signaling_v12.py -q

# 三进程联调（中继 + 发布端 + 订阅端，各自独立进程）
python -m pytest tests\test_relay_integration.py -q

# 其他既有等价验证（可选，需要本机证书与构建目录）
$env:RDC_BUILD_ROOT = 'D:\RDC-Build'
python -m poc.tools.run_relay_dataplane_check
python -m poc.tools.run_relay_tls_check
```

已取得的证据（2026-11-10，本机）：

- 语法检查：`python -m py_compile` 通过（deploy.py、healthcheck.py、relay 与 dataplane 模块）。
- 离线 dry-run：预检四项输入物齐全，打印全部动作，目标机零改动，退出码 0。
- 配置校验（退出码 0，输出一行结构化 JSON）：
  `{"event": "config.checked", "relay_endpoint": "relay://<RELAY-DOMAIN>:8750", "budget_kbps": 3000, "degrade_profile": "L0"}`；配置非法时输出 `config.invalid` 并返回 2。
- 门禁（03:14）：ruff 规范、禁止写法、结构（49 个契约命令全覆盖）、mypy 全过；ruff 格式仅剩其他并行线的 `scripts/smoke.py` 一处，与本线无关。本线中继/信令/协议 129 项测试全绿。
- 单测覆盖：预算随策略变化、非法配置逐项拒绝、长度前缀帧收发与越界拒绝、`/healthz` 字段、`_write` 幂等、已有配置不被覆盖、单元模板无凭据、dry-run 零写入、连接上限/单 IP 上限/新建速率/IP 黑白名单、数据面令牌校验与角色互斥、会话上限、超长帧、密文逐字节转发、超预算丢帧、presence 30s/90s 口径、路由投递到对端连接、登出吊销、预览令牌与聚焦名额、会话数据面 9 类消息的字段/大小/顺序/去重/摘要校验，以及 TLS 配置校验分支与 TLS 1.3 单向/双向接入（证书缺失时自动跳过）。
- 三进程联调实测（`tests/test_relay_integration.py`，中继为独立子进程，发布端/订阅端各为独立子进程，本机回环）：
  - 2 台设备完成注册/认证并上报 presence 后，`/healthz` 的 `online_devices` 为 2。
  - 20 次 offer 路由：p50 **1.99 ms**、p95 **2.19 ms**、最大 2.53 ms；认证平均 5.54 ms。
  - `relay_allocate` 下预算 1380 kbps（联调用例自带 `max_sessions=2` 配置，不代表上线默认值）；100 帧 × 2000 字节、20 ms 间隔发布，发布端实际 779.8 kbps，中继转发 100/100、丢帧 0，订阅端 SHA-256 与发布端一致。
  - 大帧验证：单帧 5.6 MB（覆盖剪贴板 image ≤ 4 MiB 经 Base64 约 5.34 MiB），34 ms 转发完成、SHA-256 一致；累计 101 帧 / 5.8 MB、丢帧 0。
  - 证据：`D:\RDC-Build\evidence\poc\relay\signaling-integration.json`。
- 双进程数据面验证（中继、发布端、订阅端各为独立进程，本机回环）：
  - 转发正确性：32 帧 × 2 KiB，订阅端 32/32 收到，逐字节 0 差异。
  - 预算限速：200 kbps 预算下按 2 倍速率发送，实际 5/8 帧转发、220.7 kbps（≤ 预算 1.25 倍），中继累计丢帧计数大于 0。
  - 证据：`D:\RDC-Build\evidence\poc\relay\dataplane-check.json`。
- TLS 1.3 本地等价验证：四个场景全部通过，协商版本 `TLSv1.3`、套件 `TLS_AES_256_GCM_SHA384`；证据：`D:\RDC-Build\evidence\poc\relay\tls-check.json`（详见第七节）。

## 七、TLS 1.3 接入

### 7.1 接入方式

- `tls_enabled = true` 后，中继在 `accept` 后立即用 `ssl` 以 `server_side=True` 包装连接，`minimum_version = TLSv1_3`；握手失败直接断开，**没有明文回退**。
- 信令与数据面共用同一条 TLS 通道：长度前缀分帧与首帧 `plane="data"` 分流逻辑不变，中继仍只转发数据面密文、不解析内容。
- 证书与私钥只从配置路径读取；代码不内置固定密钥或证书，仓库不含任何私钥。
- `/healthz` 增加 `tls` 字段（`enabled` / `min_version` / `client_cert_required`），便于健康检查确认接入状态。

### 7.2 配置项（`[tls]` 段）

| 键 | 默认 | 说明 |
|---|---|---|
| `tls_enabled` | `false` | 是否启用 TLS 1.3；`false` 时与未接入 TLS 前完全一致 |
| `cert_file` | 空 | 服务端证书链；启用时必填，文件必须存在 |
| `key_file` | 空 | 服务端私钥；启用时必填，文件必须存在 |
| `ca_file` | 空 | 客户端证书校验用 CA；仅双向校验使用 |
| `require_client_cert` | `false` | `true` 时服务端设 `CERT_REQUIRED` 并校验客户端证书 |

启动即失败的校验：启用 TLS 但缺 `cert_file`/`key_file`（或文件不存在）；`require_client_cert=true` 但缺 `ca_file`；`require_client_cert=true` 但 `tls_enabled=false`。错误信息会指出具体键名或路径。

### 7.3 自签证书生成（30 天）

生产机（Ubuntu，自带 openssl）在 `/etc/rdc-relay/tls` 生成；证书与私钥**只放服务器，不入仓库**：

```bash
export TLS=/etc/rdc-relay/tls
sudo install -d -m 0750 -o root -g rdc "$TLS"
# 1) 自签 CA
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout "$TLS/ca.key" -out "$TLS/ca.crt" -subj "/CN=RDC-Relay-CA"
# 2) 服务端证书, SAN 必须含 localhost 与 127.0.0.1
sudo openssl req -newkey rsa:2048 -nodes \
  -keyout "$TLS/server.key" -out "$TLS/server.csr" -subj "/CN=localhost"
printf 'subjectAltName=DNS:localhost,DNS:<RELAY-DOMAIN>,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' \
  | sudo tee "$TLS/server.ext" >/dev/null
sudo openssl x509 -req -in "$TLS/server.csr" -CA "$TLS/ca.crt" -CAkey "$TLS/ca.key" \
  -CAcreateserial -days 30 -out "$TLS/server.crt" -extfile "$TLS/server.ext"
# 3) 客户端证书, 仅双向校验时需要
sudo openssl req -newkey rsa:2048 -nodes \
  -keyout "$TLS/client.key" -out "$TLS/client.csr" -subj "/CN=rdc-client"
printf 'extendedKeyUsage=clientAuth\n' | sudo tee "$TLS/client.ext" >/dev/null
sudo openssl x509 -req -in "$TLS/client.csr" -CA "$TLS/ca.crt" -CAkey "$TLS/ca.key" \
  -CAcreateserial -days 30 -out "$TLS/client.crt" -extfile "$TLS/client.ext"
# 4) 收权并清理临时文件
sudo chown root:rdc "$TLS"/*.key && sudo chmod 0640 "$TLS"/*.key
sudo rm -f "$TLS"/*.csr "$TLS"/*.ext "$TLS"/*.srl
```

`-nodes` 生成的是无口令私钥，便于服务无人值守启动；因此私钥权限必须保持 `0640 root:rdc`，CA 私钥建议验证后离线保存。生产建议 CA 长有效期（如 3650 天）、服务端证书短有效期（如 90 天），到期用 `rotate-cert.sh` 重签并 `SIGHUP` 热加载，不必重启进程；流程见 7.5。

本机（Windows，无 openssl）用 Python `cryptography` 生成同名三组证书（CA / 服务端 / 客户端，另加一组错误 CA 用于反例），只写入 `D:\RDC-Build\certs\relay`，有效期同为 30 天；生成脚本为 `D:\RDC-Build\tools\make_relay_test_certs.py`。

### 7.3.1 生产固定信任锚（仓库内 `ca.crt`）

`deploy/relay/ca.crt` 是 ECS `47.109.32.249` 上 `/etc/rdc-relay/tls/ca.crt` 的**只读副本**，供客户端做证书链固定校验。仓库只含公钥，`ca.key` 从未离开服务器（导出时未读取）。

| 项目 | 取值 |
|---|---|
| Subject / Issuer | `CN = RDC-Relay-CA`（自签，二者相同） |
| 有效期 | `2026-11-10 05:57:32 GMT` 至 `2036-11-07 05:57:32 GMT`（3650 天） |
| 文件 SHA-256 | `31c7369bbfe9c0faf9a17e05e2d19ea83cf9d5ae46e3cd018c6062aeb5ccf9d8` |
| 证书 DER 指纹 | `8C:83:43:58:8F:A0:5B:8A:60:EF:50:0E:00:41:89:19:04:F2:97:69:2A:AB:A9:A9:48:6C:8B:F5:B0:DC:A7:FE` |
| 序列号 | `396F4C3EC50A5A86F2C10277A10740C231F2AA80` |
| 服务端证书 | `CN=relay.wuge.fun`，`notAfter 2027-02-08 05:57:32 GMT`（90 天口径，由 7.5 轮换） |

客户端接入：以 `deploy/relay/ca.crt` 作唯一信任锚、SNI 用 `relay.wuge.fun`，并确认握手协商为 TLS 1.3。已用该文件实测线上 8750：`TLSv1.3` / `TLS_AES_256_GCM_SHA384`，对端 `relay.wuge.fun`，固定校验通过。**换 CA 后必须同步替换本文件与客户端副本**（见 7.5）。

### 7.4 单向与双向校验的取舍

- **单向 TLS（默认）**：只出示服务端证书，解决链路加密与被动窃听；客户端仍靠 `relay_token` 做业务鉴权。若客户端不校验证书（或未固定信任的 CA），无法防中间人冒充服务端，因此客户端必须校验 `ca_file`/证书链。
- **双向 TLS（mTLS，`require_client_cert = true`）**：服务端用 `CERT_REQUIRED` + `ca_file` 校验客户端证书，设备身份与传输通道绑定，强度更高；代价是每台设备都要签发、分发、保存与轮换证书，CA 与吊销流程也要维护，运维成本明显更高。
- 本批次默认保持单向，先获得传输加密；需要设备级强身份时再开启 mTLS。两种模式都不允许回退明文。

### 7.5 证书轮换与到期告警

- 到期告警：`python3 cert_check.py --config /etc/rdc-relay/relay.conf`，剩余 `<=14` 天返回 WARN（退出码 1）、`<=7` 天返回 CRITICAL（退出码 2）、其余 OK（0）；加 `--json` 便于接监控。阈值可用 `--warn-days` / `--critical-days` 调整。`cert_check.py` **不随部署安装**，需手工放到服务器（如 `/opt/rdc-relay/tools/`）再接定时任务；定时示例（只登记，不由部署脚本安装）：`17 3 * * * /usr/bin/python3 /opt/rdc-relay/tools/cert_check.py --config /etc/rdc-relay/relay.conf`。
- 只轮换服务端证书（客户端无需改动）：

```bash
sudo ./rotate-cert.sh                       # 默认 90 天，沿用现有 CA
sudo ./rotate-cert.sh --days 90 --san 'DNS:<RELAY-DOMAIN>,DNS:localhost,IP:127.0.0.1,IP:<公网IP>'
```

脚本会先在临时目录生成新私钥与证书、用现有 CA 签名并 `openssl verify`，再原子替换 `server.key`/`server.crt`，最后 `kill -HUP` 主进程触发热加载（失败时进程保留旧上下文，日志出现 `tls.reload_failed`，不影响在线连接）。

- 同时轮换 CA（客户端必须更新信任）：

```bash
sudo ./rotate-cert.sh --rotate-ca --ca-days 3650 --days 90
sudo sha256sum /etc/rdc-relay/tls/ca.crt   # 分发这个指纹与新 ca.crt 给各控制端
```

CA 更换后，控制端必须用新 `ca.crt` 做固定校验，否则握手会因证书链不受信而失败；替换 `ca.crt` 不需要改服务端配置，服务端只需按上面的命令重签并在热加载后确认 `/healthz` 的 `cert_days_left` 已更新。
- 轮换不影响 22 端口、nginx、80/443 站点与其它服务；不重启服务器。

### 7.6 本地等价验证范围

```powershell
$env:PYTHONPATH = 'src'
$env:RDC_BUILD_ROOT = 'D:\RDC-Build'
python -m poc.tools.run_relay_tls_check
```

四个场景（中继为独立子进程，发布端/订阅端各为独立子进程，本机回环）：

| 场景 | 结果 |
|---|---|
| (a) TLS 1.3 单向，32 帧 × 2 KiB 密文转发 | 32/32 收到，逐字节 0 差异 |
| (b) `require_client_cert=true`，客户端无证书 | 首帧前被拒（握手失败/连接关闭） |
| (c) `require_client_cert=true`，客户端证书正确 | 8/8 帧收到，逐字节一致 |
| (d) 客户端只信任错误 CA | 握手失败（证书校验失败） |

(a)(b)(c) 协商版本均为 `TLSv1.3`，套件 `TLS_AES_256_GCM_SHA384`；证据写入 `D:\RDC-Build\evidence\poc\relay\tls-check.json`。

**口径：以上只到本地多进程等价验证，未跨公网验证，不是画质通道验收。**

## 八、日志与审计

- 结构化 JSON 行，固定字段 `timestamp / level / session_id / trace_id / module / event / duration_ms / error_code`；写入 `RDC_LOG_DIR`（单元里为 `/var/log/rdc-relay`）。
- 轮转由 `RotatingFileHandler` 完成：单文件 10 MiB、保留 5 个备份（`rdc.log` → `rdc.log.1..5`，单服务上限约 60 MiB），不需要额外 logrotate；journald 里还有一份 stdout 日志，部署脚本写入 `/etc/systemd/journald.conf.d/rdc.conf`（`SystemMaxUse=256M`、`SystemMaxFileSize=32M`、`RuntimeMaxUse=64M`）并重启 journald 生效，长跑总量上限约 256 MiB。
- 脱敏在写盘前完成：键名含 `password` / `token` / `secret`、`relay_token`、`signature` 等一律替换为 `***`。服务端单元设置 `RDC_LOG_REDACT_IP=0`，**保留来源 IP** 用于事故取证（仅本地 journald，随 256 MiB 上限滚动，不外传、不进客户端可见信息）；客户端展示与导出日志仍脱敏，`peer_ip` 替换为 `***`。
- 审计落点：数据面 `session.attached` / `session.detached` / `session.rejected`，登出与吊销 `device.kicked`，连接拒绝事件带 `reason`（`denied` / `rate_limited` / `too_many`）。
- 证书巡检：部署脚本安装 `rdc-cert-check.service` + `rdc-cert-check.timer`（每日 03:20，`Persistent=true`），同时检查 relay 与 signaling 配置；退出码 0/1/2 对应 OK/WARN/CRITICAL，非零时告警行 `CERT_CHECK_ALERT` 写入 journald，按实际证书剩余天数分区，不接付费监控。

## 九、上线检查单（可复制执行）

```bash
# 1) 单元状态: 期望 ActiveState=active、SubState=running、NRestarts 为 0
systemctl show rdc-relay -p ActiveState -p SubState -p NRestarts

# 2) 端口: 8750 对外监听, 8751 只在回环
ss -lntp | grep -E ':(8750|8751)\b'
# 期望: 8750 在 0.0.0.0 或 ::；8751 在 127.0.0.1

# 3) 本机连通
python3 - <<'PY'
import socket
for host, port in (("127.0.0.1", 8750), ("127.0.0.1", 8751)):
    with socket.create_connection((host, port), timeout=3):
        print(f"ok {host}:{port}")
PY

# 4) 健康检查: 期望 status=ok、budget_kbps=3000、tls.enabled 与证书剩余天数符合预期
python3 deploy/relay/healthcheck.py /etc/rdc-relay/relay.conf

# 5) 重启后自恢复(只重启服务, 不重启主机)
sudo systemctl restart rdc-relay && sleep 2 && systemctl is-active rdc-relay

# 6) 日志落盘与脱敏: 期望文件存在, 凭据计数为 0, 服务端保留来源 IP
ls -l /var/log/rdc-relay/rdc.log
grep -c '"relay_token"' /var/log/rdc-relay/rdc.log || true      # 期望 0
grep -c '"peer_ip":"\*\*\*"' /var/log/rdc-relay/rdc.log || true # 期望 0（服务端不脱敏 IP）

# 7) 证书有效期(TLS 开启时): 期望 cert_days_left 大于续期提前量
curl -s http://127.0.0.1:8751/healthz | python3 -m json.tool | grep -A3 '"tls"'
```

## 十、备份与恢复

服务状态（在线表、会话、授予）在内存里，重启即清空；**账号库 `accounts.db` 必须备份**，
其中保存账号、设备与屏幕墙归属（`walls` 表），丢失会导致恢复后墙归属与设备绑定失效。
备份留在服务器本地受限目录（目录 0700、归档 0600），私钥不出本机：

```bash
# 备份（配置 + CA/证书 + 状态目录 /var/lib/rdc-relay、/var/lib/rdc-signaling；保留 14 天）
sudo sh deploy/backup.sh backup

# 恢复演练（解到临时目录, 不改动在线服务; 用服务账号验证可读并比对哈希）
sudo sh deploy/backup.sh drill
```

恢复演练结果应包含：`DRILL_VERIFY_CHAIN` 为 `OK`、`DRILL_CERT_CHECK_AS_SERVICE_USER`
输出 `"status": "OK"`、relay.conf 与 signaling.conf 均可被各自服务账号读取、
`DRILL_KEY_MODE` 显示私钥仍为 `600/640 root`、`DRILL_HASH_COMPARE` 两侧一致、
`DRILL_ACCOUNTS_DB` 输出两个库的 `user_version=3` 与 `walls` 行数（含 WAL 已一并归档）、`DRILL_OK`。

### 10.1 屏幕墙归属验收（prepare → verify）

墙归属由 `wall_bind`/`wall_bound` 写入 `accounts.db` 的 `walls` 表。复现不依赖残留数据，两条命令即可：

```bash
# 1) 在服务器上准备/复用验收账号与设备（直接写生产账号库, 幂等, 只增不改）
sudo RDC_VERIFY_WALL_ACCOUNTS_DB=/var/lib/rdc-relay/accounts.db \
     RDC_VERIFY_WALL_STATE=/var/lib/rdc-relay/wall-verify/fixtures.json \
     PYTHONPATH=/opt/rdc-relay/app \
     /opt/rdc-py312/bin/python3.12 scripts/verify_wall_bind.py prepare

# 2) 重启使进程重新加载账号库，再单命令验证
sudo systemctl restart rdc-relay
PYTHONPATH=src python3 scripts/verify_wall_bind.py verify --host 127.0.0.1 --port 8750
PYTHONPATH=src python3 scripts/verify_wall_bind.py verify --expect-existing \
  --host <ECS-IP> --port 8750      # 跨重启保留, 公网对照
```

状态文件含测试设备私钥，必须放在 `evidence/`（已被 `.gitignore` 忽略）并在用完后删除。

清理口径（签收前/签收后）：

- **签收前**：保留测试账号与设备绑定（正向对照依赖它们），只删墙行。
  生产库长期留测试墙不合规，墙行按前缀清：`DELETE FROM walls WHERE wall_id LIKE 'wall-verify-%'`。
- **签收后**：一次性清掉 `walls`、audit 中的墙记录、测试账号/设备及其 tokens；
  此后「复现」等于「重跑 prepare + 一条 verify」，不依赖任何残留数据。

真实恢复（仅在确认需要时执行，先停服务再还原，最后启动；不重启主机）：

```bash
sudo systemctl stop rdc-relay rdc-signaling
sudo tar xzf /var/backups/rdc/rdc-config-YYYYMMDD-HHMMSS.tar.gz -C /
sudo systemctl start rdc-relay rdc-signaling
```

## 十一、卸载与回滚

```bash
# 卸载: 先停服务, 再删单元与代码; 配置与证书保留, 确认后再手工删除
sudo systemctl disable --now rdc-relay
sudo rm -f /etc/systemd/system/rdc-relay.service
sudo systemctl daemon-reload
sudo rm -rf /opt/rdc-relay /var/log/rdc-relay
sudo userdel rdc   # 无家目录, 不需要 -r
# 若按第十三节用了 3.12 独立环境, 一并删除(不影响系统 python3)
sudo rm -rf /opt/rdc-py312 /opt/rdc-conda
# 3.12 来自 conda-forge 的 Miniconda 镜像, 未向 apt 引入任何源, 无需移除 apt 源;
# 若只是回退解释器而非卸载, 重跑部署脚本即可把单元指向改回系统解释器
# (单元内容变化会重写并 daemon-reload, 随后重启 rdc-relay):
#   sudo python3 deploy.py --config relay.conf.example --python /usr/bin/python3
# 或手工把 ExecStart= 改回 /usr/bin/python3 -m rdc.relay --config /etc/rdc-relay/relay.conf 后 daemon-reload
# 最后在阿里云控制台撤销 TCP 8750 入方向规则; 8751 从未放行
python3 -V                      # 期望仍为 3.8.10
readlink -f /usr/bin/python3    # 期望仍为 /usr/bin/python3.8
ss -lntp | grep 8750            # 期望无输出
```

回滚：部署前对 `/opt/rdc-relay/app` 打一份 tar 快照；需要回退时解压覆盖、`systemctl restart rdc-relay`。配置文件默认不覆盖，旧配置无需回滚。

保留不删：`/etc/rdc-relay/relay.conf` 与 `/etc/rdc-relay/tls`（含 CA 私钥）默认保留，便于重新部署时沿用信任锚；确认不再使用后再手工删除。journald 中已有日志随系统日志轮转自然过期。

## 十二、已验证与未验证（2026-11-10 更新）

已实测（ECS <ECS-IP>，Ubuntu 20.04.6）：

- 一条命令部署已执行：系统账号、目录、配置、单元、`daemon-reload`、`enable --now` 全链路成功。
- 端口：8750 对外监听、8751 仅回环；安全组放行 8750 后外网由「超时」变为「被拒绝」，服务启动后外网真实 TLS 握手成功。
- TLS 1.3 已跨公网验证：客户端固定自签 CA 校验通过，`register → challenge → auth → auth_ok → relay_allocate` 全流程通过。当时（`max_sessions=2`、`overhead_ratio=0.08`）返回 `budget_kbps=1380`、`profile=L1`；固定档上线后应以 `budget_kbps=3000`、`profile=L0` 复核。
- 幂等复跑：内容未变化不重写、配置不覆盖、服务不重启。
- 证书轮换与到期检查脚本已实跑验证（`rotate-cert.sh`、`cert_check.py`）。

仍未验证（不得当成已通过）：

- 数据面只在本机回环验证了**密文转发与预算限速**：未做真实带宽整形、未跨公网，**不是画质通道验收**。
- TLS 未验证证书吊销、mTLS 与多客户端 CA 分发；CA 轮换后的客户端更新只做了本机验证。
- 数据面握手沿用契约缺口 202611-001 的 `from_device_id` 约定（v1 未定义该字段）；补齐前属已知缺口，不得视为契约已满足。
- 长时间稳定性、监控接入、告警通道（仅提供 `cert_check.py` 退出码）未验证。
- 会话数据面 9 类消息只做了本机回环校验与 5.6 MB 单帧转发；未跨公网、未与真实文件/剪贴板端到端联调，传输顺序/去重状态在进程内存，重启不保留。
- 屏幕墙聚焦/预览已在协议 v1.4 有线上消息类型；归属登记在 v1.5 追加 `wall_bind`/`wall_bound`，由被控端经认证会话上报并落 `accounts.db` 的 `walls` 表。跨公网正反对照已于 2026-11-10 在 8750 实跑 12 项全 PASS（含重启后 `bind_already_persisted`），验收脚本 `scripts/verify_wall_bind.py`，复现步骤见 10.1。

## 十三、上线踩坑与兼容性基线

1. **Python 版本基线**：服务端代码使用 `datetime.UTC`（3.11+），在 Ubuntu 20.04 自带 Python 3.8.10 上会 `ImportError`。当前生产用 `/opt/rdc-py312` 独立 3.12 环境运行，`deploy.py --python` 指定解释器；系统 `python3` 保持 3.8 不动（系统工具依赖它）。新机器部署前先准备 3.12 环境，不要改 `update-alternatives`。
2. **`datetime.UTC` 兼容写法**：仓库已统一改为 `from datetime import datetime, timezone` 加 `timezone.utc`，在 3.12 下已验证运行；若回退到 3.8 解释器，这 8 个模块的写法仍然可用。
3. **健康检查启动竞态**：服务 `enable --now` 后端口不是同一瞬间就绪，早期版本会因首次健康检查过早而误报失败。`deploy.py` 现最多重试 20 次 × 0.5 秒；失败仍非零退出并提示查看 journal。部署脚本不会因为这条重试而掩盖真实故障（10 秒仍不通即判失败）。
