# ==========================================================================================
# 文件        : capture.py
# 所属目录    : poc/host_capture/
# 文件用途    : PoC Windows 采集模块, 用 dxcam 取 DXGI 帧并记录单调时钟时间戳
# 架构定位    : PoC 采集层, 正式版由 native/capture_win 的 C 模块替代, 接口保持不变
# 核心功能    : 打开输出; 抓帧; 采集耗时; 连续采集测试与 CSV 输出
# 职责边界    : 只负责取帧与计时; 不编码, 不传输, 不做色彩空间转换
# 运行环境    : Python 3.12 + dxcam 0.3.0; 仅 Windows 被控端
# 依赖关系    : 被 tools/run_p0_03_capture_encode.py 与 tools/run_p0_02_smoke.py 引用
# ==========================================================================================

"""PoC Windows 采集模块."""

from __future__ import annotations

import csv
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dxcam
import numpy as np


@dataclass(frozen=True)
class CapturedFrame:
    """一帧采集结果.

    :param image: BGRA 帧数据
    :type image: numpy.ndarray
    :param monotonic_ms: 采集完成的单调时钟毫秒值
    :type monotonic_ms: float
    :param capture_ms: 本次抓帧耗时毫秒
    :type capture_ms: float
    """

    image: np.ndarray
    monotonic_ms: float
    capture_ms: float


# ================================ DxcamCapture ================================

class DxcamCapture:
    """基于 dxcam 的 Windows 采集源.

    工作流程:
        1. open() 创建指定输出的采集对象.
        2. grab() 抓一帧并记录单调时钟与耗时.
        3. close() 释放采集对象.

    实例属性清单:
        output_index (int): 显示器序号.
        camera (dxcam 采集对象 或 None): 底层采集句柄.
        width (int): 帧宽.
        height (int): 帧高.

    构造参数:
        :param output_index: 显示器序号, 0 表示主屏
        :type output_index: int

    运行约束:
        单进程独占采集; Windows 11 且有 DXGI Desktop Duplication 可用.
    """

    def __init__(self, output_index: int = 0) -> None:
        self.output_index = output_index
        self.camera = None
        self.width = 0
        self.height = 0

    def open(self) -> None:
        """打开采集源.

        :return: 无
        :rtype: None
        :raises RuntimeError: dxcam 无法创建采集对象时抛出
        """
        self.camera = dxcam.create(output_idx=self.output_index, output_color="BGRA")
        if self.camera is None:
            raise RuntimeError("dxcam 创建采集对象失败")
        self.width = int(self.camera.width)
        self.height = int(self.camera.height)

    def grab(self) -> CapturedFrame | None:
        """抓取一帧.

        :return: 采集结果, 没有新帧时返回 None
        :rtype: CapturedFrame 或 None
        """
        if self.camera is None:
            raise RuntimeError("采集源尚未打开")
        started = time.perf_counter()
        image = self.camera.grab()
        finished = time.perf_counter()
        if image is None:
            return None
        return CapturedFrame(
            image=image,
            monotonic_ms=finished * 1000.0,
            capture_ms=(finished - started) * 1000.0,
        )

    def close(self) -> None:
        """释放采集源.

        :return: 无
        :rtype: None
        """
        self.camera = None


# ================================ 连续采集测试 ================================

def run_capture_test(duration_s: float, target_fps: int, csv_path: Path) -> dict[str, Any]:
    """连续采集指定时长并输出 CSV 与统计.

    :param duration_s: 采集时长秒数
    :type duration_s: float
    :param target_fps: 目标帧率
    :type target_fps: int
    :param csv_path: CSV 输出路径
    :type csv_path: Path
    :return: 采集统计
    :rtype: dict
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    capture = DxcamCapture()
    capture.open()
    interval = 1.0 / float(target_fps)
    started = time.perf_counter()
    next_deadline = started
    rows: list[list[Any]] = []
    last_ts: float | None = None
    monotonic_ok = True
    drops = 0
    try:
        while time.perf_counter() - started < duration_s:
            next_deadline += interval
            frame = capture.grab()
            now = time.perf_counter()
            if frame is None:
                drops += 1
            else:
                if last_ts is not None and frame.monotonic_ms <= last_ts:
                    monotonic_ok = False
                last_ts = frame.monotonic_ms
                rows.append([len(rows), round(frame.monotonic_ms, 3), round(frame.capture_ms, 3), 0])
            sleep_for = next_deadline - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_deadline = now
    finally:
        capture.close()

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frame_index", "monotonic_ms", "capture_ms", "dropped"])
        writer.writerows(rows)

    capture_values = [row[2] for row in rows]
    elapsed = max(time.perf_counter() - started, 0.001)
    return {
        "frames": len(rows),
        "drops": drops,
        "duration_s": round(elapsed, 3),
        "fps_actual": round(len(rows) / elapsed, 2),
        "capture_ms_p50": round(statistics.median(capture_values), 3) if capture_values else None,
        "capture_ms_p95": round(_percentile(capture_values, 95), 3) if capture_values else None,
        "monotonic_ok": monotonic_ok,
        "resolution": f"{capture.width}x{capture.height}",
        "csv": str(csv_path),
    }


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((percent / 100.0) * (len(ordered) - 1))))
    return ordered[index]
