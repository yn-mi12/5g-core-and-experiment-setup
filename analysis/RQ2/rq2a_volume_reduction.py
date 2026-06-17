"""
RQ2a — Log volume reduction per strategy.

Produces:
  figures/rq2a_reduction_pct_by_scenario_offline.png
  figures/rq2a_reduction_pct_by_scenario_online.png
  figures/rq2a_storage_bytes_comparison.png
  figures/rq2a_line_reduction_pct.png
  tables/rq2a_volume_reduction_summary.csv
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
    FIGURES_DIR, TABLES_DIR, PALETTE,
    SCENARIOS, SCENARIO_LABELS, STRATEGY_LABELS,
    FIGURE_DPI, FIGURE_EXT,
    FONT_SIZE_TITLE, FONT_SIZE_LABEL, FONT_SIZE_TICK, FONT_SIZE_LEGEND,
    OFFLINE_STRATEGY_LIST, ONLINE_STRATEGY_LIST,
)
from load_data import load_multi_run_metrics

_CI95 = 1.96


def _check_data(df: pd.DataFrame, label: str) -> bool:
    if df.empty:
        print(f"  [{label}] No data available — skipping.")
        return False
    return True


def _ci(std: float, n: int) -> float:
    if n <= 1 or np.isnan(std):
        return 0.0
    return _CI95 * std / np.sqrt(n)


def _split_fig(n_off: int, n_on: int, height: float = 4.5) -> tuple:
    """Side-by-side figure (offline | online)."""
    w_off = max(3.0, n_off * 1.4)
    w_on  = max(3.0, n_on  * 1.4)
    fig, (ax_off, ax_on) = plt.subplots(
        1, 2, figsize=(w_off + w_on, height), dpi=FIGURE_DPI,
        gridspec_kw={"width_ratios": [w_off, w_on]},
    )
    ax_off.set_title("Offline strategies", fontsize=FONT_SIZE_LABEL, pad=4)
    ax_on.set_title("Online (DaemonSet) strategies", fontsize=FONT_SIZE_LABEL, pad=4)
    return fig, ax_off, ax_on


def plot_reduction_pct(mean_df: pd.DataFrame, std_df: pd.DataFrame, n_runs: int):
    GROUP_METRIC = {
        "offline": "log_reduction_pct",
        "online":  "reduction_pct",
    }
    GROUP_YLABEL = {
        "offline": "Storage reduction vs raw log text (%)",
        "online":  "Storage reduction vs Loki CSV (%)",
    }

    def _build_pivots(metric: str):
        pm = mean_df.pivot_table(index="scenario", columns="strategy",
                                 values=metric, aggfunc="mean").reindex(index=SCENARIOS)
        ps = (std_df.pivot_table(index="scenario", columns="strategy",
                                 values=metric, aggfunc="mean").reindex(index=SCENARIOS)
              if not std_df.empty else pd.DataFrame())
        return pm, ps

    def _draw(ax, strat_list, pivot_m, pivot_s, ylabel, present_scen):
        strats = [s for s in strat_list if s in pivot_m.columns]
        n_s, n_sc = len(strats), len(present_scen)
        x = np.arange(n_sc)
        width = 0.75 / max(n_s, 1)

        for i, strat in enumerate(strats):
            vals = pivot_m.reindex(index=present_scen)[strat].values.astype(float)
            errs = np.array([
                _ci(pivot_s.loc[sc, strat] if sc in pivot_s.index else np.nan, n_runs)
                if (not pivot_s.empty and strat in pivot_s.columns) else 0.0
                for sc in present_scen
            ])
            offset = (i - n_s / 2 + 0.5) * width
            bars = ax.bar(x + offset, vals, width * 0.9,
                          label=STRATEGY_LABELS[strat],
                          color=PALETTE[strat], alpha=0.85,
                          yerr=errs, capsize=3, error_kw={"elinewidth": 1})
            for bar, v in zip(bars, vals):
                if not np.isnan(v):
                    ax.text(bar.get_x() + bar.get_width() / 2,
                            bar.get_height() + 1,
                            f"{v:.1f}%",
                            ha="center", va="bottom",
                            fontsize=FONT_SIZE_TICK - 2, rotation=90)

        ax.set_xticks(x)
        ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in present_scen],
                           fontsize=FONT_SIZE_TICK, rotation=20, ha="right")
        ax.set_ylabel(ylabel, fontsize=FONT_SIZE_LABEL)
        ax.legend(fontsize=FONT_SIZE_LEGEND)
        ax.yaxis.grid(True, linestyle="--", alpha=0.5)
        ax.set_axisbelow(True)
        finite_vals = [v for v in vals if not np.isnan(v)]
        ax.set_ylim(0, max(finite_vals, default=10) * 1.35 if finite_vals else 10)

    for group, strat_list, suffix, group_title in [
        ("offline", OFFLINE_STRATEGY_LIST, "offline", "Offline strategies (LogShrink, Denum)"),
        ("online",  ONLINE_STRATEGY_LIST,  "online",  "Online (DaemonSet) strategies"),
    ]:
        metric = GROUP_METRIC[group]
        ylabel = GROUP_YLABEL[group]
        pivot_m, pivot_s = _build_pivots(metric)

        present_scen = [s for s in SCENARIOS
                        if s in pivot_m.index and not pivot_m.loc[s].isna().all()]
        if not present_scen:
            print(f"  [rq2a] No {metric} data for {group} — skipping.")
            continue

        n_strats = len([s for s in strat_list if s in pivot_m.columns])
        n_sc = len(present_scen)
        fig_w = max(4.0, n_strats * n_sc * 0.65)
        fig, ax = plt.subplots(figsize=(fig_w, 4.5), dpi=FIGURE_DPI)
        ax.set_title(group_title, fontsize=FONT_SIZE_LABEL, pad=4)
        _draw(ax, strat_list, pivot_m, pivot_s, ylabel, present_scen)
        fig.tight_layout()
        out = FIGURES_DIR / f"rq2a_reduction_pct_by_scenario_{suffix}.{FIGURE_EXT}"
        fig.savefig(out, bbox_inches="tight")
        plt.close(fig)
        print(f"  [rq2a] Saved {out}")


def plot_storage_bytes(mean_df: pd.DataFrame, std_df: pd.DataFrame, n_runs: int):
    steady = mean_df[mean_df["scenario"] == "steady"].copy()
    if steady.empty:
        first = mean_df["scenario"].iloc[0]
        steady = mean_df[mean_df["scenario"] == first].copy()
        title_suffix = f"({SCENARIO_LABELS.get(first, first)})"
    else:
        title_suffix = "(steady-state)"

    def _draw(ax, strat_list, use_log_bytes: bool):
        strats = [s for s in strat_list if s in steady["strategy"].values]
        if not strats:
            ax.text(0.5, 0.5, "no data", ha="center", va="center",
                    transform=ax.transAxes)
            return
        x     = np.arange(len(strats))
        width = 0.38

        in_col = "input_log_bytes" if use_log_bytes else "input_bytes"
        in_label = "Input (log text)" if use_log_bytes else "Input (raw CSV)"
        in_vals  = [float(steady[steady["strategy"] == s][in_col].values[0])
                    / 1024 for s in strats]
        out_vals = [float(steady[steady["strategy"] == s]["output_bytes"].values[0])
                    / 1024 for s in strats]

        ax.bar(x - width / 2, in_vals,  width, label=in_label, color="#90A4AE", alpha=0.9)
        bars_out = ax.bar(x + width / 2, out_vals, width, label="Output (after reduction)",
                          color=[PALETTE[s] for s in strats], alpha=0.85)

        for xi, (iv, ov) in enumerate(zip(in_vals, out_vals)):
            ax.text(xi - width / 2, iv + 0.1, f"{iv:.0f}", ha="center",
                    va="bottom", fontsize=FONT_SIZE_TICK - 1)
            ax.text(xi + width / 2, ov + 0.1, f"{ov:.0f}", ha="center",
                    va="bottom", fontsize=FONT_SIZE_TICK - 1)

        ax.set_xticks(x)
        ax.set_xticklabels([STRATEGY_LABELS[s] for s in strats], fontsize=FONT_SIZE_TICK)
        ax.set_ylabel("Storage (KiB)", fontsize=FONT_SIZE_LABEL)
        ax.legend(fontsize=FONT_SIZE_LEGEND)
        ax.yaxis.grid(True, linestyle="--", alpha=0.5)
        ax.set_axisbelow(True)

    fig, ax_off, ax_on = _split_fig(
        len(OFFLINE_STRATEGY_LIST),
        len(ONLINE_STRATEGY_LIST),
        height=4.0,
    )
    _draw(ax_off, OFFLINE_STRATEGY_LIST, use_log_bytes=True)
    _draw(ax_on,  ONLINE_STRATEGY_LIST,  use_log_bytes=False)
    fig.tight_layout()
    out = FIGURES_DIR / f"rq2a_storage_bytes_comparison.{FIGURE_EXT}"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  [rq2a] Saved {out}")


def plot_line_reduction(mean_df: pd.DataFrame, std_df: pd.DataFrame, n_runs: int):
    online_m = mean_df[mean_df["strategy"].isin(ONLINE_STRATEGY_LIST)].copy()
    online_s = std_df[std_df["strategy"].isin(ONLINE_STRATEGY_LIST)].copy() \
               if not std_df.empty else pd.DataFrame()

    if online_m.empty:
        print("  [rq2a] No line_reduction_pct data for online strategies — skipping.")
        return

    present_scen = [s for s in SCENARIOS if s in online_m["scenario"].values]
    pivot_m = online_m.pivot_table(index="scenario", columns="strategy",
                                   values="line_reduction_pct", aggfunc="mean")
    pivot_s = online_s.pivot_table(index="scenario", columns="strategy",
                                   values="line_reduction_pct", aggfunc="mean") \
              if not online_s.empty else pd.DataFrame()

    strats = [s for s in ONLINE_STRATEGY_LIST if s in pivot_m.columns]
    n_s, n_sc = len(strats), len(present_scen)
    x = np.arange(n_sc)
    width = 0.75 / max(n_s, 1)

    fig, ax = plt.subplots(figsize=(max(7, n_sc * 1.1), 4.5), dpi=FIGURE_DPI)

    for i, strat in enumerate(strats):
        vals = [
            float(pivot_m.loc[sc, strat])
            if sc in pivot_m.index and not pd.isna(pivot_m.loc[sc, strat])
            else np.nan
            for sc in present_scen
        ]
        errs = [
            _ci(pivot_s.loc[sc, strat] if sc in pivot_s.index else np.nan, n_runs)
            if (not pivot_s.empty and strat in pivot_s.columns) else 0.0
            for sc in present_scen
        ]
        offset = (i - n_s / 2 + 0.5) * width
        ax.bar(x + offset, vals, width * 0.9,
               label=STRATEGY_LABELS[strat],
               color=PALETTE[strat], alpha=0.85,
               yerr=errs, capsize=3, error_kw={"elinewidth": 1})

    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in present_scen],
                       fontsize=FONT_SIZE_TICK, rotation=20, ha="right")
    ax.set_ylabel("Log-event reduction (%)", fontsize=FONT_SIZE_LABEL)
    ax.legend(fontsize=FONT_SIZE_LEGEND)
    ax.yaxis.grid(True, linestyle="--", alpha=0.5)
    ax.set_axisbelow(True)
    ax.set_ylim(0, 100)

    ax.set_title("Online strategies — log-event reduction (%)", fontsize=FONT_SIZE_TITLE)
    fig.tight_layout()
    out = FIGURES_DIR / f"rq2a_line_reduction_pct.{FIGURE_EXT}"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  [rq2a] Saved {out}")


def save_summary_table(mean_df: pd.DataFrame):
    cols = ["strategy_label", "scenario", "input_bytes", "output_bytes",
            "reduction_pct", "log_reduction_pct", "line_reduction_pct",
            "compression_ratio", "corpus_coverage_pct", "decompression_required"]
    out_df = mean_df[[c for c in cols if c in mean_df.columns]].copy()

    for col in ["input_bytes", "output_bytes"]:
        if col in out_df.columns:
            out_df[col] = (out_df[col] / 1024).round(1)

    rename = {
        "strategy_label":         "Strategy",
        "scenario":               "Scenario",
        "input_bytes":            "Input (KiB)",
        "output_bytes":           "Output (KiB)",
        "reduction_pct":          "Reduction vs CSV (%)",
        "log_reduction_pct":      "Reduction vs raw log (%)",
        "line_reduction_pct":     "Line reduction (%)",
        "compression_ratio":      "Ratio (×) [vs raw log for lossless; vs CSV for lossy]",
        "corpus_coverage_pct":    "Corpus coverage (%)",
        "decompression_required": "Decompression needed",
    }
    out_df = out_df.rename(columns={k: v for k, v in rename.items() if k in out_df.columns})
    out_df["Scenario"] = out_df["Scenario"].map(lambda s: SCENARIO_LABELS.get(s, s))

    path = TABLES_DIR / "rq2a_volume_reduction_summary.csv"
    out_df.to_csv(path, index=False, float_format="%.2f")
    print(f"  [rq2a] Saved {path}")


def run():
    print("[rq2a] Loading strategy metrics (multi-run)...")
    mean_df, std_df, n_runs = load_multi_run_metrics()

    if not _check_data(mean_df, "rq2a"):
        return

    if n_runs > 1:
        print(f"  [rq2a] {n_runs} runs found — showing 95% CI error bars.")

    save_summary_table(mean_df)
    plot_reduction_pct(mean_df, std_df, n_runs)
    plot_storage_bytes(mean_df, std_df, n_runs)
    plot_line_reduction(mean_df, std_df, n_runs)
    print("[rq2a] Done.")


if __name__ == "__main__":
    run()
