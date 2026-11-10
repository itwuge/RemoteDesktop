# ==========================================================================================
# 文件        : ratelimit.py
# 所属目录    : src/rdc/files/
# 文件用途    : 低优先级通道的带宽分配策略与令牌桶限速
# 架构定位    : 业务层基础组件, 只依赖标准库; 可被传输引擎与测试直接复用
# 核心功能    : 计算可用带宽 = 链路带宽 - 视频地板档预留; 令牌桶按速率放行字节
# 职责边界    : 不读写文件; 不建连; 不感知会话与视频编码实现
# ==========================================================================================

"""带宽分配策略.

策略（低优先级通道）:

1. 传输通道固定标记为 ``low_priority``, 与视频流分属不同队列, 不参与视频抢占。
2. 传输可用带宽 = 链路带宽 − 视频地板档预留（默认 2500 KB/s）。
3. 请求限速超过可用带宽时收敛到可用带宽, 并在进度中标记 ``clamped=true``。
4. 收敛后可用带宽为 0 时拒绝开始传输, 返回 ``TRANSFER_RATE_INVALID``,
   保证视频流永远不跌破地板档。
5. 实际放行由令牌桶完成, 桶容量等于一个分块（256 KiB）, 允许首块立即发送,
   之后严格按 ``rate_bps`` 匀速补充。

验证方法（见 tests/test_files_transfer.py）:

- 注入可控时钟, 断言桶在 1.5 秒内恰好放行 ``rate_bps × 1.5`` 字节。
- 通过服务层断言: 首次轮询只放行一个分块, 时钟不前进时后续轮询放行 0 字节,
  前进 1 秒后增量等于 ``rate_limit_kbps × 1000``。
- 断言进度中 ``channel`` 恒为 ``low_priority`` 且 ``rate_limit_kbps`` 等于收敛后的预算。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_VIDEO_FLOOR_KBPS = 2500
CHANNEL_LOW_PRIORITY = "low_priority"


@dataclass(frozen=True)
class BandwidthPlan:
    """一次带宽分配结果."""

    link_kbps: int
    video_floor_kbps: int
    requested_kbps: int
    transfer_budget_kbps: int
    clamped: bool
    channel: str = CHANNEL_LOW_PRIORITY

    def to_dict(self) -> dict[str, Any]:
        """转换为可写入日志或进度快照的字典."""
        return {
            "link_kbps": self.link_kbps,
            "video_floor_kbps": self.video_floor_kbps,
            "requested_kbps": self.requested_kbps,
            "transfer_budget_kbps": self.transfer_budget_kbps,
            "clamped": self.clamped,
            "channel": self.channel,
        }


def plan_bandwidth(
    link_kbps: int,
    video_floor_kbps: int = DEFAULT_VIDEO_FLOOR_KBPS,
    requested_kbps: int = 0,
) -> BandwidthPlan:
    """计算低优先级传输可用带宽.

    :param link_kbps: 会话链路总带宽, KB/s
    :param video_floor_kbps: 视频地板档预留, KB/s
    :param requested_kbps: 用户请求限速, 0 表示用满可用带宽
    :return: 带宽分配结果, ``clamped`` 表示请求被收敛
    :raises ValueError: 任一带宽参数为负数
    """
    if link_kbps < 0:
        raise ValueError("链路带宽不能为负数")
    if video_floor_kbps < 0:
        raise ValueError("视频地板档预留不能为负数")
    if requested_kbps < 0:
        raise ValueError("传输限速不能为负数")

    available = max(0, link_kbps - video_floor_kbps)
    requested = requested_kbps if requested_kbps > 0 else available
    return BandwidthPlan(
        link_kbps=link_kbps,
        video_floor_kbps=video_floor_kbps,
        requested_kbps=requested_kbps,
        transfer_budget_kbps=min(requested, available),
        clamped=requested > available,
    )


class TokenBucket:
    """令牌桶限速; 时钟可注入, 便于确定性地验证速率."""

    def __init__(
        self,
        rate_bps: float,
        capacity: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        initial_tokens: float = 0.0,
    ) -> None:
        """初始化.

        :param rate_bps: 每秒补充的令牌数（字节）, 允许小数, 不截断精度
        :param capacity: 桶容量（字节）, 即单次最大突发量
        :param clock: 单调时钟, 测试可注入假时钟
        :param initial_tokens: 初始令牌数, 默认从 0 开始
        :raises ValueError: 速率或容量非正
        """
        if rate_bps <= 0:
            raise ValueError("令牌桶速率必须为正数")
        if capacity <= 0:
            raise ValueError("令牌桶容量必须为正数")
        self._rate_bps = rate_bps
        self._capacity = float(capacity)
        self._clock = clock
        self._tokens = min(float(initial_tokens), self._capacity)
        self._last_refill = clock()

    def take(self, want: int) -> int:
        """尝试取走 want 个令牌, 返回实际放行字节数."""
        if want <= 0:
            return 0
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_bps)
        allowed = int(min(self._tokens, float(want)))
        self._tokens -= allowed
        return allowed

    @property
    def rate_bps(self) -> float:
        """当前补充速率, 字节每秒."""
        return self._rate_bps

    @property
    def tokens(self) -> float:
        """当前剩余令牌数, 供测试与诊断读取."""
        return self._tokens
