#!/usr/bin/env python3
"""
B-log-strategies/daemonSet/collect.py

Collect the DaemonSet's filtered output after an experiment window and write:
  filtered.csv  — filtered log events (read by 04-visibility/measure.py)
  metrics.json  — rows_collected, output_bytes, input_bytes, reduction_pct,
                  line_reduction_pct, cpu_s, peak_mem_mb, scan_latency_s
                  (read by analysis/RQ2/load_data.py for RQ2a, RQ2b, RQ2d)
"""

import argparse
import csv
import io
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
LIB_DIR    = SCRIPT_DIR.parent / "lib"
sys.path.insert(0, str(LIB_DIR))
from measure_overhead import strip_ansi, time_linear_scan

LIMIT      = 5000
FIELDNAMES = ["timestamp_ns", "pod", "app", "line"]


# ──────────────────────────────────────────────────────────────────────────────
# Loki collection
# ──────────────────────────────────────────────────────────────────────────────

def query_range(url: str, query: str, start_ns: int, end_ns: int) -> dict:
    params = urllib.parse.urlencode({
        "query":     query,
        "start":     start_ns,
        "end":       end_ns,
        "limit":     LIMIT,
        "direction": "forward",
    })
    req_url = f"{url}/loki/api/v1/query_range?{params}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req_url, timeout=60) as resp:
                return json.load(resp)
        except Exception as exc:
            if attempt == 2:
                print(f"  [WARN] Loki request failed: {exc}", file=sys.stderr)
                return {}
            time.sleep(2 ** attempt)
    return {}


def streams_to_rows(data: dict) -> list:
    rows = []
    result = data.get("data", {}).get("result", []) or []
    for stream in result:
        labels = stream.get("stream", {}) or {}
        pod = labels.get("pod", "")
        app = labels.get("app", "")
        for ts_ns, line in stream.get("values", []):
            rows.append({
                "timestamp_ns": int(ts_ns),
                "pod":          pod,
                "app":          app,
                "line":         strip_ansi(line),
            })
    return rows


def collect_paginated(url: str, query: str, start_ns: int, end_ns: int) -> list:
    all_rows: list = []
    cursor_ns = start_ns
    page = 0

    while cursor_ns < end_ns:
        page += 1
        data = query_range(url, query, cursor_ns, end_ns)
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

        print(f"  [collect] page {page}: +{len(new_rows)} rows (total {len(all_rows)})",
              flush=True)

        if len(rows) < LIMIT:
            break

    all_rows.sort(key=lambda r: r["timestamp_ns"])
    return all_rows


# ──────────────────────────────────────────────────────────────────────────────
# Prometheus helpers 
# ──────────────────────────────────────────────────────────────────────────────

def _prom_instant(prom_url: str, promql: str, at_s: int) -> float | None:
    """Execute a Prometheus instant query at Unix timestamp at_s."""
    params = urllib.parse.urlencode({"query": promql, "time": at_s})
    req_url = f"{prom_url}/api/v1/query?{params}"
    try:
        with urllib.request.urlopen(req_url, timeout=15) as resp:
            data = json.load(resp)
        result = data.get("data", {}).get("result", [])
        if result:
            return float(result[0]["value"][1])
    except Exception as exc:
        print(f"  [prom] query failed ({promql[:80]}…): {exc}", file=sys.stderr)
    return None


def fetch_agent_overhead(prom_url: str, strategy: str, start: int, end: int) -> dict:
    """
    Query Prometheus for the filter-agent DaemonSet's CPU and peak memory
    during the experiment window [start, end].
    """
    duration = max(end - start, 1)
    pod_re   = f"log-filter-agent-{strategy}-.*"
    ns       = "open5gs"
    sel      = f'namespace="{ns}",container="filter-agent",pod=~"{pod_re}"'

    cpu_q = (
        f"sum(increase(container_cpu_usage_seconds_total{{{sel}}}[{duration}s]))"
    )

    mem_q = (
        f"max(max_over_time(container_memory_working_set_bytes{{{sel}}}[{duration}s]))"
    )

    out: dict = {}

    cpu_val = _prom_instant(prom_url, cpu_q, end)
    if cpu_val is not None:
        out["cpu_s"] = round(max(cpu_val, 0.0), 3)

    mem_val = _prom_instant(prom_url, mem_q, end)
    if mem_val is not None:
        out["peak_mem_mb"] = round(mem_val / (1024 ** 2), 1)

    return out


