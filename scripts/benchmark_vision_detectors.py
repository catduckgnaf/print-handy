#!/usr/bin/env python3
"""Reproducible offline benchmark comparing local failure-detection ONNX models (#1899).

Measures model size, session cold-load time, peak RSS delta, single-thread and
multi-thread CPU inference latency, and per-frame scores on Print Handy's real
camera frames (tests/runtime/fixtures/timelapse_real) and synthetic eval
sessions (tests/runtime/fixtures/vision_eval).

This script NEVER downloads models over the network. Point ``--models-dir`` at
a local directory containing any subset of:

* ``printguard-standard.onnx``
* ``printguard-light.onnx``
* ``printguard-legacy.onnx`` + ``printguard-legacy-prototypes.json``
* ``obico-failure-detector.onnx``

Usage:
    python scripts/benchmark_vision_detectors.py --models-dir /path/to/models
    python scripts/benchmark_vision_detectors.py --models-dir /path/to/models --json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL_FRAMES_DIR = ROOT / "tests" / "runtime" / "fixtures" / "timelapse_real"
SYNTH_DIR = ROOT / "tests" / "runtime" / "fixtures" / "vision_eval"

IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)

CANDIDATES = {
    "obico": {
        "file": "obico-failure-detector.onnx",
        "kind": "obico",
    },
    "printguard-standard": {
        "file": "printguard-standard.onnx",
        "kind": "printguard_prob",
        "resize_shortest": 256,
        "crop_px": 224,
        "grey": False,
    },
    "printguard-light": {
        "file": "printguard-light.onnx",
        "kind": "printguard_prob",
        "resize_shortest": 293,
        "crop_px": 256,
        "grey": False,
    },
    "printguard-legacy": {
        "file": "printguard-legacy.onnx",
        "prototypes": "printguard-legacy-prototypes.json",
        "kind": "printguard_proto",
        "resize_shortest": 256,
        "crop_px": 224,
        "grey": True,
    },
}


def _tile_windows(width: int, height: int, overlap: float = 0.20, max_ratio: int = 4):
    long_side = max(width, height)
    short_side = min(width, height)
    span = min(long_side, short_side * max_ratio)
    ratio = span / short_side
    if ratio <= 1.0 + overlap:
        starts = [(long_side - short_side) // 2]
    else:
        count = math.ceil((ratio - overlap) / (1.0 - overlap))
        margin = (long_side - span) // 2
        step = (span - short_side) / (count - 1)
        starts = [margin + round(i * step) for i in range(count)]
    if width >= height:
        return [(s, 0, s + height, height) for s in starts]
    return [(0, s, width, s + width) for s in starts]


def _preprocess_pg(tile, resize_shortest: int, crop_px: int, grey: bool):
    import numpy as np
    from PIL import Image

    resized = tile.resize((resize_shortest, resize_shortest), Image.Resampling.BILINEAR)
    if grey:
        resized = resized.convert("L").convert("RGB")
    offset = int(round((resize_shortest - crop_px) / 2.0))
    cropped = resized.crop((offset, offset, offset + crop_px, offset + crop_px))
    arr = np.asarray(cropped, dtype=np.float32) / np.float32(255.0)
    mean = np.asarray(IMAGENET_MEAN, dtype=np.float32)
    std = np.asarray(IMAGENET_STD, dtype=np.float32)
    norm = (arr - mean) / std
    return np.ascontiguousarray(norm.transpose(2, 0, 1)[np.newaxis, ...], dtype=np.float32)


def _preprocess_obico(rgb, width: int, height: int):
    import numpy as np
    from PIL import Image

    arr = np.asarray(rgb.resize((width, height), Image.Resampling.BILINEAR), dtype=np.float32)
    return np.ascontiguousarray(arr.transpose(2, 0, 1)[np.newaxis, ...]) / np.float32(255.0)


def run_worker(models_dir: Path, name: str, threads: int, frames_dir: Path) -> dict:
    import numpy as np
    import onnxruntime as ort
    from PIL import Image

    spec = CANDIDATES[name]
    model_path = models_dir / spec["file"]
    if not model_path.is_file():
        raise FileNotFoundError(f"missing model file: {model_path}")

    protos = None
    if spec["kind"] == "printguard_proto":
        proto_path = models_dir / spec["prototypes"]
        raw = json.loads(proto_path.read_text(encoding="utf-8"))
        table = raw["prototypes"] if "prototypes" in raw else raw
        protos = {k: np.asarray(v, dtype=np.float64) for k, v in table.items()}

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()
    session = ort.InferenceSession(
        str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
    )
    load_ms = (time.perf_counter() - t0) * 1000.0
    inp = session.get_inputs()[0]

    def score_frame(path: Path) -> tuple[float, int]:
        with Image.open(path) as im:
            rgb = im.convert("RGB")
            if spec["kind"] == "obico":
                h, w = int(inp.shape[2]), int(inp.shape[3])
                x = _preprocess_obico(rgb, w, h)
                outs = session.run(None, {inp.name: x})
                confs = np.asarray(outs[1], dtype=np.float64)[0, :, 0]
                return (float(confs.max()) if confs.size else 0.0), 1
            windows = _tile_windows(*rgb.size)
            best = 0.0
            for win in windows:
                x = _preprocess_pg(
                    rgb.crop(win),
                    spec["resize_shortest"],
                    spec["crop_px"],
                    spec["grey"],
                )
                y = session.run(None, {inp.name: x})[0]
                if protos is None:
                    score = float(np.asarray(y, dtype=np.float64).reshape(-1)[0])
                else:
                    emb = np.asarray(y, dtype=np.float64).reshape(-1)
                    ds = float(np.linalg.norm(emb - protos["success"]))
                    df = float(np.linalg.norm(emb - protos["failure"]))
                    score = 0.5 * (1.0 + math.tanh((ds - df) * (ds + df) / 2.0))
                if score > best:
                    best = score
            return best, len(windows)

    real_frames = sorted(
        p for p in frames_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    if not real_frames:
        raise RuntimeError(f"no frames found in {frames_dir}")

    # Warm up one frame before timing.
    score_frame(real_frames[0])

    latencies = []
    scores = []
    tiles_per_frame = 1
    for frame in real_frames:
        t1 = time.perf_counter()
        score, tiles_per_frame = score_frame(frame)
        latencies.append((time.perf_counter() - t1) * 1000.0)
        scores.append((frame.name, round(score, 6)))

    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "candidate": name,
        "threads": threads,
        "size_bytes": model_path.stat().st_size,
        "load_ms": round(load_ms, 2),
        "tiles_per_frame": tiles_per_frame,
        "latency_mean_ms": round(statistics.mean(latencies), 2),
        "latency_median_ms": round(statistics.median(latencies), 2),
        "rss_delta_mib": round((rss_after - rss_before) / 1024.0, 1),
        "scores": scores,
        "max_score": max(s for _, s in scores),
        "mean_score": round(statistics.mean(s for _, s in scores), 6),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models-dir", type=Path, required=True, help="Local directory with cached ONNX models")
    parser.add_argument("--frames-dir", type=Path, default=REAL_FRAMES_DIR, help="Directory of frames to score")
    parser.add_argument("--threads", default="1,4", help="Comma-separated thread counts (default: 1,4)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a summary table")
    parser.add_argument("--_worker", nargs=2, metavar=("CANDIDATE", "THREADS"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args._worker:
        row = run_worker(args.models_dir, args._worker[0], int(args._worker[1]), args.frames_dir)
        print(json.dumps(row))
        return 0

    import onnxruntime as ort

    thread_counts = [int(x.strip()) for x in args.threads.split(",") if x.strip()]
    results = []
    for name, spec in CANDIDATES.items():
        if not (args.models_dir / spec["file"]).is_file():
            continue
        if spec["kind"] == "printguard_proto" and not (args.models_dir / spec["prototypes"]).is_file():
            continue
        for t in thread_counts:
            out = subprocess.check_output(
                [
                    sys.executable,
                    __file__,
                    "--models-dir", str(args.models_dir),
                    "--frames-dir", str(args.frames_dir),
                    "--_worker", name, str(t),
                ],
                text=True,
            )
            results.append(json.loads(out))

    payload = {
        "host": {
            "machine": platform.machine(),
            "cpus": os.cpu_count(),
            "onnxruntime": ort.__version__,
            "providers": ort.get_available_providers(),
        },
        "frames_dir": str(args.frames_dir),
        "results": results,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(
        f"Host: {payload['host']['machine']} ({payload['host']['cpus']} CPUs), "
        f"onnxruntime {payload['host']['onnxruntime']}"
    )
    print(f"{'Candidate':<22} {'Thr':>3} {'Size MB':>8} {'Load ms':>8} {'Mean ms':>8} {'RSS MiB':>8} {'MaxScore':>9}")
    for r in results:
        print(
            f"{r['candidate']:<22} {r['threads']:>3d} {r['size_bytes']/1e6:>8.2f} "
            f"{r['load_ms']:>8.1f} {r['latency_mean_ms']:>8.1f} "
            f"{r['rss_delta_mib']:>8.1f} {r['max_score']:>9.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
