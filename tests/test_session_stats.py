"""会话层统计口径: 帧率/码率/延迟全部来自实测, 取不到时返回 None 由界面显示「—」."""

from __future__ import annotations

import time
from types import SimpleNamespace

from rdc.session.remote import RemoteConfig, RemoteSession


def _session() -> RemoteSession:
    return RemoteSession(RemoteConfig(host="127.0.0.1", port=27888, device_id="PEER", access_code="123456"))


def test_fps_counts_decoded_frames_in_last_second() -> None:
    """帧率取最近 1 秒实际解码帧数, 窗口外的帧不计入."""
    session = _session()
    now = time.monotonic()
    session._frame_times.extend([now - 5.0] * 40)
    session._frame_times.extend([now - 0.2] * 30)
    assert session.stats()["fps"] == 30.0


def test_fps_is_none_without_frames() -> None:
    """没有解码帧时帧率返回 None, 不用配置值代填."""
    assert _session().stats()["fps"] is None


def test_bitrate_uses_wire_bytes_and_reports_payload_ratio() -> None:
    """码率按线缆字节增量计算, 并给出净荷占比."""
    session = _session()
    session.channel = SimpleNamespace(
        sent_wire_bytes=1000,
        recv_wire_bytes=0,
        sent_payload_bytes=900,
        recv_payload_bytes=0,
        sent_frames=1,
        recv_frames=0,
    )
    now = time.monotonic()
    session._byte_samples.append((now - 1.0, 0, 0))
    kbps, ratio = session._window_bitrate(now)
    assert kbps is not None and abs(kbps - 8.0) < 0.5
    assert ratio is not None and abs(ratio - 0.9) < 0.01


def test_bitrate_is_none_without_channel() -> None:
    """未建立通道时码率返回 None."""
    assert _session()._window_bitrate(time.monotonic()) == (None, None)


def test_latency_needs_enough_samples() -> None:
    """ping/pong 样本不足 3 次时中位与 95 分位都返回 None."""
    session = _session()
    session._rtt_samples.extend([18.0, 20.0])
    assert session._latency_percentiles() == (None, None)
    session._rtt_samples.append(22.0)
    p50, p95 = session._latency_percentiles()
    assert p50 == 20.0
    assert p95 is not None and p95 >= p50


def test_profile_comes_from_actual_frame_size() -> None:
    """档位由实际画面分辨率与协商帧率推出, 不读配置档位名."""
    session = _session()
    session.status.width = 1280
    session.status.height = 720
    session.stream_fps = 30
    assert session.stats()["profile"] == "720P 30 帧"
    session.status.width = 0
    assert session.stats()["profile"] == ""
