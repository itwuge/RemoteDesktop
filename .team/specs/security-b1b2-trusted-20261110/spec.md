# 发行前复验：B1/B2 本地实例回归 + 免码不得绕过设备鉴权

状态：已执行（2026-11-10 21:08，含 T 项正向对照与令牌吊销；B1/B2/T 均通过，42/42 断言）
范围：只做复验与出证据。不改 `src/`、不改契约正文、不碰 `android/`、不接触 ECS/安全组/公网 8750。
证据目录：`D:\RDC-Build\evidence\security\20261110-b1b2-trusted-reverify`（执行时若已存在则加后缀 `-2`）。

## 1. 任务拆解（与用户口径一一对应）

| 用户要求 | 本 spec 落点 |
| --- | --- |
| 本地/虚机实例跑 session-end 场景 | 场景 B1（第 3.1 节） |
| 本地/虚机实例跑 revoke 场景 | 场景 B2（第 3.2 节） |
| 新增发行前必测：免授权不得绕过设备鉴权 | 场景 T（第 3.3 节），正向对照不成立即不通过 |
| 公网 8750 等部署窗口 | 非目标，本轮不执行、不探测 |
| 独立客户端、不复用项目 relay 脚本 | 第 2 节 |
| 不改 src、不碰 ECS 配置与安全组 | 第 6 节约束 |

## 2. 独立客户端与实例

### 2.1 客户端

- 在本轮证据目录新写单文件客户端 `b1b2_trusted_verify.py`，协议实现自包含：
  长度前缀帧、信封 canonical JSON + Ed25519、register/challenge/auth、offer/answer、
  session_start/session_end、relay_allocate、数据面 relay_hello、hs=2（X25519 + HKDF + 签名校验）。
- 只使用标准库与 `cryptography`；**不 import** 项目 `rdc.*` 的客户端/中继脚本，不调用
  `scripts/`、`poc/tools/`、中继线 `tools/probe_client.py`。
- 允许 import 的只有「被测服务」的启动装配：`RelayServer/RelayConfig`、
  `AccountCenter`、`AccountStore`、`AccountRevocationProbe`。

### 2.2 被测实例对接方式（优先中继线实例）

1. 优先：若环境变量 `RDC_LOCAL_RELAY_ENDPOINT`（形如 `host:port`）与 `RDC_LOCAL_RELAY_CA`
   已提供，直接连中继线用同一棵树起的实例，不自行起服务；`run.json` 记录 `instance_source=provided`。
2. 回退：未提供时，测试进程自行起隔离实例（`127.0.0.1` 随机数据端口 + 随机健康端口、
   临时 `accounts.db`、退出即删），`run.json` 记录 `instance_source=self`。
3. 两种方式使用完全相同的客户端与断言；结论中注明实例来源。

### 2.3 本地 TLS

账号中心 `/v1` HTTP 只在 TLS 启用时与信令共用端口（`relay/server.py:780`），因此统一 TLS：

- 自起实例时用 `cryptography` 现场生成 CA 与服务端证书（SAN：`IP:127.0.0.1`、`DNS:localhost`），
  私钥只落临时目录，不打印、不入证据。
- 客户端固定该 CA、`minimum_version=TLSv1_3`、`check_hostname=True`、`server_hostname=localhost`。
- 中继线提供实例时，使用其给出的 CA 路径；不读取、不复制线上私钥。

## 3. 场景与判定

### 3.1 B1：会话结束即断数据面

准备：两端完成信令认证；控制端 `offer`、被控端 `answer`；`session_start`；
被控端 `relay_allocate` 取 token；publisher/subscriber 接入数据面并各收到 `relay_ready`。

| 编号 | 断言 | 通过条件 |
| --- | --- | --- |
| B1.1 | 结束前 publisher→subscriber 转发 1 帧 | 对端收到同一帧（正向对照） |
| B1.2 | 被控端发 `session_end` | 控制端收到 `type=session_end` |
| B1.3 | 结束后 2 秒内两端数据面连接 | 均读到 EOF/关闭 |
| B1.4 | 结束后 publisher 再发 3 帧 | subscriber 收到 0 帧；healthz `total_frames_forwarded` 不再增长 |
| B1.5 | 旧 `relay_token` 重连数据面 | 收到 `relay_error` 且连接被关闭 |
| B1.6 | 结束后再发 `relay_allocate` | 返回 `error`，无 `relay_allocated` |
| B1.7 | healthz | `revoked_tokens>=1`、`revoked_sessions>=1`、`sessions=0` |

B1 通过 = B1.1–B1.7 全过；任一失败即 B1 不通过，附复现步骤。

### 3.2 B2：吊销后拒绝再分配并断链

准备：临时库插入设备 D 与未吊销 device 令牌；D 完成信令认证、建立会话、接入数据面；确认转发正常。

