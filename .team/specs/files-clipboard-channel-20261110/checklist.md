# 文件传输与剪贴板接入会话通道 · 验收清单

## 通道边界

- [x] 文件与剪贴板只依赖 `SessionChannel` 协议（open/send/receive/close/info），没有把传输逻辑写死在中继分支
- [x] `RelayFrameChannel` 自报 `encrypted=false`、`visible_to_relay=true`；环路通道自报 `visible_to_relay=false`
- [x] 未绑定通道时 `channel_status()` 稳定返回 `unbound` 或 `local_staging`，`get_clipboard_status()` 不会因读取而创建会话或状态
- [x] 未改动 `scripts/rdc_host_linux.py`、`scripts/android_host_agent.py`、`src/rdc/commands/media_caps.py`、帧封装与 v2 协商文件的既有逻辑

## 文件传输（缺口 19）

- [x] 分块发送，单块不超过 256 KiB；未确认在途不超过 4 MiB；背压不阻塞通道
- [x] 收端按 `.part` 落盘并 fsync 后转正，路径穿越、绝对路径、盘符与重复项均拒绝
- [x] 逐块 sha256 校验，失败回 `CHUNK_CHECKSUM_MISMATCH` 并中止
- [x] 整树摘要校验，失败回 `file_result(verified=false)` 并清理临时文件
- [x] 断点续传由收端 `.part` 实际长度经 `file_accept.resume` 回传，发送端从该偏移起传
- [x] 发送端 `completed` 只认 `file_result` 且 `verified=true`、摘要一致；`phase=verifying` 时界面显示「等待对端校验与确认」
- [x] 无通道、确认超时、背压分别返回 `SESSION_CHANNEL_UNAVAILABLE`、`SESSION_CHANNEL_TIMEOUT`、`CHANNEL_BACKPRESSURE`，不会卡在进度条
- [x] 发送端限速扣减视频地板档（2500 kbps），预算不足时拒绝并说明原因

## 剪贴板（缺口 20）

- [x] 双向 `clipboard_update` / `clipboard_request` / `clipboard_ack` 走同一通道
- [x] 同摘要去重：收端 `applied=false`，发送端重复推送被抑制
- [x] 开关关闭后双向命令一律 `CLIPBOARD_DISABLED`，通道无帧
- [x] `status` 只返回摘要与长度，不含 `text` / `image_b64`；预览走既有脱敏规则
- [x] `pending` 在 ack 前为真，界面显示「未同步（等待对端确认）」

## 提示与真话

- [x] 传输页与剪贴板页在降级通道显示「对中继可见、未达端到端加密」，`local_staging` 显示「只在本机复制」，`unbound` 禁用开始
- [x] 日志记录 `visible_to_relay` / `encrypted`，不只写在文档里

## 契约与证据

- [x] `desktop-v1` 升 v1.8：第 50 条 `get_transfer_channel_status`、3.10 节、3 个新错误码
- [x] `protocol-v1` 升 v1.6：`file_result`（第 34 类）、`file_offer.digest` 必填、`file_accept.resume` 语义、3.1 顺序、3.4 节
- [x] `.team/records/契约变更记录-契约-202611-014-20261110.md` 写明缺口 19/20 关闭依据与「未达端到端加密」
- [x] 跨进程文件实测：2,097,152 字节 / 0.265 秒 / 7,913 KB/s / 摘要 `e00eecbf…302e13` / 收端真实写入且内容一致
- [x] 跨进程剪贴板实测：双向收齐、摘要互等、拉取一致、去重与关闭生效、状态无明文（12 项全过）
- [x] `python scripts/gate.py` 6/6 通过
- [x] host 接线最小改动清单已写入验收文档 §6（3 处插入、函数名、行数）
