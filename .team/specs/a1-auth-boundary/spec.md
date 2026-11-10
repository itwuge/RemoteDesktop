# A1：未认证连接不得签发中继令牌

状态：待执行（用户已口头拍板「A1 最高优先、必须先修」）
范围：仅 A1。A5/A6 另立 spec，A1/A5/A6 全部回归通过前信令服务不得对外启用。

## 1. 漏洞复现（当前代码）

1. 攻击者 `DeviceIdentity.generate()`，与 ECS 建立 TLS 连接，不发送 `register`。
2. 发送 `relay_allocate`，payload 内自带 `public_key = 攻击者公钥`，用自己的私钥签名。
3. `src/rdc/relay/server.py::_public_key` 在 `signaling.devices` 查不到设备时回退
   `payload.get("public_key", "")`，验签通过。
4. `src/rdc/signaling/server.py::_handle_relay_allocate` 不校验会话存在性、不校验调用方
   是否为会话成员，直接签发 `relay_token` 并计入 `max_sessions`。
5. 结果：任意未认证连接可占满单路中继闸门。

## 2. 修复目标

- 验签公钥只能来自 `register` 已落库的 `signaling.devices[device_id].public_key_b64`；
  唯一例外是 `register` 消息本身的自举验签。
- 未认证连接（`device_id` 为空）只允许 `register`、`auth`、`heartbeat`。
- `relay_allocate` 必须满足：会话存在、未结束、调用方在 `state.devices` 内。
- 不改变已认证正常链路的协议语义与拒绝文案。

## 3. 精确改动

### 3.1 `src/rdc/signaling/server.py`

(1) 模块常量（放在 `DEFAULT_MIN_SESSION_KBPS` 之后）：

```python
# 未认证连接只允许自举与会话保活, 其余消息必须先完成 register + auth
UNAUTHENTICATED_TYPES: frozenset[str] = frozenset({"register", "auth", "heartbeat"})
```

(2) `handle()`：在 `msg_type in SERVER_TYPES` 判断之后、`getattr(handler)` 之前插入白名单：

```python
if not self._current_device and msg_type not in UNAUTHENTICATED_TYPES:
    self.errors.append(f"UNAUTHENTICATED: 未认证连接不得发送 {msg_type}")
    return [
        self._error(
            AUTH_FAILED,
            f"未认证连接不得发送 {msg_type}；请先完成 register 与 auth",
            envelope.get("session_id"),
        )
    ]
```

依据：`self._current_device` 由 `handle(envelope, device_id)` 首行赋值，空串即未认证连接。

(3) `_handle_relay_allocate()`：在解析 `session_id` / `requested` 之后、并发闸门之前插入：

```python
state = self.sessions.get(session_id)
if state is None or state.ended or self._current_device not in state.devices:
    self.errors.append(f"AUTH_FAILED: 设备未加入会话 {session_id}")
    return [
        self._error(
            AUTH_FAILED,
            "中继分配仅限该会话的参与设备，请先完成 offer 协商",
            envelope["session_id"],
        )
    ]
```

### 3.2 `src/rdc/relay/server.py`

(1) `_public_key()` 删除 `payload.get("public_key", "")` 回退：

```python
def _public_key(self, envelope: dict[str, Any], device_id: str | None = None) -> str:
    """取验签公钥: 只来自 register 已落库记录, 不采信任何载荷自报公钥."""
    payload = envelope["payload"]
    target = str(
        device_id
        or envelope.get("from_device_id")
        or payload.get("device_id")
        or ""
    )
    device = self.signaling.devices.get(target)
    return device.public_key_b64 if device is not None else ""
```

说明：`device_id`/`from_device_id`/`payload.device_id` 只用于「定位已落库记录」，
不再提供任何公钥值。

(2) `_process()`：`register` 单独自举验证，其余消息必须查到落库公钥：

```python
validate_envelope(envelope)
if envelope["type"] == "register":
    public_key = str(envelope["payload"].get("public_key", ""))
else:
    public_key = self._public_key(envelope, device_id)
if not public_key or not verify_envelope(envelope, public_key):
    raise ValueError("签名校验失败")
```

