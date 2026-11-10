# ==========================================================================================
# 文件        : run_p0_03_capture_encode.py
# 所属目录    : poc/tools/
# 文件用途    : 第 0 批 P0-03 工作包执行器, 连续采集并编码 Windows 屏幕, 输出证据与统计
# 架构定位    : PoC 测试装置, 串联 host_capture 与 host_encode, 不进入发布路径
# 核心功能    : 5 分钟采集编码流水线; 逐帧 CSV; 汇总 JSON; 码流落盘
# 职责边界    : 只驱动采集与编码并计时; 不传输, 不渲染, 不判断画质
# 运行环境    : Python 3.12; Windows 11 被控端; 构建根由 RDC_BUILD_ROOT 注入
# 依赖关系    : 引用 host_capture/capture.py 与 host_encode/encode.py
# ==========================================================================================

"""P0-03 采集与编码执行器."""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import psutil

from poc.host_capture.capture import DxcamCapture
from poc.host_encode.encode import VideoEncoder


def _build_root() -> Path:
    root = os.environ.get("RDC_BUILD_ROOT")
    if not root:
        raise RuntimeError("未设置 RDC_BUILD_ROOT, 不得写入系统盘或项目目录")
    return Path(root)


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((percent / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def run(duration_s: float, fps: int, bitrate: int) -> dict[str, Any]:
    """执行采集编码流水线并写出证据.

    :param duration_s: 运行时长秒数
    :type duration_s: float
    :param fps: 目标帧率
    :type fps: int
    :param bitrate: 目标码率, 单位 bps
    :type bitrate: int
    :return: 汇总统计
    :rtype: dict
    """
    root = _build_root()
    csv_path = root / "evidence" / "poc" / "csv" / "p0-03_pipeline.csv"
    summary_path = root / "evidence" / "poc" / "csv" / "p0-03_summary.json"
    stream_path = root / "evidence" / "poc" / "video" / "p0-03_stream.h264"
    for path in (csv_path, summary_path, stream_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    capture = DxcamCapture()
    capture.open()
    encoder = VideoEncoder(capture.width, capture.height, fps, bitrate)
    encoder.open()
    process = psutil.Process()
    process.cpu_percent(interval=None)

    interval = 1.0 / float(fps)
    started = time.perf_counter()
    deadline = started
    rows: list[list[Any]] = []
    capture_values: list[float] = []
    encode_values: list[float] = []
    cpu_samples: list[float] = []
    total_bytes = 0
    dropped = 0
    monotonic_ok = True
    last_ts: float | None = None
    next_cpu_sample = started + 1.0

    with stream_path.open("wb") as stream:
        try:
            while time.perf_counter() - started < duration_s:
                deadline += interval
                frame = capture.grab()
                now = time.perf_counter()
                if frame is None:
                    dropped += 1
                    rows.append([len(rows), "", "", "", 0, 0, 1])
                else:
                    if last_ts is not None and frame.monotonic_ms <= last_ts:
                        monotonic_ok = False
                    last_ts = frame.monotonic_ms
                    packets, encoded = encoder.encode(frame.image, len(rows))
                    for packet in packets:
                        if packet.size:
                            stream.write(bytes(packet))
                    total_bytes += encoded.size_bytes
                    capture_values.append(frame.capture_ms)
                    encode_values.append(encoded.encode_ms)
                    rows.append(
                        [
                            len(rows),
                            round(frame.monotonic_ms, 3),
                            round(frame.capture_ms, 3),
                            round(encoded.encode_ms, 3),
                            encoded.packets,
                            encoded.size_bytes,
                            0,
                        ]
                    )
                if now >= next_cpu_sample:
                    cpu_samples.append(process.cpu_percent(interval=None))
                    next_cpu_sample = now + 1.0
                sleep_for = deadline - now
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    deadline = now
        finally:
            for packet in encoder.flush():
                if packet.size:
                    stream.write(bytes(packet))
            capture.close()

    elapsed = max(time.perf_counter() - started, 0.001)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frame_index", "monotonic_ms", "capture_ms", "encode_ms", "packets", "bytes", "dropped"])
        writer.writerows(rows)

    summary = {
        "work_package": "P0-03",
        "resolution": f"{capture.width}x{capture.height}",
        "fps_target": fps,
        "bitrate_target_bps": bitrate,
        "duration_s": round(elapsed, 3),
        "frames": sum(1 for row in rows if row[6] == 0),
        "drops": dropped,
        "fps_actual": round(sum(1 for row in rows if row[6] == 0) / elapsed, 2),
        "capture_ms_p50": round(statistics.median(capture_values), 3) if capture_values else None,
        "capture_ms_p95": round(_percentile(capture_values, 95), 3) if capture_values else None,
        "encode_ms_p50": round(statistics.median(encode_values), 3) if encode_values else None,
        "encode_ms_p95": round(_percentile(encode_values, 95), 3) if encode_values else None,
        "encode_ms_max": round(max(encode_values), 3) if encode_values else None,
        "bytes_total": total_bytes,
        "bitrate_kbps_actual": round(total_bytes * 8 / elapsed / 1000.0, 1),
        "cpu_percent_avg": round(statistics.mean(cpu_samples), 2) if cpu_samples else None,
        "monotonic_ok": monotonic_ok,
        "encoder": encoder.summary(),
        "csv": str(csv_path),
        "stream": str(stream_path),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    """命令行入口.

    :return: 无
    :rtype: None
    """
    parser = argparse.ArgumentParser(description="P0-03 采集与编码执行器")
    parser.add_argument("--duration", type=float, default=300.0, help="运行时长秒, 默认 300")
    parser.add_argument("--fps", type=int, default=30, help="目标帧率, 默认 30")
    parser.add_argument("--bitrate", type=int, default=4_000_000, help="目标码率 bps, 默认 4000000")
    args = parser.parse_args()
    summary = run(args.duration, args.fps, args.bitrate)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
