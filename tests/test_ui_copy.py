"""界面文案 mapping 的单测: 用户可见文案不得出现契约错误码."""

from __future__ import annotations

import re

from rdc.core import errors
from rdc.ui.copy import FALLBACK_TEXT, error_text, message_text

CODE_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")


def test_known_code_maps_to_chinese_without_message() -> None:
    assert error_text(errors.CREDENTIAL_INVALID) == "凭据无效, 请重新登录"
    assert error_text(errors.UPDATE_HASH_MISMATCH) == "更新包摘要不一致, 已拒绝安装"


def test_chinese_message_wins_over_mapping() -> None:
    assert error_text(errors.TOKEN_REVOKED, "登录状态已失效, 请重新登录") == "登录状态已失效, 请重新登录"


def test_bracketed_code_is_stripped_from_formatted_message() -> None:
    assert error_text(errors.CREDENTIAL_INVALID, "登录失败 [CREDENTIAL_INVALID]") == "登录失败"
    assert error_text("", "登录失败（错误码：TOKEN_REVOKED）") == "登录失败"
    assert error_text("", "登录失败【TOKEN_EXPIRED】") == "登录失败"
    assert error_text("", "登录失败（错误码：[TOKEN_EXPIRED]）") == "登录失败"


def test_status_bar_style_bracketed_code_never_survives() -> None:
    """预打包实测漏网场景: 状态栏出现 [CREDENTIAL_INVALID] 这类方括号码."""
    assert error_text("", "[CREDENTIAL_INVALID]") == FALLBACK_TEXT
    assert error_text(errors.CREDENTIAL_INVALID, "[CREDENTIAL_INVALID]") == "凭据无效, 请重新登录"
    assert "[CREDENTIAL_INVALID]" not in error_text("", "登录状态无效 [CREDENTIAL_INVALID]")


def test_failure_message_maps_success_message_passes_through() -> None:
    assert message_text(errors.CREDENTIAL_INVALID, "[CREDENTIAL_INVALID]", ok=False) == "凭据无效, 请重新登录"
    assert message_text(errors.CREDENTIAL_INVALID, "", ok=False) == "凭据无效, 请重新登录"
    assert message_text("", "传输任务已创建", ok=True) == "传输任务已创建"


def test_inline_code_is_stripped_from_prefixed_message() -> None:
    assert error_text("", "PERMISSION_DENIED: 没有权限执行该操作") == "没有权限执行该操作"
    assert error_text("", "采集不可用: CAPTURE_PERMISSION_DENIED: 没有屏幕采集权限") == "采集不可用: 没有屏幕采集权限"


def test_device_id_and_unknown_tokens_are_preserved() -> None:
    message = "设备不存在: R6TLPEHUJL7IUAJAFAQC"
    assert error_text("", message) == message
    assert error_text("", "服务提示: CPU 占用过高") == "服务提示: CPU 占用过高"


def test_no_uppercase_code_survives_any_display_text() -> None:
    samples = [
        error_text(errors.CREDENTIAL_INVALID),
        error_text(errors.TOKEN_REVOKED, "登录状态已失效 [TOKEN_REVOKED]"),
        error_text("", "连接失败（错误码：ACCESS_CODE_INVALID）"),
        error_text("", "UPDATE_HASH_MISMATCH: 更新包摘要不一致"),
        error_text("UNKNOWN_CODE", "未知失败"),
        error_text("UNKNOWN_CODE", ""),
    ]
    for text in samples:
        assert not CODE_PATTERN.search(text), text
        assert CODE_PATTERN.search(FALLBACK_TEXT) is None
