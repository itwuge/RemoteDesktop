"""被控端媒体端口: Linux 采集装配、libx264 编码与 xdotool 命令构造的单元测试."""

from __future__ import annotations

import sys

import pytest

from rdc.commands import host_ports
from rdc.contracts.ports import EncoderParams, Frame, InputEvent, PortError
from rdc.core.errors import CAPTURE_UNAVAILABLE, ENCODE_FAILED, INPUT_PERMISSION_DENIED
from rdc.input.linux_x11 import XdotoolInputInjector
from rdc.input.windows_sendinput import SendInputInjector
from rdc.media.linux_x11 import Libx264Encoder, X11CaptureSource
from rdc.media.windows_dxcam import DxcamCaptureSource


def _injector() -> XdotoolInputInjector:
    return XdotoolInputInjector(":0")


# --------------------------------------------------------------- 装配


def test_factory_selects_platform_capture_and_input():
    """按平台真实能力装配: Windows 走 Dxcam/SendInput, Linux 走 X11/xdotool, 其余上报错误码."""
    if sys.platform.startswith("win"):
        assert isinstance(host_ports.create_capture_source(), DxcamCaptureSource)
        assert isinstance(host_ports.create_input_injector(), SendInputInjector)
        return
    if sys.platform.startswith("linux"):
        assert isinstance(host_ports.create_capture_source(), X11CaptureSource)
        assert isinstance(host_ports.create_input_injector(":0"), XdotoolInputInjector)
        assert host_ports.probe_capture() == ""
        return
    with pytest.raises(PortError) as capture_error:
        host_ports.create_capture_source()
    assert capture_error.value.code == CAPTURE_UNAVAILABLE
    with pytest.raises(PortError) as input_error:
        host_ports.create_input_injector()
    assert input_error.value.code == INPUT_PERMISSION_DENIED
    assert host_ports.probe_capture().startswith(CAPTURE_UNAVAILABLE)


# --------------------------------------------------------------- 编码


def test_libx264_encoder_emits_keyframe_packet():
    encoder = Libx264Encoder()
    handle = encoder.open(EncoderParams(width=64, height=64, fps=15, bitrate_bps=500_000))
    try:
        packets = encoder.encode(handle, Frame(width=64, height=64, pixels=bytes(64 * 64 * 4)))
    finally:
        encoder.close(handle)
    assert len(packets) == 1
    assert packets[0].data
    assert packets[0].keyframe is True
    assert packets[0].pts == 0


def test_encoder_rejects_frame_with_wrong_pixel_size():
    encoder = Libx264Encoder()
    handle = encoder.open(EncoderParams(width=64, height=64, fps=15, bitrate_bps=500_000))
    try:
        with pytest.raises(PortError) as error:
            encoder.encode(handle, Frame(width=64, height=64, pixels=b"\x00"))
    finally:
        encoder.close(handle)
    assert error.value.code == ENCODE_FAILED


# --------------------------------------------------------------- 注入


def test_xdotool_char_and_key_commands_are_whitelisted():
    injector = _injector()
    assert injector.command_for(InputEvent(kind="key", action="char", text="中")) == [
        "type",
        "--clearmodifiers",
        "--delay",
        "0",
        "--",
        "中",
    ]
    assert injector.command_for(InputEvent(kind="key", action="char", text="")) is None
    assert injector.command_for(InputEvent(kind="key", action="char", text="abcde")) is None
    assert injector.command_for(InputEvent(kind="key", action="key", key="Return")) == [
        "key",
        "--clearmodifiers",
        "Return",
    ]
    assert injector.command_for(InputEvent(kind="key", action="key", key="F13")) is None


def test_xdotool_combo_requires_known_modifiers():
    injector = _injector()
    event = InputEvent(kind="key", action="combo", key="c", mods=("ctrl", "shift"))
    assert injector.command_for(event) == ["key", "--clearmodifiers", "ctrl+shift+c"]
    unknown = InputEvent(kind="key", action="combo", key="c", mods=("meta",))
    assert injector.command_for(unknown) is None
    empty = InputEvent(kind="key", action="combo", key="", mods=("ctrl",))
    assert injector.command_for(empty) is None


def test_xdotool_mouse_commands_cover_move_click_and_wheel():
    injector = _injector()
    assert injector.command_for(InputEvent(kind="mouse", action="move", x=12, y=34)) == [
        "mousemove",
        "12",
        "34",
    ]
    assert injector.command_for(InputEvent(kind="mouse", action="click", x=12, y=34, button=1)) == [
        "mousemove",
        "12",
        "34",
        "click",
        "1",
    ]
    assert injector.command_for(InputEvent(kind="mouse", action="wheel", x=12, y=34, button=5)) == [
        "mousemove",
        "12",
        "34",
        "click",
        "5",
    ]
    assert injector.command_for(InputEvent(kind="mouse", action="click", x=0, y=0, button=9)) is None
    assert injector.command_for(InputEvent(kind="mouse", action="wheel", x=0, y=0, button=1)) is None
    assert injector.command_for(InputEvent(kind="touch", action="move")) is None
