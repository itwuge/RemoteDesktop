"""画质档位与自动降级单测: 四档表、3 秒降档、10 秒回升、切换上限与可观测记录."""

from __future__ import annotations

import pytest

from rdc.commands.service import RELAY_ONLY_PROFILES, CommandService
from rdc.media.quality import (
    DIRECT_TIERS,
    FLOOR_PROFILE,
    TIERS,
    QualityController,
    QualitySample,
)


def sample(
    timestamp_s: float,
    fps: float = 60.0,
    p95_ms: float = 30.0,
    loss_pct: float = 0.0,
    gpu_percent: float = 10.0,
    cpu_percent: float = 5.0,
    bandwidth_kbps: float = 60000.0,
) -> QualitySample:
    """构造一次采样."""
    return QualitySample(
        timestamp_s=timestamp_s,
        fps=fps,
        p95_ms=p95_ms,
        loss_pct=loss_pct,
        gpu_percent=gpu_percent,
        cpu_percent=cpu_percent,
        bandwidth_kbps=bandwidth_kbps,
    )


def auto_at(profile: str, **kwargs: object) -> QualityController:
    """构造从指定档位起跑的自动控制器."""
    controller = QualityController(session_id="s-1", profile=profile, **kwargs)
    controller.set_manual("auto")
    return controller


def test_tier_table_matches_contract() -> None:
    """四档齐全, 1080p30 是地板, 码率区间与带宽需求逐档递减."""
    assert DIRECT_TIERS == ("4K60", "1440p60", "1080p60", "1080p30")
    assert FLOOR_PROFILE == "1080p30"
    assert TIERS["4K60"].width == 3840 and TIERS["4K60"].fps == 60
    assert TIERS["1080p30"].fps == 30
    for name in DIRECT_TIERS:
        tier = TIERS[name]
        assert tier.bitrate_min_kbps < tier.bitrate_kbps < tier.bitrate_max_kbps
        assert tier.min_bandwidth_kbps > tier.bitrate_max_kbps
    assert (
        TIERS["4K60"].bitrate_kbps
        > TIERS["1440p60"].bitrate_kbps
        > TIERS["1080p60"].bitrate_kbps
        > TIERS["1080p30"].bitrate_kbps
    )


def test_downgrade_needs_full_three_seconds() -> None:
    """降档判据需连续成立 3 秒, 未满 3 秒不切换."""
    controller = auto_at("4K60")
    assert controller.observe(sample(0.0, fps=40)) is None
    assert controller.observe(sample(2.9, fps=40)) is None
    assert controller.profile == "4K60"

    event = controller.observe(sample(3.0, fps=40))
    assert event is not None
    assert event.from_profile == "4K60" and event.to_profile == "1440p60"
    assert event.metric == "fps" and event.threshold == 54.0
    assert event.window_ms == 3000
    assert controller.profile == "1440p60"


def test_p95_and_loss_and_resource_triggers() -> None:
    """p95、丢包、GPU、CPU、带宽任一越线都触发同一降档路径."""
    for kwargs, metric in (
        ({"p95_ms": 150.0}, "p95_ms"),
        ({"loss_pct": 3.0}, "loss_pct"),
        ({"gpu_percent": 90.0}, "gpu_percent"),
        ({"cpu_percent": 40.0}, "cpu_percent"),
        ({"bandwidth_kbps": 1000.0}, "bandwidth_kbps"),
    ):
        controller = auto_at("4K60")
        controller.observe(sample(0.0, **kwargs))
        event = controller.observe(sample(3.0, **kwargs))
        assert event is not None, metric
        assert event.metric == metric


def test_floor_never_downgrades() -> None:
    """1080p30 是地板, 任何越线都不再降."""
    controller = auto_at("1080p30")
    for second in range(0, 12):
        assert controller.observe(sample(float(second), fps=5, p95_ms=500, loss_pct=30)) is None
    assert controller.profile == "1080p30"
    assert controller.switches_used == 0


def test_recovery_after_ten_healthy_seconds() -> None:
    """连续 10 秒满足回升判据才升一档."""
    controller = auto_at("1080p30")
    healthy = {
        "fps": 30.0,
        "p95_ms": 40.0,
        "loss_pct": 0.1,
        "gpu_percent": 40.0,
        "cpu_percent": 15.0,
        "bandwidth_kbps": 20000.0,
    }
    for second in range(0, 10):
        assert controller.observe(sample(float(second), **healthy)) is None
    event = controller.observe(sample(10.0, **healthy))
    assert event is not None
    assert event.reason == "auto_recover"
    assert event.to_profile == "1080p60"
    assert event.window_ms == 10000


