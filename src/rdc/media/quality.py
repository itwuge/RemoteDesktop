# ==========================================================================================
# 文件        : quality.py
# 所属目录    : src/rdc/media/
# 文件用途    : 直连画质档位表与自动降级/回升控制器, 对应设计书 3.10 与契约 v1.1
# 架构定位    : 画质策略层, 纯计算, 不依赖界面、网络与采集实现
# 核心功能    : 四档位定义; 3 秒降档判据; 10 秒回升判据; 每会话切换上限; 切换事件记录
# 职责边界    : 只输出档位决策与事件; 不调整编码器, 不发送消息, 不读写文件
# 运行环境    : Python 3.12
# 依赖关系    : 被 src/rdc/commands/service.py 调用; 未来由会话编排层与真实采样接入
# ==========================================================================================

"""直连画质档位与自动降级控制器.

契约依据: docs/contracts/desktop-v1.md 第七条与第八条 (v1.1), 设计书 3.10 档位表.
1080p30 是地板, 任何自动逻辑都不得降到该档以下; 中继档另由 RelayPolicy 约束.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 直连档位, 顺序由高到低; 最后一项是地板
DIRECT_TIERS: tuple[str, ...] = ("4K60", "1440p60", "1080p60", "1080p30")
DIRECT_PROFILES: frozenset[str] = frozenset(DIRECT_TIERS) | {"auto"}
# 仅用于中继降档的档位, 直连会话下发即拒绝
RELAY_ONLY_PROFILES: frozenset[str] = frozenset({"900p30", "720p30", "540p30", "720p15"})
FLOOR_PROFILE = "1080p30"

DOWNGRADE_WINDOW_S = 3.0
RECOVERY_WINDOW_S = 10.0
DEFAULT_MAX_SWITCHES = 6
DEFAULT_MAX_PER_TIER = 2
EVENT_LIMIT = 32
# 升档要求带宽留出 30% 余量, 避免刚升档又触发降档
UPGRADE_BANDWIDTH_HEADROOM = 1.3


@dataclass(frozen=True)
class Tier:
    """一个画质档位的参数与阈值.

    :param name: 档位名, 与契约枚举一致
    :param width: 像素宽
    :param height: 像素高
    :param fps: 目标帧率
    :param bitrate_kbps: 默认视频码率
    :param bitrate_min_kbps: 码率区间下限, 地板档不足时只降码率不降分辨率
    :param bitrate_max_kbps: 码率区间上限
    :param min_bandwidth_kbps: 链路所需带宽, 含封装与重传余量
    :param downgrade_fps: 低于该帧率触发降档
    :param downgrade_p95_ms: 高于该 p95 延迟触发降档
    :param downgrade_loss_pct: 达到该丢包率触发降档
    :param downgrade_gpu_pct: 高于该 GPU 占用触发降档
    :param downgrade_cpu_pct: 高于该 CPU 占用触发降档
    :param recovery_p95_ms: 回升要求 p95 不高于该值
    :param recovery_loss_pct: 回升要求丢包率低于该值
    :param recovery_gpu_pct: 回升要求 GPU 不高于该值
    :param recovery_cpu_pct: 回升要求 CPU 不高于该值
    """

    name: str
    width: int
    height: int
    fps: int
    bitrate_kbps: int
    bitrate_min_kbps: int
    bitrate_max_kbps: int
    min_bandwidth_kbps: int
    downgrade_fps: float
    downgrade_p95_ms: float
    downgrade_loss_pct: float
    downgrade_gpu_pct: float
    downgrade_cpu_pct: float
    recovery_p95_ms: float
    recovery_loss_pct: float
    recovery_gpu_pct: float
    recovery_cpu_pct: float


TIERS: dict[str, Tier] = {
    "4K60": Tier(
        name="4K60",
        width=3840,
        height=2160,
        fps=60,
        bitrate_kbps=25000,
        bitrate_min_kbps=16000,
        bitrate_max_kbps=40000,
        min_bandwidth_kbps=50000,
        downgrade_fps=54.0,
        downgrade_p95_ms=110.0,
        downgrade_loss_pct=2.0,
        downgrade_gpu_pct=70.0,
        downgrade_cpu_pct=25.0,
        recovery_p95_ms=80.0,
        recovery_loss_pct=0.3,
        recovery_gpu_pct=55.0,
        recovery_cpu_pct=15.0,
    ),
    "1440p60": Tier(
        name="1440p60",
        width=2560,
        height=1440,
        fps=60,
        bitrate_kbps=14000,
        bitrate_min_kbps=10000,
        bitrate_max_kbps=22000,
        min_bandwidth_kbps=28000,
        downgrade_fps=54.0,
        downgrade_p95_ms=100.0,
        downgrade_loss_pct=3.0,
        downgrade_gpu_pct=70.0,
        downgrade_cpu_pct=25.0,
        recovery_p95_ms=75.0,
        recovery_loss_pct=0.5,
        recovery_gpu_pct=55.0,
        recovery_cpu_pct=20.0,
    ),
    "1080p60": Tier(
        name="1080p60",
        width=1920,
        height=1080,
        fps=60,
        bitrate_kbps=8000,
        bitrate_min_kbps=5000,
        bitrate_max_kbps=12000,
        min_bandwidth_kbps=15000,
        downgrade_fps=54.0,
        downgrade_p95_ms=90.0,
        downgrade_loss_pct=5.0,
        downgrade_gpu_pct=70.0,
        downgrade_cpu_pct=30.0,
        recovery_p95_ms=70.0,
        recovery_loss_pct=1.0,
        recovery_gpu_pct=50.0,
        recovery_cpu_pct=20.0,
    ),
    "1080p30": Tier(
        name="1080p30",
        width=1920,
        height=1080,
        fps=30,
        bitrate_kbps=4000,
        bitrate_min_kbps=2500,
        bitrate_max_kbps=6000,
        min_bandwidth_kbps=8000,
        downgrade_fps=27.0,
        downgrade_p95_ms=120.0,
        downgrade_loss_pct=8.0,
        downgrade_gpu_pct=85.0,
        downgrade_cpu_pct=45.0,
        recovery_p95_ms=60.0,
        recovery_loss_pct=0.5,
        recovery_gpu_pct=50.0,
        recovery_cpu_pct=20.0,
    ),
}


@dataclass(frozen=True)
class QualitySample:
    """一次采样, 由会话编排层每 200ms 聚合后传入."""

    timestamp_s: float
    fps: float
    p95_ms: float
    loss_pct: float
    gpu_percent: float
    cpu_percent: float
    bandwidth_kbps: float


@dataclass(frozen=True)
class QualityEvent:
    """一次档位切换记录, 用于可观测与追溯."""

    ts: float
    from_profile: str
    to_profile: str
    reason: str
    metric: str
    value: float
    threshold: float
    window_ms: int

    def to_dict(self) -> dict[str, Any]:
        """转成契约要求的字段名."""
        return {
            "ts": self.ts,
            "from": self.from_profile,
            "to": self.to_profile,
            "reason": self.reason,
            "metric": self.metric,
            "value": self.value,
            "threshold": self.threshold,
            "window_ms": self.window_ms,
        }


class QualityController:
    """按采样自动降档与回升.

    规则:
        1. 任一降档判据连续成立 3 秒, 立即降一档; 只降一档, 不跳档.
        2. 1080p30 为地板, 该档不再降分辨率; 带宽不足时只把码率压到区间下限.
        3. 回升需连续 10 秒满足目标档全部回升判据, 且带宽留 30% 余量; 每次只升一档.
        4. 每会话自动切换上限 6 次, 同一档位最多进入 2 次; 达到上限后锁定, 只保留人工指定.
        5. 人工指定档位后停止自动升降, 直到再次传入 auto.
    """

    def __init__(
        self,
        session_id: str,
        profile: str = "auto",
        max_switches: int = DEFAULT_MAX_SWITCHES,
        max_per_tier: int = DEFAULT_MAX_PER_TIER,
        event_limit: int = EVENT_LIMIT,
    ) -> None:
        self.session_id = session_id
        self.max_switches = max_switches
        self.max_per_tier = max_per_tier
        self.event_limit = event_limit
        self.manual = profile != "auto"
        # auto 从 1080p60 起步, 稳定后逐档回升; 人工指定则从该档开始
        self.current = "1080p60" if profile == "auto" else profile
        if self.current not in TIERS:
            raise ValueError(f"未知直连档位: {profile}")
        self.switches_used = 0
        self.entries: dict[str, int] = {}
        self.events: list[QualityEvent] = []
        self.locked = False
        self._breach_since: float | None = None
        self._breach_metric: str | None = None
        self._healthy_since: float | None = None

    @property
    def profile(self) -> str:
        """当前档位名."""
        return self.current

    def set_manual(self, profile: str, timestamp_s: float = 0.0) -> QualityEvent | None:
        """人工指定档位; 传 auto 恢复自动升降.

        :param profile: 直连档位或 auto
        :param timestamp_s: 切换时间, 用于事件记录
        :return: 发生实际切换时返回事件, 否则 None
        """
        if profile == "auto":
            self.manual = False
            self._reset_windows()
            return None
        if profile not in TIERS:
            raise ValueError(f"未知直连档位: {profile}")
        self.manual = True
        self._reset_windows()
        if profile == self.current:
            return None
        event = QualityEvent(
            ts=timestamp_s,
            from_profile=self.current,
            to_profile=profile,
            reason="manual",
            metric="profile",
            value=0.0,
            threshold=0.0,
            window_ms=0,
        )
        self.current = profile
        self._record(event)
        return event

    def observe(self, sample: QualitySample) -> QualityEvent | None:
        """输入一次采样, 返回本次产生的切换事件; 无切换返回 None.

        :param sample: 采样值
        :return: 切换事件或 None
        """
        if self.manual or self.locked:
            return None

        breach = self._downgrade_breach(sample)
        if breach is not None:
            self._healthy_since = None
            if self._breach_metric != breach[0]:
                self._breach_metric = breach[0]
                self._breach_since = sample.timestamp_s
                return None
            if self._breach_since is None:
                self._breach_since = sample.timestamp_s
                return None
            if sample.timestamp_s - self._breach_since + 1e-9 < DOWNGRADE_WINDOW_S:
                return None
            return self._downgrade(breach, sample.timestamp_s)

        self._breach_since = None
        self._breach_metric = None
        return self._maybe_upgrade(sample)

    # ---------------------------------------------------------------- 内部判定
    def _downgrade_breach(self, sample: QualitySample) -> tuple[str, float, float] | None:
        tier = TIERS[self.current]
        if self.current == FLOOR_PROFILE:
            # 地板只用于记录, 不触发降档
            return None
        if sample.fps < tier.downgrade_fps:
            return ("fps", sample.fps, tier.downgrade_fps)
        if sample.p95_ms > tier.downgrade_p95_ms:
            return ("p95_ms", sample.p95_ms, tier.downgrade_p95_ms)
        if sample.loss_pct >= tier.downgrade_loss_pct:
            return ("loss_pct", sample.loss_pct, tier.downgrade_loss_pct)
        if sample.gpu_percent > tier.downgrade_gpu_pct:
            return ("gpu_percent", sample.gpu_percent, tier.downgrade_gpu_pct)
        if sample.cpu_percent > tier.downgrade_cpu_pct:
            return ("cpu_percent", sample.cpu_percent, tier.downgrade_cpu_pct)
        if sample.bandwidth_kbps < tier.min_bandwidth_kbps:
            return ("bandwidth_kbps", sample.bandwidth_kbps, tier.min_bandwidth_kbps)
        return None

    def _downgrade(self, breach: tuple[str, float, float], timestamp_s: float) -> QualityEvent | None:
        index = DIRECT_TIERS.index(self.current)
        if index + 1 >= len(DIRECT_TIERS):
            return None
        target = DIRECT_TIERS[index + 1]
        if not self._can_switch_to(target):
            self.locked = True
            return None
        event = QualityEvent(
            ts=timestamp_s,
            from_profile=self.current,
            to_profile=target,
            reason="auto_degrade",
            metric=breach[0],
            value=breach[1],
            threshold=breach[2],
            window_ms=int(DOWNGRADE_WINDOW_S * 1000),
        )
        self.current = target
        self._count_switch(target)
        self._reset_windows()
        self._record(event)
        return event

    def _maybe_upgrade(self, sample: QualitySample) -> QualityEvent | None:
        index = DIRECT_TIERS.index(self.current)
        if index == 0:
            return None
        target = DIRECT_TIERS[index - 1]
        target_tier = TIERS[target]
        current_tier = TIERS[self.current]
        healthy = (
            sample.fps >= current_tier.fps * 0.98
            and sample.p95_ms <= current_tier.recovery_p95_ms
            and sample.loss_pct < current_tier.recovery_loss_pct
            and sample.gpu_percent <= current_tier.recovery_gpu_pct
            and sample.cpu_percent <= current_tier.recovery_cpu_pct
            and sample.bandwidth_kbps >= target_tier.min_bandwidth_kbps * UPGRADE_BANDWIDTH_HEADROOM
        )
        if not healthy:
            self._healthy_since = None
            return None
        if self._healthy_since is None:
            self._healthy_since = sample.timestamp_s
            return None
        if sample.timestamp_s - self._healthy_since + 1e-9 < RECOVERY_WINDOW_S:
            return None
        if not self._can_switch_to(target):
            self.locked = True
            return None
        event = QualityEvent(
            ts=sample.timestamp_s,
            from_profile=self.current,
            to_profile=target,
            reason="auto_recover",
            metric="stable_window",
            value=round(sample.timestamp_s - self._healthy_since, 3),
            threshold=RECOVERY_WINDOW_S,
            window_ms=int(RECOVERY_WINDOW_S * 1000),
        )
        self.current = target
        self._count_switch(target)
        self._reset_windows()
        self._record(event)
        return event

    def _can_switch_to(self, target: str) -> bool:
        if self.switches_used >= self.max_switches:
            return False
        return self.entries.get(target, 0) < self.max_per_tier

    def _count_switch(self, target: str) -> None:
        self.switches_used += 1
        self.entries[target] = self.entries.get(target, 0) + 1

    def _record(self, event: QualityEvent) -> None:
        self.events.append(event)
        if len(self.events) > self.event_limit:
            del self.events[0]

    def _reset_windows(self) -> None:
        self._breach_since = None
        self._breach_metric = None
        self._healthy_since = None

    def snapshot(self) -> dict[str, Any]:
        """返回契约 v1.1 要求的画质与切换字段."""
        return {
            "quality_profile": self.current,
            "switches_used": self.switches_used,
            "switches_limit": self.max_switches,
            "quality_events": [event.to_dict() for event in self.events],
        }