# ──────────────────────────────────────────────────────────────────────────────
# Storage reduction metrics
# ──────────────────────────────────────────────────────────────────────────────

def compute_storage_reduction(raw_csv: str, out_bytes: int, rows_kept: int) -> dict:
    """
    Compare the filtered CSV against the ground-truth raw CSV to produce
    storage reduction metrics with the same field names used by the static
    apply.py scripts 
    """
    raw_path = Path(raw_csv)
    if not raw_path.exists():
        print(f"  [collect] WARNING: --raw-csv not found: {raw_path}", file=sys.stderr)
        return {}

    in_bytes = raw_path.stat().st_size
    with open(raw_path, newline="", encoding="utf-8", errors="replace") as f:
        in_lines = sum(1 for _ in f) - 1   

    if in_lines <= 0:
        print(f"  [collect] WARNING: --raw-csv has 0 data rows (Loki collection likely "
              f"failed) — skipping reduction metrics", file=sys.stderr)
        return {}

    red_bytes = round((1.0 - out_bytes / in_bytes) * 100.0, 2) if in_bytes else 0.0
    red_lines = round((1.0 - rows_kept  / in_lines) * 100.0, 2)

    return {
        "input_bytes":        in_bytes,
        "input_lines":        in_lines,
        "reduction_pct":      red_bytes,
        "line_reduction_pct": red_lines,
    }


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# kubectl-based collection 
# ──────────────────────────────────────────────────────────────────────────────

