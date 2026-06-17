#!/usr/bin/env python3
"""
B-log-strategies/04-visibility/measure.py

Measure retained visibility for each reduction strategy.

"Visibility" = ability to recover fault-related events after applying a strategy.

Four metrics per strategy:

  fault_line_retention_pct
    Fraction of keyword-matched fault lines from the original that survive.
    Works for all scenarios; uses keywords (ERROR, CRITICAL, etc.).

  novelty_retention_pct
    Fraction of template-novelty anomalies that survive reduction.
    A log line is a novelty anomaly when its template was never seen in the
    steady-state baseline.

  fault_window_retention_pct
    Fraction of all log lines from the fault injection window (per timeline.json)
    that survive.  Only available for fault scenarios; set to null otherwise.

  total_retention_pct
    Overall fraction of log lines retained regardless of content.

For lossless strategies (LogShrink, Denum) the filtered_csv is absent; all
metrics are 100% by definition because the compressed artifact preserves
every byte of the original.
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "lib"))
from log_parse import normalize_mongodb
from measure_overhead import strip_ansi
FAULT_LEVEL_RE = re.compile(r'\]\s+(ERROR|CRITICAL|FATAL):', re.IGNORECASE)

OPEN5GS_LOW_LEVEL_RE = re.compile(r'\]\s*(DEBUG|INFO)\s*:', re.IGNORECASE)
FAULT_KW_RE    = re.compile(
    r'\b(error|exception|refused|failed|fatal|oom|killed|crash|abort|'
    r'timeout|reject|unreachable|cannot|unable|denied|panic)\b',
    re.IGNORECASE,
)

_UERANSIM_VIS_RE = re.compile(
    r'^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}\.\d+\]\s+'
    r'\[(?P<component>[^\]]+)\]\s+'
    r'\[(?P<level>\w+)\]\s*'
    r'(?P<message>.*)',
    re.DOTALL,
)

EXCLUDE_APPS = {"beyla"}

GO_LEVEL_RE = re.compile(r'(?:^|\s)level=(\w+)', re.IGNORECASE)

GO_STRICT_LEVEL_RE = re.compile(r'(?:^|\s)level=(error|critical|fatal)\b', re.IGNORECASE)

_VAR_PATS_VIS = [
    re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE),
    re.compile(r'\d+\.\d+\.\d+\.\d+(:\d+)?'),
    re.compile(r'0x[0-9a-fA-F]+'),
    re.compile(r'imsi-\S+'),
    re.compile(r'suci-\S+'),
    re.compile(r'\bsupi-\S+'),
    re.compile(r'\(\.\./[^)]+\)'),
    re.compile(r'\d{2}:\d{2}:\d{2}\.\d+'),   
    re.compile(r'\b\d{2}/\d{2}\b'),           
    re.compile(r'\b\d+\b'),
]


def is_fault_line(line: str) -> bool:
    line = strip_ansi(line)

    lm = GO_LEVEL_RE.search(line)
    if lm and lm.group(1).upper() in ("DEBUG", "INFO"):
        return False

    um = _UERANSIM_VIS_RE.match(line)
    if um:
        ue_level = um.group("level").upper()
        if ue_level in ("ERROR", "CRITICAL", "FATAL"):
            return True
        if ue_level in ("DEBUG", "INFO"):
            return False
        return bool(FAULT_KW_RE.search(um.group("message")))

    if FAULT_LEVEL_RE.search(line):
        return True
    if OPEN5GS_LOW_LEVEL_RE.search(line):
        return False
    return bool(FAULT_KW_RE.search(line))


def is_strict_fault_line(line: str) -> bool:
    """Return True only for lines at severity ERROR, CRITICAL, or FATAL.
    """
    line = strip_ansi(line)

    lm = GO_STRICT_LEVEL_RE.search(line)
    if lm:
        return True

    um = _UERANSIM_VIS_RE.match(line)
    if um:
        return um.group("level").upper() in ("ERROR", "CRITICAL", "FATAL")

    return bool(FAULT_LEVEL_RE.search(line))


def make_template_vis(line: str) -> str:
    t = line
    for pat in _VAR_PATS_VIS:
        t = pat.sub('<*>', t)
    return re.sub(r'(<\*>\s*)+', '<*> ', t).strip()


def build_normal_templates(baseline_rows: list[dict]) -> set[str]:
    """Return the set of templates observed in baseline (steady-state) data."""
    return {make_template_vis(r["line"]) for r in baseline_rows}


def novelty_anomalies(rows: list[dict], normal_templates: set[str]) -> list[dict]:
    """Lines whose template was never seen in normal operation."""
    return [r for r in rows if make_template_vis(r["line"]) not in normal_templates]


def load_csv_rows(csv_path: Path) -> list[dict]:
    """Load corpus rows as {ts_ns, line} dicts, excluding non-corpus apps.
    Deduplicates Loki dual-stream entries.
    """
    rows = []
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            app = row.get("app", "")
            if app in EXCLUDE_APPS:
                continue
            raw  = strip_ansi(row.get("line", ""))
            line = normalize_mongodb(raw.strip()) if app == "mongodb" else raw
            rows.append({
                "ts_ns": int(row.get("timestamp_ns", 0)),
                "line":  line,
            })

    seen: set = set()
    deduped: list = []
    for r in rows:
        k = (r.get("app", ""), r["line"], r["ts_ns"] // 1_000_000_000)
        if k not in seen:
            seen.add(k)
            deduped.append(r)
    if len(deduped) < len(rows):
        print(f"[load_csv] dedup: removed {len(rows) - len(deduped)} "
              f"dual-stream duplicates from {csv_path.name} "
              f"({len(deduped)} unique rows remain)")
    return deduped


def load_timeline(timeline_path: Path) -> dict | None:
    if not timeline_path.exists():
        return None
    with open(timeline_path) as f:
        return json.load(f)


def measure_visibility(
    original_rows: list[dict],
    filtered_rows: list[dict] | None,
    timeline: dict | None,
    normal_templates: set[str] | None = None,
) -> dict:
    """
    Compute visibility metrics.

    If filtered_rows is None (lossless), all original rows are treated as retained.
    If timeline is provided, also compute fault_window_retention_pct.
    If normal_templates is provided, also compute novelty_retention_pct.
    """
    retained = filtered_rows if filtered_rows is not None else original_rows

    in_lines  = len(original_rows)
    out_lines = len(retained)
    in_fault  = sum(1 for r in original_rows if is_fault_line(r["line"]))
    out_fault = sum(1 for r in retained        if is_fault_line(r["line"]))

    in_strict  = sum(1 for r in original_rows if is_strict_fault_line(r["line"]))
    out_strict = sum(1 for r in retained       if is_strict_fault_line(r["line"]))

    in_fault_templates  = {make_template_vis(r["line"]) for r in original_rows if is_fault_line(r["line"])}
    out_fault_templates = {make_template_vis(r["line"]) for r in retained       if is_fault_line(r["line"])}
    retained_templates  = in_fault_templates & out_fault_templates

    total_ret  = (out_lines / in_lines * 100.0) if in_lines else 100.0
    fault_ret  = (out_fault / in_fault * 100.0) if in_fault else 100.0
    strict_ret = (out_strict / in_strict * 100.0) if in_strict else 100.0
    tmpl_ret   = (len(retained_templates) / len(in_fault_templates) * 100.0
                  if in_fault_templates else 100.0)

    total_ret  = min(total_ret,  100.0)
    fault_ret  = min(fault_ret,  100.0)
    strict_ret = min(strict_ret, 100.0)
    tmpl_ret   = min(tmpl_ret,   100.0)

    result: dict = {
        "input_total_lines":               in_lines,
        "output_total_lines":              out_lines,
        "input_fault_lines":               in_fault,
        "output_fault_lines":              out_fault,
        "input_strict_fault_lines":        in_strict,
        "output_strict_fault_lines":       out_strict,
        "input_fault_templates":           len(in_fault_templates),
        "retained_fault_templates":        len(retained_templates),
        "total_retention_pct":             round(total_ret, 2),
        "fault_line_retention_pct":        round(fault_ret, 2),
        "strict_fault_line_retention_pct": round(strict_ret, 2),
        "fault_visibility_pct":            round(tmpl_ret, 2),
        "novelty_anomaly_count":                  None,
        "novelty_retention_pct":                  None,
        "novelty_false_negative_pct":             None,
        "fault_window_retention_pct":             None,
        "during_fault_line_retention_pct":        None,
        "during_strict_fault_line_retention_pct": None,
        "during_novel_template_retention_pct":    None,
        "during_input_fault_lines":               None,
        "during_output_fault_lines":              None,
        "during_input_novel_fault_templates":     None,
        "during_output_novel_fault_templates":    None,
    }

    if normal_templates is not None:
        in_novel  = novelty_anomalies(original_rows, normal_templates)
        out_novel = novelty_anomalies(retained,       normal_templates)
        n_in  = len(in_novel)
        n_out = len(out_novel)
        if n_in > 0:
            nov_ret = n_out / n_in * 100.0
            result["novelty_anomaly_count"]      = n_in
            result["novelty_retention_pct"]      = round(nov_ret, 2)
            result["novelty_false_negative_pct"] = round(100.0 - nov_ret, 2)
        else:
            result["novelty_anomaly_count"] = 0

    if timeline is not None:
        try:
            fault_start_ns = int(timeline["fault"]["start"]) * 1_000_000_000
            fault_end_ns   = int(timeline["fault"]["end"])   * 1_000_000_000
            pre_end_ns     = int(timeline["pre"]["end"])     * 1_000_000_000

            in_window  = [r for r in original_rows if fault_start_ns <= r["ts_ns"] <= fault_end_ns]
            out_window = [r for r in retained       if fault_start_ns <= r["ts_ns"] <= fault_end_ns]

            if in_window:
                window_ret = len(out_window) / len(in_window) * 100.0
                result["fault_window_retention_pct"]  = round(window_ret, 2)
                result["fault_window_total_lines"]    = len(in_window)
                result["fault_window_retained_lines"] = len(out_window)

            pre_tmpls = {make_template_vis(r["line"])
                         for r in original_rows if r["ts_ns"] <= pre_end_ns}

            in_dur_fault  = [r for r in in_window  if is_fault_line(r["line"])]
            out_dur_fault = [r for r in out_window if is_fault_line(r["line"])]
            result["during_input_fault_lines"]  = len(in_dur_fault)
            result["during_output_fault_lines"] = len(out_dur_fault)
            if in_dur_fault:
                result["during_fault_line_retention_pct"] = round(
                    min(len(out_dur_fault) / len(in_dur_fault) * 100.0, 100.0), 2)

            in_dur_strict  = [r for r in in_window  if is_strict_fault_line(r["line"])]
            out_dur_strict = [r for r in out_window if is_strict_fault_line(r["line"])]
            if in_dur_strict:
                result["during_strict_fault_line_retention_pct"] = round(
                    min(len(out_dur_strict) / len(in_dur_strict) * 100.0, 100.0), 2)

            in_novel_tmpls: set = set()
            for r in in_dur_fault:
                t = make_template_vis(r["line"])
                if t not in pre_tmpls:
                    in_novel_tmpls.add(t)

            out_novel_tmpls: set = set()
            for r in out_dur_fault:
                t = make_template_vis(r["line"])
                if t not in pre_tmpls:
                    out_novel_tmpls.add(t)

            retained_novel = in_novel_tmpls & out_novel_tmpls
            result["during_input_novel_fault_templates"]  = len(in_novel_tmpls)
            result["during_output_novel_fault_templates"] = len(retained_novel)
            if in_novel_tmpls:
                result["during_novel_template_retention_pct"] = round(
                    min(len(retained_novel) / len(in_novel_tmpls) * 100.0, 100.0), 2)

        except (KeyError, TypeError, ValueError):
            pass

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--original",          required=True,
                    help="Path to all_logs.csv (scenario under test)")
    ap.add_argument("--baseline",          default=None,
                    help="Path to steady-state all_logs.csv; enables novelty anomaly detection")
    ap.add_argument("--timeline",          default=None,
                    help="Path to timeline.json for fault-window metric")
    ap.add_argument("--logshrink-dir",      default=None)
    ap.add_argument("--denum-dir",          default=None)
    ap.add_argument("--salo-stream-dir",   default=None)
    ap.add_argument("--preproc-stream-dir", default=None)
    ap.add_argument("--drain-stream-dir",  default=None)
    ap.add_argument("--outdir",             required=True)
    ap.add_argument("--scenario",           default="unknown")
    ap.add_argument("--out-file",           default="visibility_metrics.json",
                    help="Output filename inside --outdir")
    args = ap.parse_args()

    out_dir = Path(args.outdir)
    out_dir.mkdir(parents=True, exist_ok=True)

    original_rows = load_csv_rows(Path(args.original))
    timeline      = load_timeline(Path(args.timeline)) if args.timeline else None

    normal_templates: set[str] | None = None
    if args.baseline:
        baseline_path = Path(args.baseline)
        if baseline_path.exists():
            baseline_rows    = load_csv_rows(baseline_path)
            normal_templates = build_normal_templates(baseline_rows)
            print(f"[visibility] baseline: {len(baseline_rows)} rows, "
                  f"{len(normal_templates)} normal templates")
        else:
            print(f"[visibility] WARNING: baseline not found: {baseline_path}", file=sys.stderr)

    in_fault = sum(1 for r in original_rows if is_fault_line(r["line"]))
    in_novel = (len(novelty_anomalies(original_rows, normal_templates))
                if normal_templates is not None else "n/a")
    print(f"[visibility] original: {len(original_rows)} lines, "
          f"{in_fault} keyword-fault lines, {in_novel} novelty-anomaly lines")

    results: dict = {"scenario": args.scenario, "strategies": {}}

    for strat, strat_dir in [("logshrink", args.logshrink_dir),
                              ("denum",     args.denum_dir)]:
        if strat_dir is None:
            continue
        results["strategies"][strat] = measure_visibility(
            original_rows, None, timeline, normal_templates)

    for strat, strat_dir in [("salo-stream",   args.salo_stream_dir),
                              ("preproc-stream", args.preproc_stream_dir),
                              ("drain-stream",   args.drain_stream_dir)]:
        if strat_dir is None:
            continue
        filtered_csv = Path(strat_dir) / "filtered.csv"
        if not filtered_csv.exists():
            print(f"  [{strat}] filtered.csv not found — skipping")
            continue
        filtered_rows = load_csv_rows(filtered_csv)
        results["strategies"][strat] = measure_visibility(
            original_rows, filtered_rows, timeline, normal_templates)

    for strat, data in results["strategies"].items():
        fv   = data.get("fault_visibility_pct", "?")
        tr   = data.get("total_retention_pct", "?")
        flr  = data.get("fault_line_retention_pct", "?")
        sfr  = data.get("strict_fault_line_retention_pct", "?")
        nov  = data.get("novelty_retention_pct")
        fwr  = data.get("fault_window_retention_pct")
        nov_str   = f"  novelty_ret={nov}%"          if nov is not None else ""
        fwr_str   = f"  fault_window={fwr}%"         if fwr is not None else ""
        dur_str   = f"  dur_fault_line={data.get('during_fault_line_retention_pct')}%" \
                    if data.get("during_fault_line_retention_pct") is not None else ""
        novel_str = f"  dur_novel_tmpl={data.get('during_novel_template_retention_pct')}%" \
                    if data.get("during_novel_template_retention_pct") is not None else ""
        print(f"  [{strat}] fault_visibility={fv}%  total_retention={tr}%"
              f"  fault_line_ret={flr}%  strict_fault_ret={sfr}%{nov_str}{fwr_str}{dur_str}{novel_str}")

    metrics_path = out_dir / args.out_file
    with open(metrics_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"[visibility] → {metrics_path}")


if __name__ == "__main__":
    main()
