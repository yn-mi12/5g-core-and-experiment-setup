#!/usr/bin/env python3
"""
B-log-strategies/03-denum/apply.py

Apply Denum compression to a Loki CSV log file and write:
  <scenario>/compressed/  — compressed archive (read by 04-visibility/measure.py)
  <scenario>/metrics.json — input_lines, input_log_bytes, input_csv_bytes,
                            output_bytes, compression_ratio, reduction_pct,
                            log_reduction_pct, corpus_coverage_pct,
                            wall_s, cpu_s, peak_mem_mb,
                            scan_latency_s, decompression_latency_s, total_query_latency_s
                            (read by analysis/RQ2/load_data.py for RQ2a, RQ2b, RQ2d)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
import tempfile

SCRIPT_DIR = Path(__file__).parent
B_DIR      = SCRIPT_DIR.parent
LIB_DIR    = B_DIR / "lib"
DENUM_PKG  = B_DIR / "cloned_repos" / "Denum" / "Denum_python_package"

sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(DENUM_PKG))

from measure_overhead import ResourceTracker, time_linear_scan, count_lines, dir_bytes
import Denum_simplel as Denum

LOGNAME    = "Open5GS"
CHUNK_SIZE = 100_000


def time_denum_decompress(compressed_dir: Path) -> float:
    """Time kernel decompression (xz + bz2 tar extraction) of the compressed output."""
    xz_files  = list(compressed_dir.rglob("temp.tar.xz"))
    bz2_files = list(compressed_dir.rglob("temp.tar.bz2"))
    if not xz_files and not bz2_files:
        return 0.0
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="denum_decomp_") as tmpdir:
        for xz in xz_files:
            with tarfile.open(xz, "r:xz") as tar:
                tar.extractall(tmpdir)
        for bz2 in bz2_files:
            with tarfile.open(bz2, "r:bz2") as tar:
                tar.extractall(tmpdir)
    return time.perf_counter() - t0


def run_denum(log_path: Path, out_dir: Path) -> int:
    """
    Compress log_path with Denum and copy all output files to out_dir/compressed/.
    Returns total bytes of the compressed directory.
    """
    with open(log_path, "r", encoding="ISO-8859-1") as f:
        lines = f.readlines()

    chunks = [lines[i:i + CHUNK_SIZE] for i in range(0, len(lines), CHUNK_SIZE)]

    with tempfile.TemporaryDirectory(prefix="denum_") as tmp:
        tmp  = Path(tmp)
        work = tmp / "work"
        work.mkdir()

        loader = Denum.dataloader({
            "dataset_name": LOGNAME,
            "input_path":   str(log_path),
        })

        orig_cwd = os.getcwd()
        os.chdir(str(work))
        try:
            for chunk_id, chunk in enumerate(chunks, start=1):
                loader.process_chunk(chunk_id, chunk, LOGNAME)
        finally:
            os.chdir(orig_cwd)

        output_root = tmp / "Output" / LOGNAME
        if not output_root.exists():
            return 0

        compressed_dest = out_dir / "compressed"
        if compressed_dest.exists():
            shutil.rmtree(compressed_dest)
        shutil.copytree(str(output_root), str(compressed_dest))

        total_dir_bytes = dir_bytes(compressed_dest)

    return total_dir_bytes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv",      required=True)
    ap.add_argument("--outdir",   required=True)
    ap.add_argument("--scenario", default="unknown")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    out_dir  = Path(args.outdir)
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_bytes = csv_path.stat().st_size

    log_path = out_dir / f"{LOGNAME}.log"
    subprocess.run(
        [sys.executable, str(LIB_DIR / "extract_lines.py"),
         "--csv", str(csv_path), "--out", str(log_path)],
        check=True,
    )

    n_lines   = count_lines(log_path)
    log_bytes = log_path.stat().st_size

    compressed_dir = out_dir / "compressed"
    if compressed_dir.exists():
        shutil.rmtree(compressed_dir)

    print(f"[denum] input: {n_lines} lines, "
          f"{log_bytes / 1024:.1f} KB (log), {csv_bytes / 1024:.1f} KB (csv)")

    with ResourceTracker() as rt:
        out_bytes = run_denum(log_path, out_dir)

    print(f"  [denum] wall={rt.wall_s:.3f}s  mem={rt.peak_mem_mb:.0f}MB")

    if out_bytes == 0:
        print("[denum] WARNING: compressed output empty", file=sys.stderr)
        compression_ratio   = float("nan")
        reduction_pct       = float("nan")
        log_reduction_pct   = float("nan")
        corpus_coverage_pct = float("nan")
    else:
        compression_ratio   = log_bytes / out_bytes
        reduction_pct       = (1.0 - out_bytes / csv_bytes) * 100.0
        log_reduction_pct   = (1.0 - out_bytes / log_bytes) * 100.0
        corpus_coverage_pct = log_bytes / csv_bytes * 100.0

    decomp_latency = time_denum_decompress(compressed_dir)

    _, scan_latency = time_linear_scan(log_path)
    throughput_mb_s = round(log_bytes / (1024 ** 2) / rt.wall_s, 3) if rt.wall_s > 0 else None

    metrics = {
        "strategy":               "denum",
        "scenario":               args.scenario,
        "input_lines":            n_lines,
        "input_log_bytes":        log_bytes,
        "input_csv_bytes":        csv_bytes,
        "corpus_coverage_pct":    round(corpus_coverage_pct, 2),
        "output_bytes":           out_bytes,
        "compression_ratio":      round(compression_ratio, 3),
        "reduction_pct":          round(reduction_pct, 2),
        "log_reduction_pct":      round(log_reduction_pct, 2),
        "decompression_required": True,
        "wall_s":                 round(rt.wall_s, 3),
        "cpu_s":                  round(rt.cpu_s, 3),
        "compression_throughput_mb_s": throughput_mb_s,
        "peak_mem_mb":            round(rt.peak_mem_mb, 1),
        "scan_latency_s":         round(scan_latency, 4),
        "decompression_latency_s": round(decomp_latency, 4),
        "total_query_latency_s":  round(decomp_latency + scan_latency, 4),
    }

    metrics_path = out_dir / "metrics.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)

    print(f"[denum] ratio={compression_ratio:.2f}x  "
          f"reduction(csv)={reduction_pct:.1f}%  "
          f"reduction(log)={log_reduction_pct:.1f}%  "
          f"output={out_bytes}B  wall={rt.wall_s:.3f}s  mem={rt.peak_mem_mb:.0f}MB")
    print(f"[denum] metrics → {metrics_path}")


if __name__ == "__main__":
    main()