def _kubectl_get_pods(namespace: str, strategy: str) -> list:
    result = subprocess.run(
        ["kubectl", "get", "pods", "-n", namespace,
         "-l", f"app=log-filter-agent,filter-strategy={strategy}",
         "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  [kubectl] pod list failed: {result.stderr.strip()}", file=sys.stderr)
        return []
    return [p for p in result.stdout.splitlines() if p.strip()]


def _kubectl_read_csv(namespace: str, pod: str, remote_path: str = "/data/filtered.csv") -> list:
    """Read a CSV from a pod via kubectl exec and return list of row dicts."""
    result = subprocess.run(
        ["kubectl", "exec", "-n", namespace, pod, "--", "cat", remote_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  [kubectl] exec cat {remote_path} on {pod} failed: "
              f"{result.stderr.strip()}", file=sys.stderr)
        return []
    reader = csv.DictReader(io.StringIO(result.stdout))
    return list(reader)


def _collect_rows_from_pods(namespace: str, strategy: str,
                            remote_path: str, start_ns: int, end_ns: int) -> list:
    """Read a CSV from /remote_path on every filter-agent pod, merge, dedup, sort."""
    pods = _kubectl_get_pods(namespace, strategy)
    if not pods:
        return []
    all_rows: list = []
    seen: set = set()
    for pod in pods:
        rows = _kubectl_read_csv(namespace, pod, remote_path)
        for row in rows:
            try:
                ts = int(row["timestamp_ns"])
            except (KeyError, ValueError):
                continue
            if not (start_ns <= ts <= end_ns):
                continue
            key = (ts, row.get("pod", ""), row.get("line", ""))
            if key in seen:
                continue
            seen.add(key)
            all_rows.append({
                "timestamp_ns": ts,
                "pod":          row.get("pod", ""),
                "app":          row.get("app", ""),
                "line":         row.get("line", ""),
            })
        in_win = sum(1 for r in rows if start_ns <= int(r.get("timestamp_ns", 0)) <= end_ns)
        print(f"  [kubectl] {pod} {remote_path}: {len(rows)} rows ({in_win} in window)")
    all_rows.sort(key=lambda r: r["timestamp_ns"])
    return all_rows


def collect_from_pods(strategy: str, namespace: str,
                      start_ns: int, end_ns: int) -> list:
    """Retrieve filtered CSVs from all filter-agent DaemonSet pods."""
    pods = _kubectl_get_pods(namespace, strategy)
    if not pods:
        print(f"  [kubectl] no filter-agent-{strategy} pods found in namespace {namespace}",
              file=sys.stderr)
        return []

    print(f"  [kubectl] collecting from {len(pods)} pod(s): {pods}")
    return _collect_rows_from_pods(namespace, strategy,
                                   "/data/filtered.csv", start_ns, end_ns)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy",   required=True, choices=["salo", "preproc", "drain"],
                    help="Which stream to collect")
    ap.add_argument("--start",      type=int, required=True,
                    help="Window start (unix seconds)")
    ap.add_argument("--end",        type=int, required=True,
                    help="Window end   (unix seconds)")
    ap.add_argument("--out",        required=True)
    ap.add_argument("--url",        default="http://127.0.0.1:3100",
                    help="Loki base URL (used as fallback if --kubectl-only is not set)")
    ap.add_argument("--namespace",  default="open5gs",
                    help="Kubernetes namespace of the filter-agent DaemonSet")
    ap.add_argument("--scenario",   default="unknown")
    ap.add_argument("--raw-csv",    default=None,
                    help="Ground-truth all_logs.csv; enables storage reduction metrics")
    ap.add_argument("--prom-url",   default=None,
                    help="Prometheus base URL; enables filter-agent CPU/memory metrics")
    ap.add_argument("--loki-only",  action="store_true",
                    help="Skip kubectl collection and query Loki directly")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    start_ns = args.start * 1_000_000_000
    end_ns   = args.end   * 1_000_000_000

    print(f"[collect] strategy={args.strategy}  window={args.start}→{args.end}  "
          f"({args.end - args.start}s)")

    # ── collect from DaemonSet pods via kubectl ──────────────────────
    collection_method = "kubectl"

    if args.loki_only:
        query = f'{{job="filter-agent/{args.strategy}",namespace="open5gs"}}'
        print(f"[collect] Loki query: {query}")
        rows = collect_paginated(args.url, query, start_ns, end_ns)
        collection_method = "loki"
    else:
        rows = collect_from_pods(args.strategy, args.namespace, start_ns, end_ns)
        if not rows:
            print("[collect] kubectl returned 0 rows — falling back to Loki query",
                  file=sys.stderr)
            query = f'{{job="filter-agent/{args.strategy}",namespace="open5gs"}}'
            rows = collect_paginated(args.url, query, start_ns, end_ns)
            collection_method = "loki-fallback"

    if not rows:
        print("[collect] WARNING: 0 rows from both kubectl and Loki. "
              "Check that the filter-agent DaemonSet is running and healthy.",
              file=sys.stderr)

    # ── Write filtered CSV ─────────────────────────────────────────────────────
    csv_path = out_dir / "filtered.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[collect] wrote {len(rows)} rows → {csv_path}  (via {collection_method})")

    out_bytes = csv_path.stat().st_size
    _, scan_latency = time_linear_scan(csv_path)

    # ── Prometheus: filter-agent CPU / memory during the experiment window ─────
    overhead: dict = {}
    prom_note: dict = {}
    if args.prom_url:
        print("[collect] querying Prometheus for filter-agent overhead ...")
        overhead = fetch_agent_overhead(args.prom_url, args.strategy,
                                        args.start, args.end)
        if overhead:
            print(f"  cpu_s={overhead.get('cpu_s', 'n/a')}  "
                  f"peak_mem_mb={overhead.get('peak_mem_mb', 'n/a')}")
        else:
            msg = ("Prometheus query returned no result — pod may have restarted "
                   "or metric not yet scraped")
            print(f"  [WARN] {msg} — cpu_s / peak_mem_mb will be absent",
                  file=sys.stderr)
            prom_note = {"cpu_s_note": msg}

    # ── Storage reduction ──────────────────────────────────────────────────────
    stor: dict = {}
    if args.raw_csv:
        stor = compute_storage_reduction(args.raw_csv, out_bytes, len(rows))
    if stor:
        print(f"  input_bytes={stor['input_bytes']}  "
              f"reduction={stor['reduction_pct']}%  "
              f"line_reduction={stor['line_reduction_pct']}%")

    # ── Write metrics ──────────────────────────────────────────────────────────
    metrics = {
        "strategy":       f"{args.strategy}-stream",
        "scenario":       args.scenario,
        "rows_collected": len(rows),
        "output_bytes":   out_bytes,
        **stor,
        **overhead,
        **prom_note,
        "scan_latency_s": round(scan_latency, 4),
    }

    metrics_path = out_dir / "metrics.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"[collect] metrics → {metrics_path}")


if __name__ == "__main__":
    main()