`register` 的载荷公钥只用于验证该条注册消息自身签名；`_handle_register` 仍会校验
`device_id_from_public_key(public_key) == device_id`，不会因此获得任何会话权限。

### 3.3 测试同步（改测试，不改实现迁就旧断言）

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `tests/test_protocol.py` | `test_register_auth_and_route` | `server.handle(offer)` → `server.handle(offer, alice.device_id)` |
| `tests/test_protocol.py` | `test_route_to_offline_peer_returns_error` | 同上 |
| `tests/test_protocol.py` | `test_relay_allocate_budget_and_profile` | 先 `_authenticate(alice/bob)`，发 `offer` 建会话，再带 `alice.device_id` 申请；预算断言保持 2760 / L0 |
| `tests/test_relay_integration.py` | `LARGE_SESSION_ID` 分配前 | 先由 alice 向 bob 发一条 `LARGE_SESSION_ID` 的 `offer` 并从 bob 套接字读走 |
| `scripts/relay_public_bench.py` | `_run_probe` | 分配前发送一条发给自身的 `offer`（同 `session_id`）并读走，建立会话成员身份 |
| `poc/tools/run_relay_dataplane_check.py` | `_request_relay_token` | 同上，认证后先自报 offer 建立会话 |
| `poc/tools/run_relay_tls_check.py` | 认证后分配前 | 同上 |

### 3.4 新增回归测试（`tests/test_relay_server.py`）

```python
def test_unauthenticated_connection_cannot_get_relay_allocated():
    """A1 回归: 未认证连接自报公钥不能通过验签拿到控制令牌."""
    server = RelayServer(_config(overhead_ratio=0.0))
    attacker = DeviceIdentity.generate()
    forged = build_envelope(
        attacker,
        "relay_allocate",
        {
            "session_id": SESSION,
            "requested_kbps": 2900,
            "est_duration_s": 60,
            "public_key": attacker.public_key_b64,
        },
        session_id=SESSION,
    )
    responses = server._process(json.dumps(forged).encode("utf-8"), None)
    assert all(item.get("type") != "relay_allocated" for item in responses)
    assert server.signaling.relay_grants == {}


def test_relay_allocate_requires_session_membership():
    """已认证但不是会话成员的设备不能申请中继预算."""
    server = RelayServer(_config(overhead_ratio=0.0))
    alice = DeviceIdentity.generate()
    outsider = DeviceIdentity.generate()
    _auth_connection(server, alice)      # register + auth 走 _process
    _auth_connection(server, outsider)
    request = build_envelope(
        outsider,
        "relay_allocate",
        {"session_id": SESSION, "requested_kbps": 2900, "est_duration_s": 60},
        session_id=SESSION,
    )
    responses = server._process(json.dumps(request).encode("utf-8"), outsider.device_id)
    assert responses[0]["type"] == "error"
    assert responses[0]["payload"]["code"] == AUTH_FAILED
```

`_auth_connection` 复用 `_process` 完成 register→challenge→auth，确保测试走真实网络边界。

## 4. 验收标准

1. 新增回归测试在修复前为红（返回 `relay_allocated`），修复后为绿（返回 error 且无授予）。
2. 以下测试集全绿：
   `tests/test_relay_server.py`、`tests/test_relay_signaling_v12.py`、`tests/test_protocol.py`、
   `tests/test_relay_integration.py`、`tests/test_relay_tls.py`。
3. `ruff check src tests` 与 `ruff format --check src tests` 通过。
4. 证据落盘：`evidence/a1-auth-boundary-20261110/{red.txt,green.txt,summary.md}`。
5. 不改 22/sshd、不重启实例、不碰 nginx 与 80/443、不重启线上服务；A1 仅代码与测试，
   部署动作留到 A1/A5/A6 全部通过后统一执行。

## 5. 非目标

- A5（重放 LRU、`_route` 身份绑定）、A6（墙归属）、A7（`RelayGrant.device_id`）、
  A11/A12（TLS 默认值）、A13（token 过期）、A16（真实 IP 脱敏）本 spec 不实现。

## 6. 回滚

纯代码/测试改动，无配置与线上变更。回滚 = 还原本次 commit 涉及文件；服务端未部署即无影响。
