# 任务清单：B1/B2 本地复验 + 免码不得绕过设备鉴权

状态：已执行（2026-11-10 21:06，含 T 项正向对照），证据见 `D:\RDC-Build\evidence\security\20261110-b1b2-trusted-reverify`。

- [x] T1 建证据目录；新写自研独立客户端 `b1b2_trusted_verify.py`（协议自包含，未引用项目 relay/客户端脚本）。
- [x] T2 实例编排：自起隔离 TLS/明文实例（随机端口、临时库、自签 CA）；`instance_source=self`。
- [x] T3 B1 七项：全部通过（结束后双端断开、零帧、旧令牌 `TOKEN_INVALID`、不可再分配、healthz 一致）。
- [x] T4 B2 六项：全部通过（`TOKEN_REVOKED` 并断连、数据面关闭零帧、重新认证被拒、healthz 一致）。
- [x] T5 反向四类：T2 非本账号设备、T3 伪造同账号声明、T3b 冒用受信设备标识、T5 撤销信任后再连，全部回退 6 位码或断开。
- [x] T6 正向对照：受信设备 + 空连接码建会话成功（T7.0/T7.1/T7.3/T7.4/T7.5），hs=2 hello/ack 签名与会话绑定通过（T7.6），AEAD 首帧双向密钥一致且可解密（T7.7）。
- [x] T7 令牌吊销：受信设备令牌吊销后 `relay_allocate` 返回 `TOKEN_REVOKED`「账号令牌已吊销，请重新登录」，数据面关闭（T8/T8b）。
- [x] T8 落盘 `run.json`（含 `hs_evidence`）、`transcript.json`（脱敏）、`REVERIFY-REPORT.md`；敏感值扫描通过、LF 归一、`sha256.txt`。
- [ ] T9 公网 8750 复验：等用户通知部署窗口后执行。

## 结果

- 42/42 断言通过。
- B1 通过；B2 通过；T 通过（正向成立、hs=2 与 AEAD 首帧成立、免码审计 1 条、四类反向全部拒绝、令牌吊销拒绝且原因正确）。

## 验证命令

```powershell
python "D:\RDC-Build\evidence\security\20261110-b1b2-trusted-reverify\b1b2_trusted_verify.py" --mode all
```
