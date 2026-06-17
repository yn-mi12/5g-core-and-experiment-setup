"""
Load metrics.json and visibility_metrics.json from B-log-strategies experiment runs.

metrics.json (per strategy × scenario):
  Offline (LogShrink, Denum) — written by 02-logshrink/apply.py and 03-denum/apply.py:
    input_lines, input_log_bytes, input_csv_bytes, output_bytes,
    compression_ratio, reduction_pct, log_reduction_pct, corpus_coverage_pct,
    wall_s, cpu_s, peak_mem_mb,
    scan_latency_s, decompression_latency_s, total_query_latency_s

  Online (SALO, Preprocessing, Drain) — written by daemonSet/collect.py:
    rows_collected, output_bytes, input_bytes, reduction_pct, line_reduction_pct,
    cpu_s, peak_mem_mb, scan_latency_s

visibility_metrics.json (per scenario, all strategies merged):
  Written by 04-visibility/measure.py via 04-visibility/run.sh.
  Contains per-strategy fault retention, novelty, and window metrics.
"""

import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from config import (
    STRATEGIES, STRATEGY_LABELS,
    SCENARIOS,
    LOSSLESS_STRATEGIES,
    STRATEGY_FROM_VIS_KEY,
    strategy_dirs_for_root, visibility_dir_for_root,
    discover_run_dirs,
)


def _read_json(path: Path):
    if not path.exists():
        warnings.warn(f"Missing file: {path}")
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        warnings.warn(f"Failed to read {path}: {e}")
        return {}


def _build_metrics_row(strategy: str, scenario: str, raw: dict) -> dict:
    """Convert a raw metrics.json dict into a normalised row dict."""
    in_bytes = (raw.get("input_csv_bytes") or raw.get("input_bytes")
                or raw.get("input_log_bytes") or np.nan)
    out_bytes = raw.get("output_bytes", np.nan)

    in_lines  = raw.get("input_lines", np.nan)
    out_lines = raw.get("output_lines", raw.get("rows_collected", np.nan))

    if strategy in LOSSLESS_STRATEGIES:
        line_red_pct = 0.0
        if np.isnan(out_lines) and not np.isnan(in_lines):
            out_lines = in_lines
    elif not np.isnan(in_lines) and not np.isnan(out_lines) and in_lines > 0:
        line_red_pct = (1.0 - out_lines / in_lines) * 100.0
    else:
        line_red_pct = np.nan

    comp_ratio = raw.get("compression_ratio") or raw.get("storage_ratio") or np.nan
    if np.isnan(comp_ratio) and not np.isnan(in_bytes) and not np.isnan(out_bytes) and out_bytes > 0:
        comp_ratio = in_bytes / out_bytes

    mb   = in_bytes / (1024 ** 2) if not np.isnan(in_bytes) else np.nan
    wall = raw.get("wall_s", np.nan)

    tput = (mb / wall) if (strategy in LOSSLESS_STRATEGIES
                           and not np.isnan(wall) and wall > 0) else np.nan

    if np.isnan(wall) and not np.isnan(tput) and not np.isnan(mb) and tput > 0:
        wall = round(mb / tput, 3)

    log_tput = raw.get("compression_throughput_mb_s", np.nan)
    if np.isnan(log_tput) and strategy in LOSSLESS_STRATEGIES:
        log_bytes = raw.get("input_log_bytes", np.nan)
        if not np.isnan(log_bytes) and not np.isnan(wall) and wall > 0:
            log_tput = log_bytes / (1024 ** 2) / wall

    raw_cpu = raw.get("cpu_s", np.nan)

    if strategy in LOSSLESS_STRATEGIES:
        log_b = raw.get("input_log_bytes", np.nan)
        cpu_mb = log_b / (1024 ** 2) if not np.isnan(log_b) else mb
    else:
        cpu_mb = mb

    cpu_per_mb = (raw_cpu / cpu_mb) if (
        not np.isnan(raw_cpu) and not np.isnan(cpu_mb) and cpu_mb > 0
    ) else np.nan

    cpu_tput = (mb / raw_cpu) if (
        not np.isnan(raw_cpu) and raw_cpu > 0 and not np.isnan(mb)
    ) else np.nan

    log_red_pct = raw.get("log_reduction_pct", np.nan)
    if np.isnan(log_red_pct) and strategy not in LOSSLESS_STRATEGIES:
        log_red_pct = raw.get("reduction_pct", np.nan)

    return {
        "strategy":               strategy,
        "scenario":               scenario,
        "input_bytes":            in_bytes,
        "input_log_bytes":        raw.get("input_log_bytes", np.nan),
        "output_bytes":           out_bytes,
        "input_lines":            in_lines,
        "output_lines":           out_lines,
        "reduction_pct":          raw.get("reduction_pct", np.nan),
        "log_reduction_pct":      log_red_pct,
        "line_reduction_pct":     line_red_pct,
        "compression_ratio":      comp_ratio,
        "wall_s":                 wall,
        "cpu_s":                  raw_cpu,
        "cpu_per_mb":             cpu_per_mb,
        "peak_mem_mb":            raw.get("peak_mem_mb", np.nan),
        "scan_latency_s":         raw.get("scan_latency_s",
                                          raw.get("query_latency_s", np.nan)),
        "decompression_latency_s": raw.get("decompression_latency_s", np.nan),
        "query_latency_s":        raw.get("total_query_latency_s",
                                          raw.get("scan_latency_s",
                                                  raw.get("query_latency_s", np.nan))),
        "throughput_mb_s":        tput,
        "log_throughput_mb_s":    log_tput,
        "cpu_throughput_mb_s":    cpu_tput,
        "decompression_required": bool(raw.get("decompression_required", False)),
        "corpus_coverage_pct":    raw.get("corpus_coverage_pct", np.nan),
    }