| 编号 | 断言 | 通过条件 |
| --- | --- | --- |
| B2.1 | 吊销前 publisher→subscriber 转发 | 对端收到（正向对照） |
| B2.2 | 置 `tokens.revoked_at` 后发下一条 `relay_allocate` | 返回 `error` 且 `payload.code=TOKEN_REVOKED` |
| B2.3 | 该信令连接 | 2 秒内关闭 |
| B2.4 | 两端数据面连接 | 2 秒内关闭；其后 0 帧；healthz 帧数不增长 |
| B2.5 | 新建连接重新 register/auth | 返回 `error` 且 `payload.code=TOKEN_REVOKED`，连接关闭 |
| B2.6 | healthz | `revocation.revoked_devices>=1`、`dataplane.revoked_tokens>=1`、`revoked_sessions>=1`、`sessions=0` |

B2 通过 = B2.1–B2.6 全过；任一失败即 B2 不通过。

### 3.3 T：免授权不得绕过设备鉴权（发行前必测）

判定原则：**T1 正向对照成立且 T2–T6 全过才算通过；只测拒绝不算通过。**

准备（全部走账号中心 `/v1`，真实 HTTP + TLS）：
- 注册账号 A；设备 H（被控）登录并绑定 A；设备 C（控制）登录并绑定 A；H 将 C 设为受信。
- 注册账号 B；设备 D 登录并绑定 B。

| 编号 | 用例 | 通过条件 |
| --- | --- | --- |
| T1 | C 不带连接码对 H 调 `POST /v1/access/resolve` | `route=passwordless`、`trusted=true`、`audited=true`；审计恰好 1 条 `access.passwordless`（同 session_id 不重复） |
| T2 | D（非本账号）对 H 调 `resolve_access` | 不得为 `passwordless`；应为 `approval`/`denied` |
| T3 | D 用自己的 Bearer，但请求体伪造 `account_id=A` 等「同账号声明」 | 判定仍按 Bearer 身份，不得为 `passwordless` |
| T4 | 攻击者用自己密钥在信令中自报 `from_device_id=C` | 信封验签失败 `AUTH_FAILED`，不能冒充受信设备 |
| T5 | H 撤销 C 的受信后，C 再调 `resolve_access` | 回到 `approval`，不再 `passwordless` |
| T6 | 被测 `host_handshake` 喂入无签名 / `hs=1` / `session_id` 或 `peer_device_id` 不匹配的 hello | 全部 `HandshakeError`；正确 hs=2 hello 成功派生会话密钥 |
| T7 | 端到端正向：真受信设备 C 不带连接码对 H 建会话并完成设备签名 + hs=2 | 建会话成功且完成 hs=2（真实会话链路，不用模拟主机） |

T7 的执行与判定：
- 先做接线探测（只读）：`resolve_connection_access`/`authorize_connection` 是否被
  `commands/service.py` 或 `session/host.py` 调用；`host.py` 是否仍无条件走 `AccessCodeGuard`。
- 再跑一次真实链路：受信设备不带码发起连接，观察是否进入会话与数据面 hs=2。
- 若成功：T 通过。
- 若被会话入口拒绝：T **不通过**，记录实际错误码/文案、阻断文件与行号、复测入口；
  不把「代码里看起来没接线」当作唯一证据，必须有运行时输出。

## 4. 证据与脱敏

目录 `D:\RDC-Build\evidence\security\20261110-b1b2-trusted-reverify\`：

| 文件 | 内容 |
| --- | --- |
| `b1b2_trusted_verify.py` | 自研单文件客户端 + 实例编排，可直接复跑 |
| `run.json` | 实例来源、端口、每项断言、命令、时间戳、原始结果 |
| `transcript.json` | 收发消息与数据面帧摘要（脱敏后） |
| `REVERIFY-REPORT.md` | B1/B2/T 三项结论、未通过项复现路径、公网待窗口说明 |
| `sha256.txt` | 上述文件归一（UTF-8 无 BOM、LF）后的 SHA-256 |

脱敏：`access_code`、口令、`device_token`、`session_token`、`relay_token`、`sig`、私钥
一律替换为 `***`；扫描全部证据文件，命中即判失败并重跑。
凭据只用于本轮实例，不写入仓库、日志、终端回显。

## 5. 结论口径

- B1/B2 各自独立给「通过/不通过」，不因对方结果连带。
- T 只按 T1–T7 判定；T7 不成立即「不通过（正向对照未成立）」，写明阻断点与复测入口。
- 公网 8750 一律标「待部署窗口」，本轮不做任何动作。
- 最终结论写入 `REVERIFY-REPORT.md` 并同步父会话。

## 6. 非目标与约束

- 不修改 `src/`、`tests/`、`android/`、契约正文、部署配置。
- 不访问 ECS、不改安全组、不改 22/80/443/8750/8760、不重启任何线上服务。
- 不探测公网 8750，不读取线上私钥或凭据文件。
- 不修复缺陷；发现缺陷只给复现路径、期望行为与证据，交主线。

## 7. 回滚

仅新增 `.team/specs` 文档与证据目录，无产品代码与线上变更；回滚即删除两者。

## 8. 需批准事项

1. 同意本地起隔离实例（优先使用中继线提供的实例；无则自起，TLS 自签 CA）。
2. 同意新写自研独立客户端，不引用项目 relay/客户端脚本。
3. 同意 T7 以真实会话链路为准，正向不成立即判不通过。
