# Tasks · 视频帧 v2 默认路径 + 控制开销 + 中继 1080p30

状态：待批准后执行。每项完成后立即更新状态。

| # | 任务 | 文件 | 完成判据 |
|---|---|---|---|
| 1 | 命令层媒体能力单一出处 | 新增 `src/rdc/commands/media_caps.py`；导出 `DEFAULT_FRAME_VERSIONS` / `default_media_caps` / `negotiate_media` | 默认声明 `[2,1]`；降级写 WARNING 结构化日志并返回原因 |
| 2 | host 默认 v2 | `scripts/rdc_host_linux.py`：`_on_offer` 协商、`_answer` 带上 `frame_versions`、`HostSession`/`VideoSender` 走 `encode_v2`+`send_bytes` | 与 v2 控制端连通时全部帧为 `RF` 二进制；与只支持 v1 的对端连通时降级且 `_emit` 明确原因 |
| 3 | controller 默认 v2 | `scripts/rdc_control_app.py`：offer 用 `default_media_caps()`、answer 后协商、`_receive_loop` 用 `recv_raw` + `decode_frame` | 能解 v2 与 v1；降级有状态提示与日志 |
| 4 | AEAD 通道发送串行化 | `scripts/e2e_common.py`：`AeadChannel` 加发送锁 | 并发发送不会出现长度前缀错位 |
| 5 | 单测 | 新增 `tests/test_media_caps.py` | 4 类协商分支 + 默认声明，全部通过 |
| 6 | 控制面降频/缩短 | `scripts/relay_public_bench.py`：ping/pong 去冗余字段；`RTT_ACTIVE_INTERVAL_S=0.5` / `RTT_IDLE_INTERVAL_S=1.0` 自适应 | 720p10 两场景 wire/media ≤ 1.05 |
| 7 | 低码率复测 | `evidence/frame-v2-720p10-ctrl/` | 给出改后 wire bytes、控制开销字节数与占比 |
| 8 | Android 对接说明 | 新增 `docs/验收/安卓-v2帧格式对接说明-2026-11-10.md` | 头字段表 + 版本号 + hs=2 衔接 + 分流规则 + 错误码，未改 `android/` |
| 9 | 中继 3000 kbps 1080p30 三场景 | `evidence/relay-3mbps-1080p30-v2/` | 静态/文本/视频各给出线上码率、SSIM、丢帧 |
| 10 | 报告与契约登记 | 更新 `docs/验收/视频帧封装v2实测报告-2026-11-10.md`；新增 `.team/records/` 记录 | 三项数据齐全，`docs/contracts` 正文零改动 |
| 11 | 门禁 | `python scripts/gate.py` | 6/6，无 `# noqa` |

依赖：1 → 2/3/5；6 → 7；其余可并行（8/9 独立）。

回滚：各文件改动互相独立；`media_caps.py` 与单测可单独回退，host/controller 回退到 v1 分支即可恢复旧行为。
