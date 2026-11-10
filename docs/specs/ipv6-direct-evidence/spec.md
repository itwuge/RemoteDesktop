# IPv6 直连双向取证脚本（取证，不改产品行为）

## 目标

新增 `scripts/e2e_ipv6_direct.py`，对「Windows 控制端 ↔ Ubuntu 虚机被控端」做 IPv6 直连的双向取证：

1. 正向：Windows 控制端 → Linux 被控端（虚机 `:0` 运行 `scripts/rdc_host_linux.py`），真实解码出画面，采集会话统计真实值（帧率、码率、延迟 p50/p95、档位），控制端注入可区分测试串并在虚机落盘回读。
2. 反向：Linux 控制端 → Windows 被控端，出画面 + Windows 侧输入落盘回读。
3. 断言：对端 TCP 地址必须为 `240e:…`，且不存在指向外部中继 `47.109.32.249` 的连接；命中即判定 `direct_ipv6`，否则失败，不允许以中继/IPv4 结果冒充。
4. 落盘：`evidence/ipv6-direct/<时间戳>/` 下保存截图（解码帧 PNG）、双方日志、输入落盘原文、会话统计原始值、候选/选中路径与断言 JSON。
5. 门禁：新文件通过 `scripts/gate.py` 全项（ruff、ruff format、禁止写法、结构、mypy、pytest），日志走 `RDC_LOG_DIR` 隔离；不新增 noqa/豁免/拆分字符串。

## 现状与关键事实（已只读确认）

- 虚机运行中，桥接；`ens33` 全局 IPv6 `240e:370:6303:cb0:15f2:306a:ed29:2e5d/64`（另有临时地址），默认路由 `via fe80::a4d9:acff:fe28:6a20`；SSH `192.168.1.215:22022` 可达。
- 虚机 `:0` 已登录，`XAUTHORITY=/run/user/1000/gdm/Xauthority`，`xdotool`/`gnome-terminal`/`xterm`/`python3` 可用，仓库在 `/home/wuge/rdc/app`，27888 空闲。
- 产品被控端监听层 `src/rdc/relay/server.py:603` 使用 `socket.AF_INET`，`HostConfig.bind=""` 实际只绑定 IPv4；控制端 `src/rdc/session/remote.py` 的信令与数据面均连 `config.host:port`。
  → 预期：以 IPv6 字面量连接 27888 会失败（连接被拒/超时），脚本必须把该点作为带数值的失败证据，而不是降级。
- Windows 防火墙三个配置文件均 `Enabled=False`，无 27888 入站规则；本机侧防火墙不拦 TCP。

## 脚本设计（唯一新增文件）

`scripts/e2e_ipv6_direct.py`（标准库 + 项目依赖；`paramiko` 仅用于虚机 SSH）：

- 入口：`main()`，参数 `--guest-host 192.168.1.215`、`--ssh-port 22022`、`--guest-ipv6 <addr>`、`--port 27888`、`--direction forward|reverse|both`、`--evidence <dir>`、`--marker <串>`。
- 凭据：只读 `RDC_VM_USER` / `RDC_VM_PASSWORD` 环境变量；不打印、不落盘；SSH 命令行不拼接口令。
- `Evidence` 类：创建 `<evidence>/<ts>/`，写 `run.json`、`forward/`、`reverse/`、`logs/`；所有断言结果进 JSON。
- 正向流程：
  1. SSH 起被控端（`DISPLAY=:0 XAUTHORITY=... nohup python3 scripts/rdc_host_linux.py`，日志重定向到 `/tmp/rdc-e2e-host-<ts>.log`），解析三行横幅（监听地址/设备 ID/连接码）。
  2. 虚机侧 `ss -lntH sport = :27888` 原始输出落盘，断言是否出现 `[::]`/`240e` 监听地址。
  3. Windows 侧 `socket.create_connection((guest_ipv6, 27888), 3)` 记录结果与耗时；失败即记 `blocked_at=host_listener_family` 并结束正向（带 errno/超时数值）。
  4. 成功才继续：`RemoteSession(RemoteConfig(host=guest_ipv6, port, device_id, code))`，`on_frame` 保存首帧与稳定帧 PNG；每 2s 抓 `stats()` 原始字典；`send_input` 注入 marker。
  5. 虚机侧 `xterm/gnome-terminal -e "bash -c 'cat > /tmp/rdc-e2e-input-<ts>.txt'"`（`DISPLAY=:0`）聚焦后注入，SSH 回读该文件，逐字节比对 marker。
  6. 断言：Windows `Get-NetTCPConnection -OwningProcess <this>` 的 `RemoteAddress` 全部属于 `240e` 集合；虚机 `ss -tn` 对端为 Windows `240e` 地址；全表无 `47.109.32.249`。
- 反向流程：
  1. Windows 侧用 `rdc.commands.api.start_hosting` 起真实被控端，读取 `get_hosting_info()` 的 device_id/access_code/port。
  2. 虚机侧通过 SSH 运行临时控制端片段（写入 `/tmp/rdc-e2e-ctrl-<ts>.py`，仅取证用途，不含口令），连接 Windows 的 `240e` 地址，保存解码帧与 `stats()` 到 `/tmp` 并回传。
  3. Windows 侧开一个等待 `Read-Host` 并写文件的控制台窗口，聚焦后由虚机控制端注入 marker，回读文件断言。
  4. TCP 断言同上（Windows 监听/对端、虚机对端、无外部中继）。
- 判定：`verdict ∈ {direct_ipv6, failed}`；失败 JSON 含 `blocked_at` 与实测数值（errno、超时秒数、监听地址列表、实际对端地址）。

## 反向只读核对（并行事项，不改路由器）

在同一脚本外单独执行，结果写 `evidence/ipv6-direct/<ts>/reachability/`：

1. 读取 Windows 防火墙状态与 27888 规则（已确认：全关、无规则）。
2. 读取本机 240e 地址与监听端口族（预期仅 IPv4 0.0.0.0:27888）。
3. 路由器 `fw3`：若可经 SSH 只读访问则取 `uci show firewall` 的 `wan/wan6 → lan` forward 与 `nft list ruleset` 相关链；无可用访问方式时如实记录 `router_access=unavailable`，不猜测规则。
4. 输出一句话结论：手机蜂窝 IPv6 能否连入本机，卡在路由器还是本机（当前证据指向被控端仅监听 IPv4），以及最小放行（路由器放行 TCP 27888 到该主机 + 应用监听 IPv6）。

## 验证

- 正向、反向各跑一次；失败也须留下 `run.json` 与原始日志。
- `python scripts/gate.py` 全绿（`RDC_LOG_DIR` 由 gate 自身隔离到 `logs/gate/<ts>`）。
- 不改 `src/`、不改产品行为；虚机保持桥接不回 NAT。

## 需确认

- 是否同意按上述方式新增该脚本并立即执行（预期在「被控端仅监听 IPv4」处失败并如实上报）。
- 路由器侧只读检查：是否提供/允许一个只读 SSH 入口；否则按 `router_access=unavailable` 记录。
