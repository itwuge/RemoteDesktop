# A1 任务清单

- [x] T1 写回归测试 `tests/test_relay_server.py::test_unauthenticated_connection_cannot_get_relay_allocated`
      与 `::test_relay_allocate_requires_session_membership`，先跑一次留 RED 证据。
- [x] T2 改 `src/rdc/relay/server.py`：`_public_key` 删除载荷公钥回退；`_process` 对 `register` 自举特判。
- [x] T3 改 `src/rdc/signaling/server.py`：新增 `UNAUTHENTICATED_TYPES` 与 `handle()` 白名单；
      `_handle_relay_allocate` 增加会话存在性与成员校验。
- [x] T4 同步测试：`tests/test_protocol.py`（3 处）、`tests/test_relay_integration.py`（LARGE 会话先建会话）。
- [x] T5 同步脚本：`scripts/relay_public_bench.py::_run_probe`、
      `poc/tools/run_relay_dataplane_check.py`、`poc/tools/run_relay_tls_check.py` 分配前先建立会话。
- [x] T6 跑目标测试集与 ruff，收入 GREEN 证据到 `evidence/a1-auth-boundary-20261110/`。
- [x] T7 上报 A1 结果（改动文件、RED→GREEN、测试计数），随后进入 A5。

## 验证命令

```powershell
python -m pytest tests/test_relay_server.py tests/test_relay_signaling_v12.py tests/test_protocol.py tests/test_relay_integration.py tests/test_relay_tls.py -q
ruff check src tests
ruff format --check src tests
```
