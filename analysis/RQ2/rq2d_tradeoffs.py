"""
RQ2d — Trade-off analysis: reduction, overhead, and retained visibility.

Produces:
  figures/rq2d_summary_heatmap_offline.png   offline strategy comparison (LogShrink, Denum)
  figures/rq2d_summary_heatmap_online.png    online strategy comparison (SALO, Preprocessing, Drain)
  tables/rq2d_tradeoff_comparison.csv
"""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    FIGURES_DIR, TABLES_DIR, STRATEGY_LABELS,
    STRATEGIES, FAULT_SCENARIOS,
    FIGURE_DPI, FIGURE_EXT,
    FONT_SIZE_TITLE, FONT_SIZE_TICK,
    OFFLINE_STRATEGY_LIST, ONLINE_STRATEGY_LIST,
)
from load_data import (load_multi_run_metrics, load_multi_run_visibility)

try:
    import seaborn as sns
    _HAS_SEABORN = True
except ImportError:
    _HAS_SEABORN = False


def _check_data(df: pd.DataFrame, label: str) -> bool:
    if df.empty:
        print(f"  [{label}] No data available — skipping.")
        return False
    return True


def _fmt_cell(col: str, v: float) -> str:
    if np.isnan(v):
        return ""
    if col.endswith("_pct"):
        return f"{v:.1f}%"
    if col == "compression_ratio":
        return f"{v:.1f}×"
    if col == "cpu_throughput_mb_s":
        return f"{v:.2f}"
    if col in ("cpu_s", "query_latency_s", "total_query_latency_s"):
        return f"{v:.2f}s"
    if col == "peak_mem_mb":
        return f"{v:.0f}M"
    return f"{v:.2f}"


def _display_round(col: str, series: pd.Series) -> pd.Series:
    if col.endswith("_pct"):
        return series.round(1)
    if col in ("compression_ratio", "cpu_throughput_mb_s",
               "cpu_s", "query_latency_s", "total_query_latency_s"):
        return series.round(2)
    if col == "peak_mem_mb":
        return series.round(0)
    return series.round(2)


def _normalise_col(col: str, series: pd.Series, higher_better: bool,
                   zero_floor: bool = False) -> pd.Series:
    """Normalise a metric to [0, 1] for heatmap colouring (green = good)."""
    vals = series.replace([np.inf, -np.inf], np.nan)
    finite = vals.dropna()
    if finite.empty:
        return pd.Series(np.nan, index=series.index)
    is_pct = col.endswith("_pct")
    if is_pct:
        result = vals / 100.0
    else:
        mn = 0.0 if zero_floor else finite.min()
        mx = finite.max()
        relative_range = (mx - mn) / max(abs(mx), 1e-10)
        if mx == mn or relative_range < 0.05:
            result = pd.Series(0.5, index=series.index)
        elif higher_better:
            result = (vals - mn) / (mx - mn)
        else:
            result = 1.0 - (vals - mn) / (mx - mn)
    if not higher_better and is_pct:
        result = 1.0 - result
    return result


_ZERO_FLOOR_COLS = {"cpu_throughput_mb_s"}

# Visibility columns that are only meaningful for fault scenarios (not steady/bursty).
_FAULT_ONLY_VIS_COLS = {
    "fault_visibility_pct",
    "fault_line_retention_pct",
    "strict_fault_line_retention_pct",
    "novelty_retention_pct",
    "during_fault_line_retention_pct",
    "during_strict_fault_line_retention_pct",
    "during_novel_template_retention_pct",
}


