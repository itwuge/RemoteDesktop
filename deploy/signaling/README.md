# 独立信令服务部署

## 一、定位

把信令从 `rdc-relay` 进程里拆出来，作为独立 systemd 单元运行：

| 项 | 值 |
|---|---|
| 单元 | `rdc-signaling.service` |
| 账号/组 | `rdc-signaling`（系统账号，nologin） |
| 代码目录 | `/opt/rdc-signaling/app`（0750，文件 0640） |
| 配置 | `/etc/rdc-signaling/signaling.conf`（0640 root:rdc-signaling） |
| 状态目录 | `/var/lib/rdc-signaling`（`StateDirectory`，存设备在线状态与 `accounts.db`，含屏幕墙归属 `walls` 表） |
| 日志 | journald，`SyslogIdentifier=rdc-signaling`；`/var/log/rdc-signaling` 预留 |
| 信令端口 | **8760/tcp** |
| 健康检查 | **127.0.0.1:8761**（只回环，禁止安全组放行） |
| 数据面 | 关闭（`dataplane_enabled = false`） |

端口选择依据：8750 是 relay 信令+数据面，8751 是 relay 健康检查，3478 是 STUN；8760/8761 均不冲突。

## 二、为什么复用 `src/rdc` 而不另写一套协议栈

`python -m rdc.signaling` 复用 relay 已验证的 TLS 1.3、长度前缀帧、连接治理、信令状态机与预算授予，
只通过 `dataplane_enabled = false` 关闭中继数据面。理由：

- 协议只有一套实现，避免信令在两份代码里漂移；
- TLS 加固、握手超时、限流、SIGHUP 热加载已经过外网验证；
- 独立单元、独立账号、独立端口、独立配置，运行边界仍然隔离。

## 三、TLS 依据

- 只允许 TLS 1.3（`build_server_context` 锁定 `minimum_version = TLSv1_3`），无明文回退。
- 沿用 relay 现有自签 CA 与 `server.crt`：`subject=CN=relay.wuge.fun`，
  `SAN=DNS:relay.wuge.fun, DNS:localhost, IP:127.0.0.1, IP:47.109.32.249`。
  因此客户端可直连 `relay.wuge.fun:8760`，也可经 SSH 隧道连 `127.0.0.1:8760`，
  两种方式主机名校验都能通过；不新签证书、不新增 DNS。
- 客户端用仓库 `deploy/relay/ca.crt` 做唯一信任锚（SHA-256
  `31c7369bbfe9c0faf9a17e05e2d19ea83cf9d5ae46e3cd018c6062aeb5ccf9d8`）。
- 客户端证书策略：**默认关闭（方案 B：服务端证书 + 应用层 Ed25519 鉴权）**，
  `require_client_cert = false`。理由：全仓库不使用 `getpeercert()`，证书 CN/SAN
  不参与设备/账号判定，双向证书在补上 CN 绑定前只是"准入闸门"，不提供设备级身份；
  身份由 register 落库公钥、challenge nonce 签名与包络验签保证。代价是公网任意主机
  可触达 register/challenge，需靠 64 全局 / 8 单 IP / 60 次每分限流兜底。
- **方案 A 开关路径（合规需要时）**：仅把 `require_client_cert` 改回 `true` 不够，
  必须同时落地四件事，缺一不可：
  1. 握手后取 `getpeercert()`，校验 `CN == device_id` 并与 register 的 device_id 比对；
  2. 客户端证书 30 天有效期，第 20 天自动续签（本地生成密钥与 CSR，私钥不出设备）；
  3. CRL：`openssl ca -gencrl` 生成，服务端 `load_verify_locations` +
     `verify_flags=VERIFY_CRL_CHECK_LEAF`（Python 3.12.5 / OpenSSL 3.0.13 已验证支持）；
  4. CRL 更新后重建 TLS 上下文并重载服务，否则旧吊销不生效。
- 客户端证书签发（仅切 A 时）：本地生成私钥与 CSR，私钥不出本机；CSR 上传服务器后用
  `openssl x509 -req -CA /etc/rdc-relay/tls/ca.crt -CAkey /etc/rdc-relay/tls/ca.key`
  签发，只回传 `client.crt`。CA 私钥不外传。
- 单元通过 `SupplementaryGroups=rdc` 只读 `/etc/rdc-relay/tls`，不复制私钥；
  证书轮换沿用 `SIGHUP` 热加载，不重启进程。
- 证书巡检由 relay 部署脚本安装的 `rdc-cert-check.timer` 统一负责（同时读取
  `/etc/rdc-relay/relay.conf` 与 `/etc/rdc-signaling/signaling.conf`）；备份与恢复演练见
  `deploy/backup.sh`。

