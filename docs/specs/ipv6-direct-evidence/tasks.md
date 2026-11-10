# 任务清单：IPv6 直连双向取证

## A. 脚本

- [x] A1 新增 `scripts/e2e_ipv6_direct.py`：参数解析、证据目录、SSH（env 凭据）、被控端横幅解析、`ss` 采集、TCP 探测、`RemoteSession` 驱动、帧落盘、marker 注入与回读、TCP 对端断言、`run.json`。
- [x] A2 正向失败路径写死：IPv6 连接失败 → `blocked_at` + errno/超时数值 + 监听地址列表，不降级中继。
- [x] A3 反向：Windows `start_hosting` + 虚机临时控制端片段 + Windows 控制台回读 marker + 同样的 TCP 断言。
- [x] A4 截图：首帧、稳定帧、输入前后各一张 PNG；双方日志复制到证据目录。

## B. 执行

- [x] B1 取虚机当前 IPv6（`ip -6 -o addr show scope global`）与 `ss -lnt` 原始输出。
- [x] B2 起 `rdc_host_linux.py`（`DISPLAY=:0`，`XAUTHORITY=/run/user/1000/gdm/Xauthority`），记录横幅。
- [x] B3 正向会话：出画面、统计原始值、marker 落盘回读、TCP 断言。
- [x] B4 反向会话：出画面、统计原始值、marker 落盘回读、TCP 断言。
- [x] B5 汇总 `run.json`：candidates、selected_path、assertions、metrics、verdict；失败带 `blocked_at`。

## C. 并行只读核对（手机蜂窝 → 本机）

- [x] C1 Windows 防火墙状态与 27888 入站规则 → 证据。
- [x] C2 本机 240e 地址与 27888 监听族 → 证据。
- [x] C3 路由器 `fw3` forward 规则（可只读访问时）；否则记 `router_access=unavailable`。
- [x] C4 一句话结论 + 最小放行项。

## D. 门禁

- [x] D1 `python scripts/gate.py`：ruff、format、禁止写法、结构、mypy、pytest 全绿。
- [x] D2 门禁日志路径写入证据目录说明；确认无 noqa/豁免/拆字符串。

## E. 收尾

- [x] E1 不改产品代码；虚机保持桥接。
- [x] E2 带数值回报每个失败点。
