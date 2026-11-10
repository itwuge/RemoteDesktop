# 被控端 answer 候选下发与主动打洞（S11）任务拆分

- 关联规格: `docs/specs/host-answer-candidates/spec.md`
- 执行顺序: T1 → T2 → T3 → T4 → T5 → T6，串行；T5 可与 T4 并行写测试，但门禁必须最后统一跑

## T1 候选过滤纯函数（policy.py）

- [ ] 新增 `sort_candidates(items) -> tuple[Candidate, ...]`：按 `-priority, family, address, port` 排序。
- [ ] 新增 `is_link_local(item) -> bool`：`scope=link`、IPv6 `fe80::/10`、IPv4 `169.254.0.0/16` 均为真。
- [ ] 新增 `filter_candidates(items, egress=()) -> tuple[tuple[Candidate, ...], int]`：
  - [ ] 先 `normalize_candidate`，再按 `is_publishable` 与 `is_link_local` 剔除。
  - [ ] 取 `egress` 中 `kind=srflx` 的地址集合；`items` 中 `host/srflx/relay` 命中即剔除并计数。
  - [ ] 返回排序去重后的候选与 hairpin 计数。
- 验收: `tests/test_connect_policy.py` 覆盖 link-local、hairpin、局域网 host 保留、排序去重。

## T2 双栈 STUN 发送（stun.py）

- [ ] 新增 `_server_endpoint(sock, server)`：`AF_INET` 原样；`AF_INET6` 下 IPv4 字面量/主机名转 v4-mapped，IPv6 原样。
- [ ] `query_server` 用 `_server_endpoint` 发送。
- 验收: 回环假 STUN 服务在 `AF_INET6` 双栈 socket 上能收到请求并回包，客户端解析出映射地址与端口。

## T3 主动探测（punch.py）

- [ ] 新增 `RESPONDER_PROBE_INTERVAL_S = 0.1`、`RESPONDER_PROBE_BUDGET_S = 8.0`。
- [ ] `PunchResponder.__init__` 增加 `_io_lock`、`_targets`、`_pending`、`_next_probe`、`_probe_deadline`、`sent`、`forward`。
- [ ] 新增 `set_targets(targets, budget_s=RESPONDER_PROBE_BUDGET_S)`：去重、重置待回复表与截止时间。
- [ ] 新增 `exclusive()`：持锁让出 socket，供 STUN 使用。
- [ ] `poll` 持锁执行：先 `_send_probes`，再 `select` + `_drain`。
- [ ] `_send_probes`：按 100 ms 节奏向每个目标发 `Q`，token 记入 `_pending`，`sent += 1`；超过预算停止。
- [ ] `_drain`：`R` 命中 `_pending` 时 `forward += 1`、刷新 `peer/last_seen`，不计 `ignored`；未知 `R` 计 `ignored`；`Q` 照旧回 `R`。
- [ ] `release` / `close` 持锁，保证与探测、STUN 互斥。
- 验收: 两个回环 UDP socket 的探测用例通过；原 `test_punch_responder_replies_and_hands_socket_over` 不掉。

## T4 被控端 answer 候选与探测接线（host.py）

- [ ] 新增常量 `ANSWER_CANDIDATE_BUDGET_MS = 1500`；`PUNCH_POLL_S` 改为 0.1。
- [ ] 新增 `_answer_candidates()`：v6 host（`config.port`）+ v4 host（有打洞 socket 时 `config.port`）+ srflx（复用打洞 socket，`discover_srflx`，预算 1500 ms）+ relay（`local_ip()` / `config.port` / 优先级 10）。
- [ ] 新增 `_peer_candidates(payload, local)`：解析 offer `candidates`，`filter_candidates(控制端, 本端)`，只留 `family=4` 且 `host/srflx`；记录 `punch.targets` 日志。
- [ ] `_on_offer`：准入通过后构造本端候选 → 过滤 peer → `filter_candidates(本端, peer)` → `_answer(..., candidates=...)` → `responder.set_targets(...)`。
- [ ] `_answer` 增加 `candidates` 参数，仅 `accepted` 且非空时写 `payload["candidates"] = [candidate_to_payload(...)]`；`chosen_candidate` 不变。
- [ ] `punch_stats` 增加 `sent`、`forward`、`targets`。
- 验收: answer 载荷与候选组成用例通过；旧字段 `chosen_candidate` 仍为 relay。

## T5 回归测试

- [ ] `tests/test_connect_policy.py`：`filter_candidates` 与 `sort_candidates` 用例。
- [ ] `tests/test_connect_runtime.py`：
  - [ ] `PunchResponder.set_targets` 后主动发 `Q`、收 `R` 记 `forward`、无目标不发包。
  - [ ] 双栈 socket 对回环 IPv4 假 STUN 服务的查询用例。
- [ ] `tests/test_ipv6_listen.py`：
  - [ ] `_answer_candidates()` 组成与端口（monkeypatch `native_host_candidates` / `discover_srflx`）。
  - [ ] `_answer()` 载荷含全量字段且保留 `chosen_candidate`。
  - [ ] `_peer_candidates()` 剔除 link-local 与 hairpin。
- [ ] 全部用例不依赖公网、不依赖本机 IPv6 状态；日志写临时目录。

## T6 门禁与交付

- [ ] `RDC_LOG_DIR` 按时间戳隔离，避免与并行线共用 `rdc.log`。
- [ ] `python scripts/gate.py` 六项全绿：ruff lint、ruff format、禁止写法、结构、mypy、pytest。
- [ ] 记录证据目录与通过数。
- [ ] 向父会话报告：改动文件、候选组成、探测参数、门禁证据；阻塞或偏差立即上报。

## 完成判据

- 被控端 answer 同时带 `candidates` 与 `chosen_candidate`，四类候选齐全且端口正确。
- STUN 失败时 answer 只有 host + relay，无伪造值。
- 控制端能从 answer 拿到 v4 host / srflx，并在打洞阶段看到被控端主动包（`backward > 0`）。
- 门禁六项通过，无 noqa / 豁免 / 拆字符串。