def test_recovery_requires_bandwidth_headroom() -> None:
    """带宽不足 30% 余量时不回升."""
    controller = auto_at("1080p30")
    healthy = {
        "fps": 30.0,
        "p95_ms": 40.0,
        "loss_pct": 0.1,
        "gpu_percent": 40.0,
        "cpu_percent": 15.0,
        "bandwidth_kbps": 12000.0,
    }
    for second in range(0, 15):
        assert controller.observe(sample(float(second), **healthy)) is None
    assert controller.profile == "1080p30"


def test_per_tier_entry_cap() -> None:
    """同一档位最多进入 2 次, 超限后锁定在当前位置."""
    controller = auto_at("4K60", max_per_tier=1, max_switches=10)
    healthy = {
        "fps": 60.0,
        "p95_ms": 30.0,
        "loss_pct": 0.0,
        "gpu_percent": 20.0,
        "cpu_percent": 10.0,
        "bandwidth_kbps": 70000.0,
    }
    assert controller.observe(sample(0.0, fps=40)) is None
    assert controller.observe(sample(3.0, fps=40)) is not None  # -> 1440p60, 第 1 次
    assert controller.entries["1440p60"] == 1
    for second in range(10, 21):
        controller.observe(sample(float(second), **healthy))
    assert controller.profile == "4K60"  # 回升成功
    assert controller.entries["4K60"] == 1
    # 再次越线试图第二次进入 1440p60, 被同档上限拦住
    controller.observe(sample(30.0, fps=40))
    assert controller.observe(sample(33.0, fps=40)) is None
    assert controller.locked is True
    assert controller.profile == "4K60"


def test_total_switch_cap() -> None:
    """每会话自动切换总次数达上限后锁定."""
    controller = auto_at("4K60", max_switches=1)
    assert controller.observe(sample(0.0, fps=40)) is None
    assert controller.observe(sample(3.0, fps=40)) is not None
    assert controller.switches_used == 1
    for second in range(4, 20):
        event = controller.observe(sample(float(second), fps=40))
        assert event is None
    assert controller.locked is True


def test_events_are_recorded_and_capped() -> None:
    """切换事件带全字段, 超出上限只保留最近记录."""
    controller = QualityController(session_id="s-1", profile="1080p30", event_limit=2)
    controller.set_manual("1080p60", timestamp_s=1.0)
    controller.set_manual("1440p60", timestamp_s=2.0)
    controller.set_manual("4K60", timestamp_s=3.0)
    assert len(controller.events) == 2
    payload = controller.events[-1].to_dict()
    assert set(payload) == {"ts", "from", "to", "reason", "metric", "value", "threshold", "window_ms"}
    assert payload["to"] == "4K60"
    assert controller.snapshot()["switches_used"] == 0  # 人工切换不计入自动切换


def test_manual_profile_stops_auto_control() -> None:
    """人工指定后不再自动升降."""
    controller = QualityController(session_id="s-1", profile="4K60")
    for second in range(0, 10):
        assert controller.observe(sample(float(second), fps=10)) is None
    assert controller.profile == "4K60"


def test_service_exposes_ladder_metrics_and_switch_events() -> None:
    """命令层指标含新字段, 自动降档会写进 quality_events."""
    service = CommandService()
    session = service.add_session("A" * 20, transport_kind="direct", quality_profile="auto")
    assert session.quality_profile == "1080p60"

    for second in (0.0, 1.0, 2.0):
        assert service.feed_quality_sample(session.session_id, sample(second, fps=20)) is None
    event = service.feed_quality_sample(session.session_id, sample(3.0, fps=20))
    assert event is not None
    assert session.quality_profile == "1080p30"

    metrics = service.get_session_metrics(session.session_id)
    assert metrics.ok is True
    assert metrics.data["quality_profile"] == "1080p30"
    assert metrics.data["switches_used"] == 1
    assert metrics.data["switches_limit"] == 6
    assert metrics.data["quality_events"][0]["reason"] == "auto_degrade"
    assert metrics.data["gpu_percent"] == 10.0


def test_relay_session_only_accepts_relay_profiles() -> None:
    """中继会话只接受中继降档档位, 直连档位被拒."""
    service = CommandService()
    session = service.add_session("A" * 20, transport_kind="relay", quality_profile="auto")
    accepted = service.apply_quality_profile(session.session_id, "720p30")
    assert accepted.ok is True
    assert accepted.data["quality_profile"] == "720p30"
    rejected = service.apply_quality_profile(session.session_id, "4K60")
    assert rejected.ok is False
    assert rejected.error_code == "PROFILE_UNSUPPORTED"
    assert "4K60" not in RELAY_ONLY_PROFILES


@pytest.mark.parametrize("profile", ["4K60", "1440p60", "1080p60", "1080p30"])
def test_direct_profiles_accepted(profile: str) -> None:
    """直连四档都能通过命令层下发."""
    service = CommandService()
    session = service.add_session("A" * 20, transport_kind="direct", quality_profile="auto")
    result = service.apply_quality_profile(session.session_id, profile)
    assert result.ok is True
    assert result.data["quality_profile"] == profile
