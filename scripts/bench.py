# ==========================================================================================
# 文件        : bench.py
# 所属目录    : scripts/
# 文件用途    : V1 性能基准: 采集/编码/解码耗时、帧率、SSIM、输入延迟、CPU 与内存
# 架构定位    : 验收测量装置, 只读调用 PoC 采集与编码模块及本机输入注入, 不改产品实现
# 核心功能    : 分段计时; p50/p95; SSIM; 进程 CPU/RSS; 每项指标标注口径; 证据落构建根
# 职责边界    : 不接公网; 不改中继; 不把本机等价或软编数值写成端到端实测
# ==========================================================================================

"""V1 性能基准.

用法:
    powershell -File scripts/bench.ps1
    python scripts/bench.py [--duration 8] [--fps 30] [--bitrate 4000000] [--skip-input]

口径:
    实测      —— 本机真实采集、编码、解码或真实系统输入注入
    替代口径  —— 本机等价测量, 不含网络、远端渲染或硬件编码
    未验证    —— 本机条件不具备, 只记录原因, 不给数值

证据目录: %RDC_BUILD_ROOT%\\evidence\\bench\\<时间戳>\\
退出码: 采集或输入测量步骤失败返回 1, 其余返回 0; 未验证项不冒充通过.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import av
import cv2
import numpy as np
import psutil

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _candidate in (str(PROJECT_ROOT / "scripts"), str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

STATUS_MEASURED = "实测"
STATUS_SUBSTITUTE = "替代口径"
STATUS_UNVERIFIED = "未验证"

INPUT_TEXT = "RDC bench 2026"
SSIM_WIDTH = 320
SSIM_HEIGHT = 180


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _build_root() -> Path:
    configured = os.environ.get("RDC_BUILD_ROOT")
    if configured and Path(configured).is_dir():
        return Path(configured)
    fallback = PROJECT_ROOT / "evidence"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _ssim(reference: np.ndarray, candidate: np.ndarray) -> float:
    """单通道 SSIM, 11x11 高斯窗, 标准常数 C1/C2."""
    first = reference.astype(np.float64)
    second = candidate.astype(np.float64)
    kernel = (11, 11)
    sigma = 1.5
    mu_first = cv2.GaussianBlur(first, kernel, sigma)
    mu_second = cv2.GaussianBlur(second, kernel, sigma)
    mu_first_sq = mu_first * mu_first
    mu_second_sq = mu_second * mu_second
    mu_both = mu_first * mu_second
    sigma_first_sq = cv2.GaussianBlur(first * first, kernel, sigma) - mu_first_sq
    sigma_second_sq = cv2.GaussianBlur(second * second, kernel, sigma) - mu_second_sq
    sigma_both = cv2.GaussianBlur(first * second, kernel, sigma) - mu_both
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    numerator = (2 * mu_both + c1) * (2 * sigma_both + c2)
    denominator = (mu_first_sq + mu_second_sq + c1) * (sigma_first_sq + sigma_second_sq + c2)
    return float(np.mean(numerator / denominator))


def _nal_has_sps(payload: bytes) -> bool:
    """Annex B 载荷是否含 SPS(nal type 7); 用于定位可独立解码的关键帧."""
    return b"\x00\x00\x00\x01\x67" in payload or b"\x00\x00\x01\x67" in payload


def _decode_packets(
    packets: list[tuple[int, bytes]], extradata: bytes = b""
) -> tuple[list[tuple[int, np.ndarray]], list[float], int]:
    """从首个带 SPS 的包开始解码并逐包计时.

    PoC 编码器 open() 试编的首帧包未返回, 软编时该包携带 SPS/PPS 与 IDR;
    因此从首个带 SPS 的包起解, 前面不可独立解码的包计入跳过数, 不伪造帧.
    """
    start = next((index for index, (_pts, payload) in enumerate(packets) if _nal_has_sps(payload)), 0)
    decoder = av.CodecContext.create("h264", "r")
    if extradata:
        decoder.extradata = extradata
    decoded: list[tuple[int, np.ndarray]] = []
    decode_ms: list[float] = []
    for pts, payload in packets[start:]:
        started = time.perf_counter()
        frames = decoder.decode(av.Packet(payload))
        finished = time.perf_counter()
        cost = (finished - started) * 1000.0 / max(1, len(frames))
        for frame in frames:
            frame_pts = pts if frame.pts is None else int(frame.pts)
            decoded.append((frame_pts, frame.to_ndarray(format="gray")))
            decode_ms.append(cost)
    for frame in decoder.decode(None):
        pts = packets[-1][0] if packets else 0
        if frame.pts is not None:
            pts = int(frame.pts)
        decoded.append((pts, frame.to_ndarray(format="gray")))
        decode_ms.append(0.0)
    return decoded, decode_ms, start


def measure_pipeline(evidence_dir: Path, duration: float, fps: int, bitrate: int) -> dict[str, Any]:
    """真实采集 → 编码 → 解码 → SSIM, 逐帧计时并采样 CPU/RSS."""
    from poc.host_capture.capture import DxcamCapture
    from poc.host_encode.encode import VideoEncoder

    from input_probe import percentile

    capture = DxcamCapture()
    capture.open()
    encoder = VideoEncoder(capture.width, capture.height, fps, bitrate)
    encoder.open()
    process = psutil.Process()
    process.cpu_percent(interval=None)

    interval = 1.0 / float(fps)
    started = time.perf_counter()
    deadline = started
    next_sample = started + 1.0
    capture_ms: list[float] = []
    encode_ms: list[float] = []
    cpu_samples: list[float] = []
    rss_samples: list[int] = [process.memory_info().rss]
    packets: list[tuple[int, bytes]] = []
    references: dict[int, np.ndarray] = {}
    frame_index = 0
    dropped = 0
    total_bytes = 0

    try:
        while time.perf_counter() - started < duration:
            deadline += interval
            frame = capture.grab()
            now = time.perf_counter()
            if frame is None:
                dropped += 1
            else:
                encoded_packets, encoded = encoder.encode(frame.image, frame_index)
                for packet in encoded_packets:
                    if packet.size:
                        pts = frame_index if packet.pts is None else int(packet.pts)
                        packets.append((pts, bytes(packet)))
                capture_ms.append(frame.capture_ms)
                encode_ms.append(encoded.encode_ms)
                total_bytes += encoded.size_bytes
                gray = cv2.cvtColor(frame.image, cv2.COLOR_BGRA2GRAY)
                references[frame_index] = cv2.resize(gray, (SSIM_WIDTH, SSIM_HEIGHT))
                frame_index += 1
            if now >= next_sample:
                cpu_samples.append(process.cpu_percent(interval=None))
                rss_samples.append(process.memory_info().rss)
                next_sample = now + 1.0
            sleep_for = deadline - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                deadline = now
    finally:
        for packet in encoder.flush():
            if packet.size:
                packets.append((frame_index, bytes(packet)))
        capture.close()

    elapsed = max(time.perf_counter() - started, 0.001)
    extradata = bytes(getattr(encoder.context, "extradata", b"") or b"")
    decoded, decode_ms, decode_start = _decode_packets(packets, extradata)

    ssim_values: list[float] = []
    for pts, gray in decoded:
        reference = references.get(pts)
        if reference is None:
            continue
        ssim_values.append(_ssim(reference, cv2.resize(gray, (SSIM_WIDTH, SSIM_HEIGHT))))

    pipeline_ms = [
        capture_ms[pts] + encode_ms[pts] + cost
        for (pts, _frame), cost in zip(decoded, decode_ms, strict=False)
        if 0 <= pts < len(capture_ms)
    ]
    sample: Path | None = None
    if references:
        sample = evidence_dir / "reference_frame.png"
        cv2.imwrite(str(sample), references[sorted(references)[0]])

    return {
        "resolution": f"{capture.width}x{capture.height}",
        "fps_target": fps,
        "bitrate_target_bps": bitrate,
        "duration_s": round(elapsed, 3),
        "frames": frame_index,
        "dropped": dropped,
        "fps_actual": round(frame_index / elapsed, 2),
        "capture_ms_p50": percentile(capture_ms, 0.50),
        "capture_ms_p95": percentile(capture_ms, 0.95),
        "encode_ms_p50": percentile(encode_ms, 0.50),
        "encode_ms_p95": percentile(encode_ms, 0.95),
        "decode_ms_p50": percentile(decode_ms, 0.50),
        "decode_ms_p95": percentile(decode_ms, 0.95),
        "pipeline_ms_p50": percentile(pipeline_ms, 0.50),
        "pipeline_ms_p95": percentile(pipeline_ms, 0.95),
        "ssim_p50": round(percentile(ssim_values, 0.50), 4) if ssim_values else None,
        "ssim_p95": round(percentile(ssim_values, 0.95), 4) if ssim_values else None,
        "ssim_samples": len(ssim_values),
        "frames_decoded": len(decoded),
        "decode_start_packet": decode_start,
        "decode_skipped_frames": decode_start,
        "decode_note": "首个带 SPS 的包之前无法独立解码(编码器试编首帧包未返回), 已按跳过计, 不伪造帧",
        "bitrate_kbps_actual": round(total_bytes * 8 / elapsed / 1000.0, 1),
        "process_cpu_percent_avg": round(float(np.mean(cpu_samples)), 2) if cpu_samples else None,
        "rss_start_mib": round(rss_samples[0] / 1048576, 1),
        "rss_peak_mib": round(max(rss_samples) / 1048576, 1),
        "encoder": encoder.summary(),
        "reference_frame": str(sample) if sample else None,
    }


def _wait_state(path: Path, predicate: Any, timeout: float) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    latest: dict[str, Any] | None = None
    while time.time() < deadline:
        if path.exists():
            latest = json.loads(path.read_text(encoding="utf-8"))
            if predicate(latest):
                return latest
        time.sleep(0.05)
    return None


def measure_input(evidence_dir: Path) -> tuple[dict[str, Any] | None, str]:
    """本机真实键鼠注入延迟 p50/p95; 非 Windows 或不可用时返回原因."""
    from input_probe import Injector, percentile

    if sys.platform != "win32":
        return None, "非 Windows, 本机输入注入未执行"
    work = evidence_dir / "input"
    work.mkdir(parents=True, exist_ok=True)
    ready_path = work / "ready.json"
    state_path = work / "state.json"
    for path in (ready_path, state_path):
        if path.exists():
            path.unlink()
    target = subprocess.Popen(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "smoke_target.py"),
            "--ready",
            str(ready_path),
            "--state",
            str(state_path),
        ],
        cwd=str(PROJECT_ROOT),
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not ready_path.exists():
            time.sleep(0.1)
        if not ready_path.exists():
            return None, "靶窗口未在 20 秒内就绪"
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        time.sleep(1.5)
        injector = Injector()
        box = ready["text"]
        injector.click(box["x"] + box["w"] // 2, box["y"] + box["h"] // 2)
        time.sleep(0.3)
        sent_at = injector.type_text(INPUT_TEXT)
        state = _wait_state(state_path, lambda item: item.get("text") == INPUT_TEXT, 8.0)
        if state is None:
            return None, "键鼠注入未在靶窗口生效"
        characters = [event for event in state["key_events"] if event.get("char")]
        latencies = [
            (float(event["received_ns"]) - float(sent_at[index])) / 1_000_000
            for index, event in enumerate(characters[-len(sent_at) :])
        ]
        return {
            "p50_ms": percentile(latencies, 0.50),
            "p95_ms": percentile(latencies, 0.95),
            "samples": len(latencies),
            "typed": state["text"],
        }, ""
    finally:
        target.terminate()
        try:
            target.wait(timeout=5)
        except subprocess.TimeoutExpired:
            target.kill()


def _print_report(payload: dict[str, Any], evidence: Path, elapsed: float) -> None:
    pipeline = payload.get("pipeline") or {}
    control = payload.get("input") or {}
    encoder = pipeline.get("encoder") or {}
    _emit("")
    _emit("=" * 100)
    _emit(f"V1 性能基准  {payload['generated_at']}  用时 {elapsed:.1f}s")
    _emit("=" * 100)
    _emit(f"{'指标':<24}{'数值':<24}{'口径'}")
    _emit("-" * 100)
    rows = [
        ("分辨率", pipeline.get("resolution", "-"), STATUS_MEASURED),
        (
            "目标/实际帧率",
            f"{pipeline.get('fps_target', '-')} / {pipeline.get('fps_actual', '-')} fps",
            STATUS_MEASURED,
        ),
        (
            "采集耗时 p50/p95",
            f"{pipeline.get('capture_ms_p50')} / {pipeline.get('capture_ms_p95')} ms",
            STATUS_MEASURED,
        ),
        ("编码耗时 p50/p95", f"{pipeline.get('encode_ms_p50')} / {pipeline.get('encode_ms_p95')} ms", STATUS_MEASURED),
        ("解码耗时 p50/p95", f"{pipeline.get('decode_ms_p50')} / {pipeline.get('decode_ms_p95')} ms", STATUS_MEASURED),
        (
            "单帧管线 p50/p95",
            f"{pipeline.get('pipeline_ms_p50')} / {pipeline.get('pipeline_ms_p95')} ms",
            STATUS_SUBSTITUTE,
        ),
        ("SSIM p50/p95", f"{pipeline.get('ssim_p50')} / {pipeline.get('ssim_p95')}", STATUS_SUBSTITUTE),
        (
            "输入延迟 p50/p95",
            f"{control.get('p50_ms')} / {control.get('p95_ms')} ms"
            if control
            else f"未测量: {payload['input_reason']}",
            STATUS_SUBSTITUTE if control else STATUS_UNVERIFIED,
        ),
        ("进程 CPU 平均", f"{pipeline.get('process_cpu_percent_avg')} %", STATUS_MEASURED),
        (
            "进程内存 峰值/起始",
            f"{pipeline.get('rss_peak_mib')} / {pipeline.get('rss_start_mib')} MiB",
            STATUS_MEASURED,
        ),
        ("编码器", f"{encoder.get('encoder', '-')}({encoder.get('kind', '-')})", STATUS_MEASURED),
    ]
    for name, value, caliber in rows:
        _emit(f"{name:<24}{value:<24}{caliber}")
    _emit("-" * 100)
    _emit("未验证项(不给数值):")
    for item in payload.get("unverified", []):
        _emit(f"  - {item['name']}: {item['reason']}")
    _emit(f"证据: {evidence}")
    _emit("口径说明: 采集/编码/解码为本机真实执行; 单帧管线不含网络与渲染; 输入延迟为本机注入.")
    _emit("            端到端网络延迟、4K60 硬件编解码、中继吞吐 均未验证, 不冒充实测通过.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V1 性能基准")
    parser.add_argument("--duration", type=float, default=8.0, help="采集编码时长秒, 默认 8")
    parser.add_argument("--fps", type=int, default=30, help="目标帧率, 默认 30")
    parser.add_argument("--bitrate", type=int, default=4_000_000, help="目标码率 bps, 默认 4000000")
    parser.add_argument("--skip-input", action="store_true", help="跳过本机输入延迟测量")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    started = time.perf_counter()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence = _build_root() / "evidence" / "bench" / stamp
    evidence.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("RDC_BUILD_ROOT", str(_build_root()))

    failures: list[str] = []
    pipeline: dict[str, Any] = {}
    try:
        pipeline = measure_pipeline(evidence, args.duration, args.fps, args.bitrate)
    except (OSError, RuntimeError, ValueError, av.error.FFmpegError) as error:
        failures.append(f"采集编码解码测量失败: {type(error).__name__}: {error}")

    control: dict[str, Any] = {}
    input_reason = ""
    if not args.skip_input:
        try:
            measured, input_reason = measure_input(evidence)
        except (OSError, RuntimeError, ValueError) as error:
            failures.append(f"输入延迟测量失败: {type(error).__name__}: {error}")
        else:
            if measured is not None:
                control = measured

    encoder_kind = (pipeline.get("encoder") or {}).get("kind")
    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": round(time.perf_counter() - started, 2),
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "cpu_count": os.cpu_count(),
        },
        "pipeline": pipeline,
        "pipeline_caliber": "本机真实采集/编码/解码; 不含网络与远端渲染",
        "input": control,
        "input_caliber": "本机 SendInput 注入到 Tk 靶窗口; 不含采集编码网络与远端渲染",
        "input_reason": input_reason,
        "failures": failures,
        "unverified": [
            {"name": "端到端控制延迟", "reason": "无高速相机与真实媒体回传链路, 无法测端到端"},
            {"name": "4K60 硬件编解码", "reason": "本机无可用硬件编码器, 只有软编参考"}
            if encoder_kind != "hardware"
            else {"name": "4K60 硬件编解码", "reason": "本机未接 4K 采集与渲染, 不构成 4K60 端到端"},
            {"name": "中继吞吐与跨网抖动", "reason": "未接入 ECS 与真实公网链路, 本轮不触碰服务器"},
        ],
    }
    (evidence / "bench.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_report(payload, evidence, payload["elapsed_s"])
    if failures:
        _emit("结果: 基准测量失败 " + "; ".join(failures))
        return 1
    _emit("结果: 基准完成(未验证项未冒充通过)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
