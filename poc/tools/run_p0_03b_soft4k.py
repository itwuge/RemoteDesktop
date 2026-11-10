# ==========================================================================================
# 文件        : run_p0_03b_soft4k.py
# 所属目录    : poc/tools/
# 文件用途    : 4K60 软件编码上限测试, 判定 libx264 软编能否承载 4K60
# 架构定位    : P0-03 的补充取证工具, 只测编码, 不测采集与传输
# 核心功能    : 合成 4K 帧序列; 逐帧编码计时; 采样 CPU 与 RSS; 输出汇总 JSON 与逐帧 CSV
# 职责边界    : 不修改编码模块; 不进入发布路径; 结论标注软编口径
# 运行环境    : Python 3.12 + PyAV 19.0.2; Windows 被控端
# 依赖关系    : 引用 poc/host_encode/encode.py
# ==========================================================================================

"""4K60 软件编码上限测试.

本机显示器只有 1920x1080, 无法真实采集 3840x2160, 因此本测试只覆盖编码环节:
用合成 4K 帧驱动 libx264, 测量可达帧率、单帧耗时、码率、CPU 与内存占用。
结论只能标注为「软编口径, 硬编待复测」, 不得当作 4K60 端到端通过。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
from pathlib import Path

import numpy as np
import psutil

from poc.host_encode.encode import VideoEncoder


def build_frames(width: int, height: int, count: int) -> list[np.ndarray]:
    """预生成带移动内容的 BGRA 帧, 避免把生成耗时算进编码耗时.

    :param width: 帧宽
    :param height: 帧高
    :param count: 帧数量
    :return: BGRA 帧列表
    """
    y = np.linspace(0, 255, height, dtype=np.uint8).reshape(height, 1)
    x = np.linspace(0, 255, width, dtype=np.uint8).reshape(1, width)
    base = np.empty((height, width, 4), dtype=np.uint8)
    base[..., 0] = (x // 2 + y // 2).astype(np.uint8)
    base[..., 1] = (x * 0.7 + y * 0.3).astype(np.uint8)
    base[..., 2] = (255 - x // 2 + y // 4).astype(np.uint8)
    base[..., 3] = 255

    frames: list[np.ndarray] = []
    for index in range(count):
        frame = base.copy()
        left = (index * 97) % max(1, width - 320)
        top = (index * 53) % max(1, height - 180)
        frame[top : top + 180, left : left + 320, :3] = 255
        frame[top + 40 : top + 140, left + 40 : left + 280, :3] = 0
        frames.append(frame)
    return frames


def percentile(values: list[float], ratio: float) -> float:
    """计算百分位, 空列表返回 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[position]


def main() -> int:
    parser = argparse.ArgumentParser(description="4K60 software encode ceiling test")
    parser.add_argument("--width", type=int, default=3840)
    parser.add_argument("--height", type=int, default=2160)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--bitrate", type=int, default=25_000_000)
    parser.add_argument("--duration", type=float, default=20.0)
    args = parser.parse_args()

    root = Path(os.environ.get("RDC_BUILD_ROOT", r"D:\RDC-Build"))
    csv_dir = root / "evidence" / "poc" / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)

    frames = build_frames(args.width, args.height, 12)
    encoder = VideoEncoder(args.width, args.height, args.fps, args.bitrate, force_encoder="libx264")
    encoder.open()
    if encoder.encoder_kind != "software":
        raise SystemExit(f"预期软件编码, 实际选中 {encoder.encoder_name} ({encoder.encoder_kind})")

    process = psutil.Process()
    rss_start = process.memory_info().rss
    cpu_samples: list[float] = []
    rss_samples: list[int] = [rss_start]
    encode_ms: list[float] = []
    rows: list[dict[str, float | int]] = []
    total_bytes = 0
    packets_total = 0
    frame_index = 0
    started = time.perf_counter()
    next_sample = started
    budget_ms = 1000.0 / args.fps

    while time.perf_counter() - started < args.duration:
        image = frames[frame_index % len(frames)]
        packets, encoded = encoder.encode(image, frame_index)
        total_bytes += encoded.size_bytes
        packets_total += encoded.packets
        encode_ms.append(encoded.encode_ms)
        rows.append(
            {
                "frame_index": frame_index,
                "encode_ms": round(encoded.encode_ms, 3),
                "packets": encoded.packets,
                "bytes": encoded.size_bytes,
                "over_budget": int(encoded.encode_ms > budget_ms),
            }
        )
        frame_index += 1
        now = time.perf_counter()
        if now >= next_sample:
            cpu_samples.append(process.cpu_percent(interval=None))
            rss_samples.append(process.memory_info().rss)
            next_sample = now + 1.0

    elapsed = time.perf_counter() - started
    flushed = encoder.flush()
    total_bytes += sum(packet.size for packet in flushed if packet.size)
    packets_total += len(flushed)

    rss_end = process.memory_info().rss
    summary = {
        "work_package": "P0-03b",
        "test_kind": "software-encode-only",
        "qualifier": "软编口径, 硬编待复测; 4K60 端到端未验证",
        "resolution": f"{args.width}x{args.height}",
        "fps_target": args.fps,
        "bitrate_target_bps": args.bitrate,
        "duration_s": round(elapsed, 3),
        "frames_encoded": frame_index,
        "achieved_fps": round(frame_index / elapsed, 2),
        "frame_budget_ms": round(budget_ms, 3),
        "encode_ms_p50": round(statistics.median(encode_ms), 3),
        "encode_ms_p95": round(percentile(encode_ms, 0.95), 3),
        "encode_ms_max": round(max(encode_ms), 3),
        "frames_over_budget": sum(1 for value in encode_ms if value > budget_ms),
        "bytes_total": total_bytes,
        "packets_total": packets_total,
        "bitrate_kbps_actual": round(total_bytes * 8 / elapsed / 1000, 1),
        "process_cpu_percent_avg": round(statistics.mean(cpu_samples), 2) if cpu_samples else None,
        "process_cpu_cores_avg": round(statistics.mean(cpu_samples) / 100, 2) if cpu_samples else None,
        "rss_start_mib": round(rss_start / 1048576, 1),
        "rss_end_mib": round(rss_end / 1048576, 1),
        "rss_growth_mib": round((rss_end - rss_start) / 1048576, 1),
        "rss_peak_mib": round(max(rss_samples) / 1048576, 1),
        "encoder": encoder.summary(),
        "bottleneck": "encode",
        "bottleneck_ratio": round(statistics.median(encode_ms) / budget_ms, 2),
    }

    csv_path = csv_dir / "p0-03b_soft4k_frames.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    json_path = csv_dir / "p0-03b_soft4k_summary.json"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"frames_csv={csv_path}")
    print(f"summary_json={json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
