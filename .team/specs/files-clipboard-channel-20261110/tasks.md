# 文件传输与剪贴板接入会话通道 · 任务清单

## M0 先做真话提示（用户明确要求先于接通道）

- [x] T0a `src/rdc/files/service.py` 增 `channel`/`allow_local_staging` 字段与 `channel_status()`（无通道稳定返回 `unbound`/`local_staging`）；`src/rdc/commands/transfer.py` 增 `get_transfer_channel_status()`，模块默认服务改为 `allow_local_staging=False`。
- [x] T0b `src/rdc/ui/pages/transfer.py` 与 `clipboard_page.py` 先出提示条：未接入"未接入会话通道，当前无法跨端传输"；接入后若 `visible_to_relay` 常驻"当前走信令降级通道：载荷对中继可见，未达端到端加密"；`local_staging` 显示"只在本机复制"。
- [x] T0c 跑 `tests/test_transfer_clipboard_pages.py tests/test_ui_smoke.py`，确认提示条不破坏既有空态与操作。

## M1 通道与文件端到端（首个文件跑通即回报）

- [x] T1 新增 `src/rdc/session/__init__.py`、`src/rdc/session/channel.py`：
      `ChannelInfo`、`SessionChannelError`、`SessionChannel` 协议、`LoopbackChannel.pair`、`RelayFrameChannel`、`connect_relay_channel`。
- [x] T2 新增 `src/rdc/files/digest.py`（`file_digest`、`tree_digest`），`files/service.py::_tree_digest` 改为委托，跑 `tests/test_files_transfer.py` 确认本机路径无回归。
- [x] T3 新增 `src/rdc/files/receiver.py`：offer/accept（`resume` 偏移列表）、chunk 落 `.part` 与逐块校验、ack、complete 整体校验、`file_result`、abort、路径穿越防护、`snapshot`。
- [x] T4 `src/rdc/files/model.py` 的 `TransferItem` 增 `sha256`，`TransferJob` 增 `acked_bytes/remote/phase`；`snapshot` 增加 `acked_bytes`、`remote`、`phase`、`channel`。
- [x] T5 `src/rdc/files/service.py`：`channel`/`allow_local_staging`/`ack_timeout_s`/`inflight_limit_bytes`；`bind_channel`、`channel_status`；`start_transfer` 通道模式；`_advance_remote` 状态推进、4 MiB 窗口、限速、超时明确失败、`file_abort`；本机分支仅在 `allow_local_staging=True` 时可用。
- [x] T6 新增 `tests/test_session_channel.py`（环路 + 真实 TCP `RelayDataPlane` 握手与往返）。
- [x] T7 新增 `tests/test_file_channel_transfer.py`：2 MiB 端到端、块篡改、续传、背压、ack 超时、无通道、路径穿越、`file_result` 未到不完成且失败回执转 failed。
- [x] T8 新增 `scripts/verify_file_channel_e2e.py`：relay/sender/receiver 三个独立 OS 进程 + 本机真实 TCP；默认编排模式校验两端摘要与逐字节一致，产物写 `evidence/file-channel-<时间戳>/`。
- [x] T9 实测首个文件（跨进程）：记录大小、耗时、速率、`digest`、收端文件逐字节一致、对端真实写入路径、RelayDataPlane 转发计数；已向父会话回报（附数字）。
- [x] 里程碑：M1 完成 → 已回报"通道打通，能传第一个文件"。

## M2 剪贴板双向（能双向同步即回报）

- [x] T10 新增 `src/rdc/clipboard/receiver.py`：`clipboard_update` 校验/去重/ack、`clipboard_request` 应答、`set_local`。
- [x] T11 `src/rdc/clipboard/sync.py`：`channel`、`bind_channel`、通道 push/pull、`poll_channel`、pending/confirmed、`status` 新字段；无通道保持内存模式但 `mode="local"`。
- [x] T12 `src/rdc/commands/clipboard.py`：`get_clipboard_status` 内部先 `poll_channel()`，签名不变。
- [x] T13 新增 `tests/test_clipboard_channel.py`：双向 push/ack、去重、pull、开关、pending 未确认。
- [x] T14 实测双向同步（A 发 B 收、B 发 A 收，去重与关闭各一次，跨进程）；已回报。
- [x] 里程碑：M2 完成 → 已回报"剪贴板能双向同步"。

## M3 提示、契约与门禁

- [x] T15 `src/rdc/signaling/messages.py` 增 `file_result`（共 34 类）并更新 `SESSION_PLANE_TYPES`；`src/rdc/signaling/server.py` 增 `_handle_file_result`；`session_plane.py` 支持 `file_accept.resume` 与 `_check_file_result`；`tests/test_protocol.py` 33→34。
- [x] T16 `src/rdc/ui/pages/transfer.py`：`phase=verifying` 显示"等待对端校验与确认"；`clipboard_page.py` pending 显示"未同步（等待对端确认）"；按钮按通道状态禁用。
- [x] T17 `src/rdc/core/errors.py` 增 `SESSION_CHANNEL_UNAVAILABLE`、`SESSION_CHANNEL_TIMEOUT`、`CHANNEL_BACKPRESSURE`。
- [x] T18 契约：`docs/contracts/protocol-v1.md` 升 v1.6（`file_result`、`file_offer.digest` 必填、`file_accept.resume` 语义、3.1 顺序、3.4 节、版本段）；`docs/contracts/desktop-v1.md` 升 v1.8（第 50 条、3.10 节、错误码）；新增 `.team/records/契约变更记录-契约-202611-014-20261110.md`，写明缺口 19/20 关闭依据与"未达端到端加密"。
- [x] T19 更新 `tests/test_transfer_clipboard_pages.py` 增通道提示与待确认断言；`tests/test_ui_smoke.py` 保持空态。
- [x] T20 写 `docs/验收/文件传输与剪贴板会话通道实测-2026-11-10.md`（命令、输出、大小/耗时/速率/哈希/落盘证据）。
- [x] T21 跑 `python scripts/gate.py` 至 6/6。
- [x] T22 产出 host 接线清单核对（验收文档 §6：3 处插入、函数名、行数），随最终回报提交。
- [x] 里程碑：M3 完成 → 汇报契约条目、门禁结果、缺口关闭依据、host 接线清单。

## 验证命令

```powershell
python -m pytest tests/test_session_channel.py tests/test_file_channel_transfer.py tests/test_clipboard_channel.py tests/test_files_transfer.py tests/test_clipboard_sync.py tests/test_transfer_clipboard_pages.py tests/test_protocol.py tests/test_signaling_session_plane.py -q
python scripts/gate.py
```

## 回报纪律

- M1 或 M2 任一达成立即回报，不等全部完成。
- 遇阻塞（禁改文件冲突、中继行为不符、门禁被并行线长期拖红）立即回报，并给出冲突点与两条备选路径。
