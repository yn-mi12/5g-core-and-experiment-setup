#!/usr/bin/env python3
"""
B-log-strategies/lib/collect_raw_loki.py

Collect ALL logs from the open5gs namespace via Loki HTTP API
"""

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from measure_overhead import ResourceTracker, strip_ansi

LIMIT = 5000
QUERY = '{namespace="open5gs",job!~"filter-agent/.*"}'
OUT_FILE = "all_logs.csv"
FIELDNAMES = ["timestamp_ns", "pod", "container", "app", "line"]


def query_range(url: str, query: str, start_ns: int, end_ns: int, limit: int) -> dict:
    params = urllib.parse.urlencode({
        "query":     query,
        "start":     start_ns,
        "end":       end_ns,
        "limit":     limit,
        "direction": "forward",
    })
    req_url = f"{url}/loki/api/v1/query_range?{params}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req_url, timeout=60) as resp:
                return json.load(resp)
        except Exception as e:
            if attempt == 2:
                print(f"  [WARN] Loki request failed: {e}", file=sys.stderr)
                return {}
            time.sleep(2 ** attempt)
    return {}


def streams_to_rows(data: dict) -> list:
    rows = []
    result = data.get("data", {}).get("result", []) or []
    for stream in result:
        labels = stream.get("stream", {}) or {}
        pod       = labels.get("pod", "")
        container = labels.get("container", "")
        app       = labels.get("app", "") or labels.get("app_kubernetes_io_name", "")
        for ts_ns, line in stream.get("values", []):
            rows.append({
                "timestamp_ns": int(ts_ns),
                "pod":          pod,
                "container":    container,
                "app":          app,
                "line":         strip_ansi(line),
            })
    return rows


def _normalize_pod(pod: str) -> str:
    """
    Normalize pod identifier to the actual Kubernetes pod name.
    """
    parts = pod.split("_")
    if len(parts) >= 3:
        last = parts[-1]
        if len(last) == 36 and last.count("-") == 4:
            return "_".join(parts[1:-1])
    return pod


def _dedup_key(row: dict) -> tuple:
    """
    Deduplication key: same pod + same stripped content within the same second.
    """
    return (_normalize_pod(row["pod"]), strip_ansi(row["line"]).strip(), row["timestamp_ns"] // 1_000_000_000)


def collect_paginated(url: str, start_ns: int, end_ns: int) -> list:
    """
    Paginate through Loki results by advancing the start timestamp
    past the last received entry until the window is exhausted.
    """
    all_rows: list = []
    cursor_ns = start_ns
    page = 0

    while cursor_ns < end_ns:
        page += 1
        data = query_range(url, QUERY, cursor_ns, end_ns, LIMIT)
        rows = streams_to_rows(data)
        if not rows:
            break

        seen_keys = {(r["timestamp_ns"], r["pod"], r["line"]) for r in all_rows[-LIMIT:]}
        new_rows = [r for r in rows if (r["timestamp_ns"], r["pod"], r["line"]) not in seen_keys]
        all_rows.extend(new_rows)

        last_ts = max(r["timestamp_ns"] for r in rows)
        if last_ts <= cursor_ns:
            break
        cursor_ns = last_ts + 1

        total = len(all_rows)
        print(f"  [loki] page {page}: +{len(new_rows)} rows (total {total})", flush=True)

        if len(rows) < LIMIT:
            break

    all_rows.sort(key=lambda r: r["timestamp_ns"])

    before_fs = len(all_rows)
    all_rows = [r for r in all_rows if _normalize_pod(r["pod"]) == r["pod"]]
    removed_fs = before_fs - len(all_rows)
    if removed_fs:
        print(f"  [loki] removed {removed_fs} filesystem-path pod duplicates", flush=True)

    seen_content: set = set()
    deduped: list = []
    for row in all_rows:
        k = _dedup_key(row)
        if k not in seen_content:
            seen_content.add(k)
            deduped.append(row)
    if len(deduped) < len(all_rows):
        print(f"  [loki] dedup: removed {len(all_rows) - len(deduped)} "
              f"dual-stream duplicates ({len(deduped)} unique rows remain)", flush=True)
    return deduped


def write_csv(rows: list, out_path: Path):
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  [loki] wrote {len(rows)} rows → {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url",   default="http://127.0.0.1:3100")
    parser.add_argument("--start", type=int, required=True, help="Window start, unix seconds")
    parser.add_argument("--end",   type=int, required=True, help="Window end,   unix seconds")
    parser.add_argument("--out",   required=True)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    start_ns = args.start * 1_000_000_000
    end_ns   = args.end   * 1_000_000_000

    print(f"[collect_raw_loki] window: {args.start} → {args.end} ({args.end - args.start}s)")

    with ResourceTracker() as rt:
        rows = collect_paginated(args.url, start_ns, end_ns)

    csv_path = out_dir / OUT_FILE
    write_csv(rows, csv_path)

    overhead = {
        "rows_collected":    len(rows),
        "output_bytes":      csv_path.stat().st_size,
        "throughput_rows_s": round(len(rows) / rt.wall_s, 1) if rt.wall_s > 0 else 0,
        **rt.to_dict(),
    }
    overhead_path = out_dir / "collection_overhead.json"
    with open(overhead_path, "w") as f:
        json.dump(overhead, f, indent=2)
    print(f"[collect_raw_loki] overhead → {overhead_path}")


if __name__ == "__main__":
    main()