def _render_heatmap(raw: pd.DataFrame, metric_config: list,
                    strat_list: list, title: str, filename: str):
    present_strats = [s for s in strat_list if s in raw.index]
    if not present_strats:
        print(f"  [rq2d] No data for {filename} — skipping.")
        return

    sub_raw = raw.loc[present_strats]
    normalised = sub_raw.copy()
    for col, _, higher_better in metric_config:
        if col not in sub_raw.columns:
            continue
        normalised[col] = _normalise_col(col, _display_round(col, sub_raw[col]),
                                         higher_better,
                                         zero_floor=(col in _ZERO_FLOOR_COLS))

    normalised = normalised.dropna(how="all")
    if normalised.empty:
        print(f"  [rq2d] No data for {filename} — skipping.")
        return

    row_labels   = [STRATEGY_LABELS.get(s, s) for s in normalised.index]
    present_cols = [c for c, _, _ in metric_config if c in normalised.columns]
    col_labels   = [lbl for c, lbl, _ in metric_config if c in present_cols]
    color_matrix = normalised[present_cols].values.astype(float)

    annot_matrix = np.empty(color_matrix.shape, dtype=object)
    for j, col in enumerate(present_cols):
        for i, strat in enumerate(normalised.index):
            v = sub_raw.loc[strat, col] if strat in sub_raw.index else np.nan
            annot_matrix[i, j] = _fmt_cell(col, v)

    fig, ax = plt.subplots(figsize=(max(8, len(col_labels) * 1.3),
                                   max(3, len(row_labels) * 0.9)),
                           dpi=FIGURE_DPI)
    if _HAS_SEABORN:
        sns.heatmap(color_matrix,
                    xticklabels=col_labels, yticklabels=row_labels,
                    annot=annot_matrix, fmt="", cmap="RdYlGn",
                    vmin=0, vmax=1, linewidths=0.5, ax=ax,
                    annot_kws={"size": FONT_SIZE_TICK - 1}, cbar_kws={})
    else:
        im = ax.imshow(color_matrix, cmap="RdYlGn", vmin=0, vmax=1, aspect="auto")
        plt.colorbar(im, ax=ax)
        ax.set_xticks(range(len(col_labels)))
        ax.set_yticks(range(len(row_labels)))
        ax.set_xticklabels(col_labels, fontsize=FONT_SIZE_TICK, rotation=30, ha="right")
        ax.set_yticklabels(row_labels, fontsize=FONT_SIZE_TICK)
        for i in range(len(row_labels)):
            for j in range(len(col_labels)):
                txt = annot_matrix[i, j]
                if txt:
                    ax.text(j, i, txt, ha="center", va="center",
                            fontsize=FONT_SIZE_TICK - 1)

    ax.set_title(title, fontsize=FONT_SIZE_TITLE)
    fig.tight_layout()
    out = FIGURES_DIR / filename
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  [rq2d] Saved {out}")


