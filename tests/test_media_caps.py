# 文件: test_media_caps.py | 用途: 命令层媒体能力协商单测 | 边界: 不启动网络与界面
"""命令层媒体能力协商: 默认 v2 优先, 降级必须有原因与日志."""

from __future__ import annotations

from rdc.commands.media_caps import DEFAULT_FRAME_VERSIONS, default_media_caps, negotiate_media
from rdc.core.logging import recent_events, reset_for_tests
from rdc.media.frame import VERSION_V1, VERSION_V2


def _degraded_events() -> list[dict[str, object]]:
    return [event for event in recent_events(200) if event.get("event") == "frame_version.degraded"]


def test_default_media_caps_prefers_v2() -> None:
    caps = default_media_caps()
    assert caps["frame_versions"] == [VERSION_V2, VERSION_V1]
    assert DEFAULT_FRAME_VERSIONS == (VERSION_V2, VERSION_V1)
    assert caps["codecs"] == ["h264"]


def test_peer_declares_v2_negotiates_v2_without_warning() -> None:
    reset_for_tests()
    version, reason = negotiate_media({"frame_versions": [2, 1]}, role="host")
    assert version == VERSION_V2
    assert reason == ""
    assert _degraded_events() == []


def test_peer_declares_only_v1_degrades_with_reason() -> None:
    reset_for_tests()
    version, reason = negotiate_media({"frame_versions": [1]}, role="controller")
    assert version == VERSION_V1
    assert "只声明了 v1" in reason
    events = _degraded_events()
    assert events and events[-1]["level"] == "WARNING"
    assert events[-1]["role"] == "controller"


def test_missing_peer_caps_degrades_with_reason() -> None:
    reset_for_tests()
    version, reason = negotiate_media(None, role="host")
    assert version == VERSION_V1
    assert "未声明" in reason
    assert _degraded_events()


def test_unknown_versions_are_ignored_and_degrade() -> None:
    reset_for_tests()
    version, reason = negotiate_media({"frame_versions": [7, "2"]}, role="host")
    assert version == VERSION_V1
    assert "不匹配" in reason


def test_single_frame_version_field_is_accepted() -> None:
    reset_for_tests()
    version, _ = negotiate_media({"frame_version": 2}, role="host")
    assert version == VERSION_V2