def _load_metrics_from_dirs(dirs: dict) -> pd.DataFrame:
    """Load metrics.json for every strategy × scenario using a custom dir mapping."""
    rows = []
    for strategy in STRATEGIES:
        base = dirs.get(strategy)
        if base is None:
            continue
        for scenario in SCENARIOS:
            path = base / scenario / "metrics.json"
            raw = _read_json(path)
            if not raw:
                continue
            rows.append(_build_metrics_row(strategy, scenario, raw))

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["strategy_label"] = df["strategy"].map(STRATEGY_LABELS)
    return df


def _build_visibility_rows(scenario: str, raw: dict) -> list:
    """Flatten a visibility_metrics.json dict into per-strategy rows."""
    rows = []
    strats = raw.get("strategies", {})
    for vis_key, vis in strats.items():
        strategy = STRATEGY_FROM_VIS_KEY.get(vis_key, vis_key)
        rows.append({
            "strategy":                         strategy,
            "scenario":                         scenario,
            "total_retention_pct":              vis.get("total_retention_pct", np.nan),
            "fault_line_retention_pct":         vis.get("fault_line_retention_pct", np.nan),
            "strict_fault_line_retention_pct":  vis.get("strict_fault_line_retention_pct", np.nan),
            "fault_template_retention_pct":     vis.get("fault_template_retention_pct", np.nan),
            "fault_visibility_pct":             vis.get("fault_visibility_pct", np.nan),
            "novelty_retention_pct":            vis.get("novelty_retention_pct", np.nan),
            "novelty_false_negative_pct":       vis.get("novelty_false_negative_pct", np.nan),
            "fault_window_retention_pct":       vis.get("fault_window_retention_pct", np.nan),
            "fault_window_total_lines":         vis.get("fault_window_total_lines", np.nan),
            "fault_window_retained_lines":      vis.get("fault_window_retained_lines", np.nan),
            "input_total_lines":                vis.get("input_total_lines", np.nan),
            "output_total_lines":               vis.get("output_total_lines", np.nan),
            "input_fault_lines":                vis.get("input_fault_lines", np.nan),
            "output_fault_lines":               vis.get("output_fault_lines", np.nan),
            "input_strict_fault_lines":         vis.get("input_strict_fault_lines", np.nan),
            "output_strict_fault_lines":        vis.get("output_strict_fault_lines", np.nan),
            "novelty_anomaly_count":                  vis.get("novelty_anomaly_count", np.nan),
            "during_fault_line_retention_pct":        vis.get("during_fault_line_retention_pct", np.nan),
            "during_strict_fault_line_retention_pct": vis.get("during_strict_fault_line_retention_pct", np.nan),
            "during_novel_template_retention_pct":    vis.get("during_novel_template_retention_pct", np.nan),
            "during_input_fault_lines":               vis.get("during_input_fault_lines", np.nan),
            "during_output_fault_lines":              vis.get("during_output_fault_lines", np.nan),
            "during_input_novel_fault_templates":     vis.get("during_input_novel_fault_templates", np.nan),
            "during_output_novel_fault_templates":    vis.get("during_output_novel_fault_templates", np.nan),
        })
    return rows