def plot_summary_heatmap(metrics: pd.DataFrame, vis: pd.DataFrame):
    m_mean = metrics.groupby("strategy").mean(numeric_only=True)
    if not vis.empty:
        v_mean_all = vis.groupby("strategy").mean(numeric_only=True)
        _fault_vis = vis[vis["scenario"].isin(FAULT_SCENARIOS)]
        v_mean_fault = (_fault_vis.groupby("strategy").mean(numeric_only=True)
                        if not _fault_vis.empty else pd.DataFrame())
    else:
        v_mean_all = pd.DataFrame()
        v_mean_fault = pd.DataFrame()

    all_metric_config = [
        ("log_reduction_pct",                "Byte\nreduction (%)",                True),
        ("line_reduction_pct",               "Event\nreduction (%)",               True),
        ("compression_ratio",                "Compression\nratio",                 True),
        ("fault_visibility_pct",             "Fault\ntemplate\nvisibility (%)",    True),
        ("fault_window_retention_pct",       "Fault window\nretention (%)",        True),
        ("fault_line_retention_pct",         "Fault line\nretention\n(liberal %)", True),
        ("strict_fault_line_retention_pct",  "ERROR+\nretention (%)",              True),
        ("total_retention_pct",              "Total\nretention (%)",               True),
        ("novelty_retention_pct",            "Novelty\nretention (%)",             True),
        ("during_novel_template_retention_pct", "Novel fault\ntemplates\n(during %)", True),
        ("cpu_s",                            "CPU\ntime (s)",                      False),
        ("peak_mem_mb",                      "Peak\nmem (MiB)",                    False),
        ("query_latency_s",                  "Query\nlatency (s)",                 False),
        ("cpu_throughput_mb_s",              "CPU\nthroughput\n(MB/CPU-s)",        True),
    ]

    rows = {}
    for strat in STRATEGIES:
        row = {}
        for col, _, _ in all_metric_config:
            if col in m_mean.columns and strat in m_mean.index:
                row[col] = float(m_mean.loc[strat, col])
            else:
                v_src = v_mean_fault if col in _FAULT_ONLY_VIS_COLS else v_mean_all
                if not v_src.empty and col in v_src.columns and strat in v_src.index:
                    row[col] = float(v_src.loc[strat, col])
                else:
                    row[col] = np.nan
        rows[strat] = row

    raw = pd.DataFrame(rows, index=[c for c, _, _ in all_metric_config]).T

    # Offline: lossless strategies retain everything, so visibility metrics are trivially 100%
    _offline_exclude = {
        "line_reduction_pct",
        "fault_visibility_pct",
        "fault_window_retention_pct",
        "fault_line_retention_pct",
        "strict_fault_line_retention_pct",
        "total_retention_pct",
        "novelty_retention_pct",
        "during_novel_template_retention_pct",
    }
    offline_config = [(c, lbl, hb) for c, lbl, hb in all_metric_config
                      if c not in _offline_exclude]
    _render_heatmap(
        raw, offline_config, OFFLINE_STRATEGY_LIST,
        title="RQ2d — Offline strategy comparison (LogShrink, Denum)",
        filename=f"rq2d_summary_heatmap_offline.{FIGURE_EXT}",
    )

    # Online: exclude byte-reduction and offline-only overhead metrics
    _online_exclude = {"log_reduction_pct", "peak_mem_mb", "query_latency_s"}
    online_config = [(c, lbl, hb) for c, lbl, hb in all_metric_config
                     if c not in _online_exclude]
    _render_heatmap(
        raw, online_config, ONLINE_STRATEGY_LIST,
        title="RQ2d — Online strategy comparison (SALO, Preprocessing, Drain)",
        filename=f"rq2d_summary_heatmap_online.{FIGURE_EXT}",
    )