## 四、部署

```bash
# 本地 dry-run
python3 deploy/signaling/deploy.py --dry-run \
  --config deploy/signaling/signaling.conf.example \
  --python /opt/rdc-py312/bin/python3.12

# 服务器首次部署（会创建账号/目录/单元并启动一次）
sudo python3 deploy/signaling/deploy.py \
  --config deploy/signaling/signaling.conf.example \
  --python /opt/rdc-py312/bin/python3.12

# 幂等复跑：内容一致时变更项 0、不重启
sudo python3 deploy/signaling/deploy.py \
  --config deploy/signaling/signaling.conf.example \
  --python /opt/rdc-py312/bin/python3.12
```

配置已存在时默认不覆盖；确需更新用 `--force-config`。只有应用代码/配置/单元内容变化才置位重启，
建目录等动作不触发重启。

## 五、上线验证

```bash
# 本机健康
curl -s http://127.0.0.1:8761/healthz | python3 -m json.tool
# 期望 service=rdc-signaling、budget_kbps=3000、dataplane=null

# 外网 TLS + 全链（register → challenge → auth → auth_ok → presence → offer → relay_allocate → session_end）
# 方案 B 下不需要客户端证书，只需要固定 CA 校验服务端
python3 scripts/relay_public_bench.py probe \
  --relay-host relay.wuge.fun --relay-port 8760 \
  --ca deploy/relay/ca.crt \
  --ca-sha256 31c7369bbfe9c0faf9a17e05e2d19ea83cf9d5ae46e3cd018c6062aeb5ccf9d8
```

外网前提：安全组入方向放行 **TCP 8760**；**不得放行 8761**。
安全组未放行时，用 SSH `direct-tcpip` 隧道到 `127.0.0.1:8760` 验证；
证书 SAN 含 `127.0.0.1`，主机名校验仍可通过。

### 5.1 异常来源排查（journald 结构化日志）

服务端单元设置 `RDC_LOG_REDACT_IP=0`，保留来源 IP（仅本地 journald，随 256 MiB 上限滚动，
不外传、不进客户端可见信息）；客户端展示与导出日志仍脱敏。`register` / `auth` 阶段每次
失败都会写一条 WARNING：

| event | 关键字段 | 含义 |
| --- | --- | --- |
| `register.rejected` | `peer_ip`、`device_id`、`error_code`、`reason` | 注册被拒（签名/公钥/字段不合法） |
| `challenge.issued` | `peer_ip`、`device_id` | 注册通过并下发 challenge，可用于观察谁在反复注册 |
| `auth.failed` | `peer_ip`、`error_code`、`reason` | challenge 签名或 device_id 校验失败 |
| `connection.rejected` | `peer_ip`、`reason` | 连接级限流/黑白名单/超限 |

按来源 IP 汇总（最近 24 小时）：

```bash
sudo journalctl -u rdc-signaling --since "24 hours ago" -o cat \
  | jq -r 'select(.event=="register.rejected" or .event=="auth.failed" or .event=="connection.rejected")
           | [.peer_ip, .event, (.error_code // "-")] | @tsv' \
  | sort | uniq -c | sort -rn | head -20
```

没有 `jq` 时用 Python：

```bash
sudo journalctl -u rdc-signaling --since "24 hours ago" -o cat \
  | python3 -c 'import sys,json,collections
c=collections.Counter()
for line in sys.stdin:
    try: o=json.loads(line)
    except Exception: continue
    if o.get("event") in ("register.rejected","auth.failed","connection.rejected"):
        c[(o.get("peer_ip"),o.get("event"),o.get("error_code") or "-")]+=1
for k,v in c.most_common(20): print(v,*k)'
```

## 六、回滚

部署前先按本节逐条演练一次（停服务 → 删单元/目录/账号 → 确认端口为空且 relay 不受影响），
演练通过后再部署。回滚命令逐条可执行：

```bash
systemctl disable --now rdc-signaling
rm -f /etc/systemd/system/rdc-signaling.service
systemctl daemon-reload
rm -rf /opt/rdc-signaling /etc/rdc-signaling /var/log/rdc-signaling
userdel rdc-signaling
groupdel rdc-signaling 2>/dev/null || true
# 撤销安全组 TCP 8760 入方向（本次未放行, 无需操作）
ss -lntp | grep -E ':(8760|8761)\b' || true   # 应为空
systemctl is-active rdc-relay                  # 应仍为 active，未受影响
```

保留项：`/etc/rdc-relay/tls` 与 `/opt/rdc-relay` 不动，relay 8750/8751 与 STUN 3478 不受影响。
