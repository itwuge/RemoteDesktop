# 验收清单：IPv6 直连双向取证

## 脚本与判定

- [x] `scripts/e2e_ipv6_direct.py` 已新增，含参数解析、证据目录、SSH（env 凭据）、横幅解析、`ss` 采集、TCP 探测、会话驱动、帧落盘、marker 注入回读、双端对端断言
- [x] 失败路径不降级：连不通时写 `blocked_at` 与 errno/超时数值，不以中继结果冒充
- [x] `run.json` 含 candidates、selected_path、assertions、metrics 原始值、verdict

## 正向（Windows → 虚机）

- [x] 虚机以 `:0` 起被控端，监听 `*:27888`（双栈），横幅三行落盘
- [x] 可达地址 `240e:...:6f60:...` TCP 建连 19.35ms；不可达 mngtmpaddr 2002.47ms 超时，已如实记录
- [x] 会话出画面，解码帧 3 张落盘（frame_001/030/090.png，176171/410452/548962 字节）
- [x] 统计原始值：1280x800@30fps，fps 29–31，bitrate 2847–3477 kbps，latency p50 0.0ms / p95 16–47ms，档位 800P 30 帧
- [x] marker `RDC-IPV6-20261111-033036` 在虚机落盘文件回读成功
- [x] Windows 出站对端全部为 `240e:...:6f60:...`；虚机 `ss` 对端为本机 240e；无 47.109.32.249

## 反向（虚机 → Windows）

- [x] Windows 被控端监听 `::`:27888（AF_INET6，属本脚本进程）
- [x] 虚机探测两个 Windows 240e 地址均 OK（6.2ms / 6.0ms）
- [x] 会话出画面，首帧 PNG 回传（332574 字节）；1920x1080@30fps，fps 25–30，bitrate 1924–3558 kbps，latency p50 1.1ms / p95 1.2ms，档位 1080P 30 帧
- [x] marker `RDC-IPV6-20261111-033009` 在 Windows 控制台落盘文件回读成功
- [x] Windows 侧 6 条入站连接对端均为 `240e:...:6f60:...` 且归属本进程；虚机 `ss` 同源；无外部中继

## 证据与门禁

- [x] 证据目录：`evidence/ipv6-direct/20261111-033036/`（正向）、`20261111-033009/`（反向）、`20261111-033927/reachability/`（只读核对）
- [x] 双方日志：虚机被控端日志、Windows 产品日志（本轮 `RDC_LOG_DIR` 指向证据 `logs/`）
- [x] 快照与断言：`guest_established.txt`、`windows_samples.json`、`windows_listen.json`、`guest_ipv6_probes.json`、`guest_probes.json`
- [x] `python scripts/gate.py` 全绿（6/6；日志 `D:\RDC-Build\logs\gate\20261111-033644`），无 noqa/豁免/拆字符串
- [x] 未改产品代码行为；虚机保持桥接未回 NAT

## 并行只读核对（手机蜂窝 → 本机）

- [x] Windows 防火墙三配置文件 `Enabled=False`，27888 无入站规则 → 不拦
- [x] 本机两个 240e 全局地址 Preferred，`::/0` 经 fe80::a4d9…，出网 TCP 443 31.0ms
- [x] 27888 当前无监听（非被占用），回环与本机 240e 均 10061 拒绝
- [x] 路由器 `fw3` 无只读凭据：`router_access=unavailable`，未猜测规则
- [x] 一句话结论与最小放行写入 `reachability/conclusion.json`