def save_tradeoff_table(metrics: pd.DataFrame, vis: pd.DataFrame):
    m_mean = metrics.groupby("strategy").mean(numeric_only=True).reset_index()
    m_mean["strategy_label"] = m_mean["strategy"].map(STRATEGY_LABELS)

    if not vis.empty:
        v_mean = vis.groupby("strategy").mean(numeric_only=True).reset_index()
        fault_vis = vis[vis["scenario"].isin(FAULT_SCENARIOS)]
        if not fault_vis.empty:
            v_fault = fault_vis.groupby("strategy").mean(numeric_only=True).reset_index()
            fault_cols = ["strategy", "fault_visibility_pct", "fault_window_retention_pct",
                          "fault_line_retention_pct", "strict_fault_line_retention_pct",
                          "novelty_retention_pct"]
            v_fault = v_fault[[c for c in fault_cols if c in v_fault.columns]].rename(
                columns={c: f"{c}_fault_only" for c in fault_cols if c != "strategy"})
            v_mean = v_mean.merge(v_fault, on="strategy", how="left")
        merged = m_mean.merge(v_mean, on="strategy", how="outer", suffixes=("", "_vis"))
        mask = merged["strategy_label"].isna() & merged["strategy"].notna()
        merged.loc[mask, "strategy_label"] = merged.loc[mask, "strategy"].map(
            lambda s: STRATEGY_LABELS.get(s, s))
    else:
        merged = m_mean

    cols = [
        "strategy_label", "reduction_pct", "line_reduction_pct",
        "log_reduction_pct", "compression_ratio",
        "cpu_s", "cpu_throughput_mb_s", "peak_mem_mb", "query_latency_s",
        "fault_visibility_pct", "fault_visibility_pct_fault_only",
        "fault_window_retention_pct", "fault_window_retention_pct_fault_only",
        "fault_line_retention_pct", "fault_line_retention_pct_fault_only",
        "strict_fault_line_retention_pct", "strict_fault_line_retention_pct_fault_only",
        "total_retention_pct", "novelty_retention_pct", "novelty_retention_pct_fault_only",
        "during_fault_line_retention_pct", "during_strict_fault_line_retention_pct",
        "during_novel_template_retention_pct",
    ]
    out_df = merged[[c for c in cols if c in merged.columns]].copy()
    rename = {
        "strategy_label":                              "Strategy",
        "reduction_pct":                               "Byte reduction vs CSV (%)",
        "line_reduction_pct":                          "Event reduction (%) [0 for lossless]",
        "log_reduction_pct":                           "Reduction vs raw log (%)",
        "compression_ratio":                           "Compression ratio (×)",
        "cpu_s":                                       "CPU time (s)",
        "cpu_throughput_mb_s":                         "CPU throughput (MB/CPU-s)",
        "peak_mem_mb":                                 "Peak memory (MiB)",
        "query_latency_s":                             "Query latency (s)",
        "fault_visibility_pct":                        "Fault template visibility (%, all scen)",
        "fault_visibility_pct_fault_only":             "Fault template visibility (%, fault scen only)",
        "fault_window_retention_pct":                  "Fault window retention (%, fault scen)",
        "fault_window_retention_pct_fault_only":       "Fault window retention (%, fault scen only)",
        "fault_line_retention_pct":                    "Fault line retention (liberal %, all scen)",
        "fault_line_retention_pct_fault_only":         "Fault line retention (liberal %, fault scen only)",
        "strict_fault_line_retention_pct":             "ERROR+ line retention (%, all scen)",
        "strict_fault_line_retention_pct_fault_only":  "ERROR+ line retention (%, fault scen only)",
        "total_retention_pct":                         "Total retention (%)",
        "novelty_retention_pct":                       "Novelty retention (%)",
        "novelty_retention_pct_fault_only":            "Novelty retention (%, fault scen only)",
        "during_fault_line_retention_pct":             "During: fault line retention (%)",
        "during_strict_fault_line_retention_pct":      "During: ERROR+ line retention (%)",
        "during_novel_template_retention_pct":         "During: novel fault template retention (%)",
    }
    out_df = out_df.rename(columns={k: v for k, v in rename.items() if k in out_df.columns})
    path = TABLES_DIR / "rq2d_tradeoff_comparison.csv"
    out_df.to_csv(path, index=False, float_format="%.3f")
    print(f"  [rq2d] Saved {path}")


def run():
    print("[rq2d] Loading combined metrics (multi-run)...")
    metrics_mean, _m_std, n_runs = load_multi_run_metrics()
    vis_mean, _v_std, _          = load_multi_run_visibility()

    if metrics_mean.empty and vis_mean.empty:
        print("  [rq2d] No data available — skipping.")
        return
    if n_runs > 1:
        print(f"  [rq2d] {n_runs} runs found — using mean values for trade-off plots.")

    if not metrics_mean.empty and not vis_mean.empty:
        save_tradeoff_table(metrics_mean, vis_mean)
        plot_summary_heatmap(metrics_mean, vis_mean)
    elif not metrics_mean.empty:
        save_tradeoff_table(metrics_mean, pd.DataFrame())
        plot_summary_heatmap(metrics_mean, pd.DataFrame())
    else:
        print("  [rq2d] Only visibility data available — skipping heatmap.")

    print("[rq2d] Done.")


if __name__ == "__main__":
    run()
