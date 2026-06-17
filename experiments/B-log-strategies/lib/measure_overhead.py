#!/usr/bin/env python3
"""
B-log-strategies/lib/measure_overhead.py

Utilities for measuring CPU time, peak memory, and query latency.
Used by all strategy apply.py scripts.
"""

import os
import re
import resource
import threading
import time
from pathlib import Path

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfnihlp]')


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def count_lines(path: Path) -> int:
    with open(path, encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


class ResourceTracker:
    """
    Tracks wall time, CPU time, and peak RSS memory for
    the code block it wraps.
    """

    def __init__(self, sample_interval: float = 0.5):
        self.sample_interval = sample_interval
        self.wall_s: float = 0.0
        self.cpu_s: float = 0.0
        self.peak_mem_mb: float = 0.0
        self._peak_rss: int = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample_loop(self):
        while not self._stop.is_set():
            try:
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                if rss > self._peak_rss:
                    self._peak_rss = rss
            except Exception:
                pass
            self._stop.wait(self.sample_interval)

    def __enter__(self):
        self._t0_wall = time.perf_counter()
        self._ru0          = resource.getrusage(resource.RUSAGE_SELF)
        self._ru0_children = resource.getrusage(resource.RUSAGE_CHILDREN)
        self._peak_rss = self._ru0.ru_maxrss
        self._stop.clear()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(timeout=2)
        t1_wall = time.perf_counter()
        ru1          = resource.getrusage(resource.RUSAGE_SELF)
        ru1_children = resource.getrusage(resource.RUSAGE_CHILDREN)

        self.wall_s = t1_wall - self._t0_wall

        self_cpu = (
            (ru1.ru_utime - self._ru0.ru_utime) +
            (ru1.ru_stime - self._ru0.ru_stime)
        )

        children_cpu = (
            (ru1_children.ru_utime - self._ru0_children.ru_utime) +
            (ru1_children.ru_stime - self._ru0_children.ru_stime)
        )
        self.cpu_s = self_cpu + children_cpu

        scale = 1024 if os.uname().sysname == "Linux" else 1
        self.peak_mem_mb = self._peak_rss * scale / (1024 ** 2)

        child_rss_delta = ru1_children.ru_maxrss - self._ru0_children.ru_maxrss
        if child_rss_delta > 0:
            child_mem_mb = child_rss_delta * scale / (1024 ** 2)
            self.peak_mem_mb = max(self.peak_mem_mb, child_mem_mb)

    def to_dict(self) -> dict:
        return {
            "wall_s":      round(self.wall_s, 3),
            "cpu_s":       round(self.cpu_s, 3),
            "peak_mem_mb": round(self.peak_mem_mb, 1),
        }


def time_linear_scan(path: Path, pattern: str = "ERROR") -> tuple[int, float]:
    """
    Scan a text/CSV file line-by-line for pattern. Simulates a typical
    observability query (find all fault events) on the output of a strategy.

    Returns (match_count, elapsed_seconds).
    """
    t0 = time.perf_counter()
    count = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if pattern in line:
                count += 1
    return count, time.perf_counter() - t0