def _load_visibility_from_dir(vis_dir: Path) -> pd.DataFrame:
    """Load visibility_metrics.json for every scenario under a given directory."""
    rows = []
    for scenario in SCENARIOS:
        path = vis_dir / scenario / "visibility_metrics.json"
        raw = _read_json(path)
        if raw:
            rows.extend(_build_visibility_rows(scenario, raw))

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["strategy_label"] = df["strategy"].map(STRATEGY_LABELS)
    return df


def load_multi_run_metrics() -> tuple:
    """Load strategy metrics across all runs. Returns (mean_df, std_df, n_runs)."""
    runs = discover_run_dirs()
    all_dfs = []
    for run_label, data_root in runs:
        dirs = strategy_dirs_for_root(data_root)
        df = _load_metrics_from_dirs(dirs)
        if not df.empty:
            df["run"] = run_label
            all_dfs.append(df)

    if not all_dfs:
        empty = pd.DataFrame()
        return empty, empty, 0

    n_runs = len(all_dfs)
    combined = pd.concat(all_dfs, ignore_index=True)
    grp = combined.groupby(["strategy", "scenario"])

    mean_df = grp.mean(numeric_only=True).reset_index()
    std_df  = grp.std(numeric_only=True).reset_index()

    mean_df["strategy_label"] = mean_df["strategy"].map(STRATEGY_LABELS)
    std_df["strategy_label"]  = std_df["strategy"].map(STRATEGY_LABELS)

    return mean_df, std_df, n_runs


def load_multi_run_visibility() -> tuple:
    """Load visibility metrics across all runs. Returns (mean_df, std_df, n_runs)."""
    runs = discover_run_dirs()
    all_dfs = []
    for run_label, data_root in runs:
        vis_dir = visibility_dir_for_root(data_root)
        df = _load_visibility_from_dir(vis_dir)
        if not df.empty:
            df["run"] = run_label
            all_dfs.append(df)

    if not all_dfs:
        empty = pd.DataFrame()
        return empty, empty, 0

    n_runs = len(all_dfs)
    combined = pd.concat(all_dfs, ignore_index=True)
    grp = combined.groupby(["strategy", "scenario"])

    mean_df = grp.mean(numeric_only=True).reset_index()
    std_df  = grp.std(numeric_only=True).reset_index()

    mean_df["strategy_label"] = mean_df["strategy"].map(STRATEGY_LABELS)
    std_df["strategy_label"]  = std_df["strategy"].map(STRATEGY_LABELS)

    return mean_df, std_df, n_runs


def load_multi_run_combined() -> tuple:
    """Join multi-run strategy metrics with visibility metrics. Returns (mean_df, std_df, n_runs)."""
    m_mean, m_std, n_m = load_multi_run_metrics()
    v_mean, v_std, n_v = load_multi_run_visibility()
    n_runs = max(n_m, n_v)

    def _merge(m, v):
        if m.empty and v.empty:
            return pd.DataFrame()
        if m.empty:
            return v
        if v.empty:
            return m
        merged = m.merge(v, on=["strategy", "scenario"], how="outer",
                         suffixes=("", "_vis"))
        if "strategy_label" not in merged.columns and "strategy_label_vis" in merged.columns:
            merged["strategy_label"] = merged["strategy_label_vis"]
        return merged

    return _merge(m_mean, v_mean), _merge(m_std, v_std), n_runs
