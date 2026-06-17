"""
RQ2c — Retained fault visibility for each reduction strategy.

Produces:
  figures/rq2c_fault_template_visibility.png
  figures/rq2c_fault_line_retention.png
  figures/rq2c_error_line_retention.png
  figures/rq2c_fault_window_retention.png
  figures/rq2c_novelty_retention.png
  figures/rq2c_during_fault_line_retention.png
  figures/rq2c_during_novel_template_retention.png
  tables/rq2c_visibility_summary.csv
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
    FIGURES_DIR, TABLES_DIR, PALETTE, SCENARIO_LABELS, STRATEGY_LABELS,
    SCENARIOS, FAULT_SCENARIOS,
    FIGURE_DPI, FIGURE_EXT,
    FONT_SIZE_TITLE, FONT_SIZE_LABEL, FONT_SIZE_TICK, FONT_SIZE_LEGEND,
    OFFLINE_STRATEGY_LIST, ONLINE_STRATEGY_LIST,
)
from load_data import load_multi_run_visibility

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


def _online_fault_bar(pivot_m: pd.DataFrame, pivot_s: pd.DataFrame,
                      present_scenarios: list,
                      title: str, ylabel: str, filename: str, n_runs: int):
    """Grouped-bar chart for one fault-retention metric, online strategies only."""
    strats = [s for s in ONLINE_STRATEGY_LIST if s in pivot_m.columns]
    if not strats:
        print(f"  [rq2c] No online data for {filename} — skipping.")
        return

    n_s, n_sc = len(strats), len(present_scenarios)
    x = np.arange(n_sc)
    width = 0.75 / max(n_s, 1)

    fig, ax = plt.subplots(figsize=(max(7, n_sc * 1.1), 4.5), dpi=FIGURE_DPI)
    for i, strat in enumerate(strats):
        vals = [
            float(pivot_m.loc[sc, strat])
            if sc in pivot_m.index and not pd.isna(pivot_m.loc[sc, strat])
            else np.nan
            for sc in present_scenarios
        ]
        errs = [
            _ci(pivot_s.loc[sc, strat]
                if (not pivot_s.empty and sc in pivot_s.index and strat in pivot_s.columns)
                else np.nan, n_runs)
            for sc in present_scenarios
        ]
        errs_neg = [min(e, v)       if not np.isnan(v) else 0 for e, v in zip(errs, vals)]
        errs_pos = [min(e, 100 - v) if not np.isnan(v) else 0 for e, v in zip(errs, vals)]
        offset = (i - n_s / 2 + 0.5) * width
        ax.bar(x + offset, vals, width * 0.9,
               label=STRATEGY_LABELS[strat], color=PALETTE[strat], alpha=0.85,
               yerr=[errs_neg, errs_pos], capsize=3, error_kw={"elinewidth": 1})

    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in present_scenarios],
                       fontsize=FONT_SIZE_TICK, rotation=20, ha="right")
    ax.set_ylabel(ylabel, fontsize=FONT_SIZE_LABEL)
    ax.legend(fontsize=FONT_SIZE_LEGEND - 1)
    ax.yaxis.grid(True, linestyle="--", alpha=0.5)
    ax.set_axisbelow(True)
    ax.set_ylim(0, 115)
    ax.set_title(f"{title} — online strategies (fault scenarios)", fontsize=FONT_SIZE_TITLE)
    fig.tight_layout()
    out = FIGURES_DIR / filename
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  [rq2c] Saved {out}")


def plot_fault_visibility_bar(mean_df: pd.DataFrame, std_df: pd.DataFrame, n_runs: int):
    fault_m = mean_df[mean_df["scenario"].isin(FAULT_SCENARIOS)].copy()
    fault_s = std_df[std_df["scenario"].isin(FAULT_SCENARIOS)].copy() \
              if not std_df.empty else pd.DataFrame()
    if fault_m.empty:
        fault_m = mean_df.copy()
        fault_s = std_df.copy() if not std_df.empty else pd.DataFrame()

    def _pivot(df, col):
        if df.empty or col not in df.columns:
            return pd.DataFrame()
        return df.pivot_table(index="scenario", columns="strategy",
                              values=col, aggfunc="mean")

    pv_vis_m    = _pivot(fault_m, "fault_visibility_pct")
    pv_line_m   = _pivot(fault_m, "fault_line_retention_pct")
    pv_strict_m = _pivot(fault_m, "strict_fault_line_retention_pct")
    pv_vis_s    = _pivot(fault_s, "fault_visibility_pct")
    pv_line_s   = _pivot(fault_s, "fault_line_retention_pct")
    pv_strict_s = _pivot(fault_s, "strict_fault_line_retention_pct")

    present_scen = [s for s in FAULT_SCENARIOS
                    if s in pv_vis_m.index or s in pv_line_m.index]
    if not present_scen:
        present_scen = fault_m["scenario"].unique().tolist()
    if not present_scen:
        print("  [rq2c] No data for fault visibility bar — skipping.")
        return

    if not pv_vis_m.empty:
        _online_fault_bar(pv_vis_m, pv_vis_s, present_scen,
                          "Fault template visibility", "Fault template visibility (%)",
                          f"rq2c_fault_template_visibility.{FIGURE_EXT}", n_runs)
    if not pv_line_m.empty:
        _online_fault_bar(pv_line_m, pv_line_s, present_scen,
                          "Fault line retention (liberal: WARNING + keyword)",
                          "Fault line retention (liberal %)",
                          f"rq2c_fault_line_retention.{FIGURE_EXT}", n_runs)
    if not pv_strict_m.dropna(how="all").empty:
        _online_fault_bar(pv_strict_m, pv_strict_s, present_scen,
                          "ERROR+ line retention", "ERROR+ line retention (%)",
                          f"rq2c_error_line_retention.{FIGURE_EXT}", n_runs)


def plot_fault_window_retention(mean_df: pd.DataFrame, std_df: pd.DataFrame, n_runs: int):
    fault_m = mean_df[
        mean_df["scenario"].isin(FAULT_SCENARIOS) &
        mean_df["strategy"].isin(ONLINE_STRATEGY_LIST)
    ].dropna(subset=["fault_window_retention_pct"])
    fault_s = (std_df[
        std_df["scenario"].isin(FAULT_SCENARIOS) &
        std_df["strategy"].isin(ONLINE_STRATEGY_LIST)
    ] if not std_df.empty else pd.DataFrame())

    if fault_m.empty:
        print("  [rq2c] No fault_window_retention_pct data — skipping.")
        return

    pivot_m = fault_m.pivot_table(index="scenario", columns="strategy",
                                  values="fault_window_retention_pct", aggfunc="mean")
    pivot_s = (fault_s.pivot_table(index="scenario", columns="strategy",
                                   values="fault_window_retention_pct", aggfunc="mean")
               if not fault_s.empty else pd.DataFrame())

    present_scen = [s for s in FAULT_SCENARIOS if s in pivot_m.index]
    pivot_m = pivot_m.reindex(index=present_scen)
    pivot_s = pivot_s.reindex(index=present_scen) if not pivot_s.empty else pivot_s
    if pivot_m.empty:
        return

    strats = [s for s in ONLINE_STRATEGY_LIST if s in pivot_m.columns]
    n_s, n_sc = len(strats), len(present_scen)
    x = np.arange(n_sc)
    width = 0.75 / max(n_s, 1)

    fig, ax = plt.subplots(figsize=(max(7, n_sc * 1.1), 5), dpi=FIGURE_DPI)
    for i, strat in enumerate(strats):
        vals = [float(pivot_m.loc[sc, strat])
                if sc in pivot_m.index and not pd.isna(pivot_m.loc[sc, strat])
                else np.nan for sc in present_scen]
        errs = [
            _ci(pivot_s.loc[sc, strat]
                if (not pivot_s.empty and sc in pivot_s.index and strat in pivot_s.columns)
                else np.nan, n_runs)
            for sc in present_scen
        ]
        errs_neg = [min(e, v)       if not np.isnan(v) else 0 for e, v in zip(errs, vals)]
        errs_pos = [min(e, 100 - v) if not np.isnan(v) else 0 for e, v in zip(errs, vals)]
        offset = (i - n_s / 2 + 0.5) * width
        bars = ax.bar(x + offset, vals, width * 0.9,
                      label=STRATEGY_LABELS[strat], color=PALETTE[strat], alpha=0.85,
                      yerr=[errs_neg, errs_pos], capsize=3, error_kw={"elinewidth": 1})
        for bar, v in zip(bars, vals):
            if not np.isnan(v):
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                        f"{v:.1f}%", ha="center", va="bottom", fontsize=FONT_SIZE_TICK - 2)

    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in present_scen],
                       fontsize=FONT_SIZE_TICK, rotation=15, ha="right")
    ax.set_ylabel("Lines retained from fault window (%)", fontsize=FONT_SIZE_LABEL)
    ax.legend(fontsize=FONT_SIZE_LEGEND)
    ax.yaxis.grid(True, linestyle="--", alpha=0.5)
    ax.set_axisbelow(True)
    ax.set_ylim(0, 115)
    ax.set_title("Retention during fault window — online strategies", fontsize=FONT_SIZE_TITLE)
    fig.tight_layout()
    out = FIGURES_DIR / f"rq2c_fault_window_retention.{FIGURE_EXT}"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  [rq2c] Saved {out}")


def plot_novelty_retention(mean_df: pd.DataFrame, std_df: pd.DataFrame, n_runs: int):
    nov_m = mean_df[
        ~mean_df["scenario"].isin(["steady", "bursty"]) &
        mean_df["strategy"].isin(ONLINE_STRATEGY_LIST)
    ].dropna(subset=["novelty_retention_pct"]).copy()
    nov_s = (std_df[
        ~std_df["scenario"].isin(["steady", "bursty"]) &
        std_df["strategy"].isin(ONLINE_STRATEGY_LIST)
    ].copy() if not std_df.empty else pd.DataFrame())

    if nov_m.empty:
        print("  [rq2c] No novelty_retention_pct data — skipping.")
        return

    pivot_m = nov_m.pivot_table(index="scenario", columns="strategy",
                                values="novelty_retention_pct", aggfunc="mean")
    pivot_s = (nov_s.pivot_table(index="scenario", columns="strategy",
                                 values="novelty_retention_pct", aggfunc="mean")
               if not nov_s.empty else pd.DataFrame())

    scen_order = [s for s in SCENARIOS if s not in ("steady", "bursty") and s in pivot_m.index]
    pivot_m = pivot_m.reindex(index=scen_order)
    pivot_s = pivot_s.reindex(index=scen_order) if not pivot_s.empty else pivot_s
    if pivot_m.empty:
        return

    strats = [s for s in ONLINE_STRATEGY_LIST if s in pivot_m.columns]
    n_s, n_sc = len(strats), len(scen_order)
    x = np.arange(n_sc)
    width = 0.75 / max(n_s, 1)

    fig, ax = plt.subplots(figsize=(max(7, n_sc * 1.1), 5), dpi=FIGURE_DPI)
    for i, strat in enumerate(strats):
        vals = [float(pivot_m.loc[sc, strat])
                if sc in pivot_m.index and not pd.isna(pivot_m.loc[sc, strat])
                else np.nan for sc in scen_order]
        errs = [
            _ci(pivot_s.loc[sc, strat]
                if (not pivot_s.empty and sc in pivot_s.index and strat in pivot_s.columns)
                else np.nan, n_runs)
            for sc in scen_order
        ]
        errs_neg = [min(e, v)       if not np.isnan(v) else 0 for e, v in zip(errs, vals)]
        errs_pos = [min(e, 100 - v) if not np.isnan(v) else 0 for e, v in zip(errs, vals)]
        offset = (i - n_s / 2 + 0.5) * width
        ax.bar(x + offset, vals, width * 0.9,
               label=STRATEGY_LABELS[strat], color=PALETTE[strat], alpha=0.85,
               yerr=[errs_neg, errs_pos], capsize=3, error_kw={"elinewidth": 1})

    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scen_order],
                       fontsize=FONT_SIZE_TICK, rotation=15, ha="right")
    ax.set_ylabel("Novelty anomaly retention (%)", fontsize=FONT_SIZE_LABEL)
    ax.legend(fontsize=FONT_SIZE_LEGEND)
    ax.yaxis.grid(True, linestyle="--", alpha=0.5)
    ax.set_axisbelow(True)
    ax.set_ylim(0, 130)
    ax.set_title("Novelty anomaly retention — online strategies", fontsize=FONT_SIZE_TITLE)
    fig.tight_layout()
    out = FIGURES_DIR / f"rq2c_novelty_retention.{FIGURE_EXT}"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  [rq2c] Saved {out}")


def plot_during_fault_retention(mean_df: pd.DataFrame, std_df: pd.DataFrame, n_runs: int):
    """Retention metrics computed only within the fault injection window."""
    fault_m = mean_df[mean_df["scenario"].isin(FAULT_SCENARIOS)].copy()
    fault_s = std_df[std_df["scenario"].isin(FAULT_SCENARIOS)].copy() \
              if not std_df.empty else pd.DataFrame()

    has_line  = "during_fault_line_retention_pct"    in fault_m.columns
    has_novel = "during_novel_template_retention_pct" in fault_m.columns

    if not has_line and not has_novel:
        print("  [rq2c] No during-window metrics — re-run measure.py to generate them.")
        return

    def _pivot(df, col):
        if df.empty or col not in df.columns:
            return pd.DataFrame()
        return df.pivot_table(index="scenario", columns="strategy",
                              values=col, aggfunc="mean")

    configs = []
    if has_line:
        configs.append(("during_fault_line_retention_pct",
                        "Fault-keyword line retention\n(during window only, %)",
                        f"rq2c_during_fault_line_retention.{FIGURE_EXT}"))
    if has_novel:
        configs.append(("during_novel_template_retention_pct",
                        "Novel fault template retention\n(during window, pre-baseline subtracted, %)",
                        f"rq2c_during_novel_template_retention.{FIGURE_EXT}"))

    present_scen = [s for s in FAULT_SCENARIOS if s in fault_m["scenario"].values]
    for col, ylabel, fname in configs:
        pivot_m = _pivot(fault_m, col)
        pivot_s = _pivot(fault_s, col) if not fault_s.empty else pd.DataFrame()
        if pivot_m.empty:
            continue
        _online_fault_bar(pivot_m, pivot_s, present_scen,
                          ylabel.split("\n")[0], ylabel, fname, n_runs)


def save_summary_table(mean_df: pd.DataFrame):
    cols = [
        "strategy_label", "scenario",
        "total_retention_pct", "fault_line_retention_pct",
        "strict_fault_line_retention_pct",
        "fault_visibility_pct", "novelty_retention_pct",
        "novelty_false_negative_pct", "fault_window_retention_pct",
        "input_fault_lines", "output_fault_lines",
        "input_strict_fault_lines", "output_strict_fault_lines",
        "during_fault_line_retention_pct",
        "during_strict_fault_line_retention_pct",
        "during_novel_template_retention_pct",
        "during_input_fault_lines", "during_output_fault_lines",
        "during_input_novel_fault_templates", "during_output_novel_fault_templates",
    ]
    out_df = mean_df[[c for c in cols if c in mean_df.columns]].copy()
    rename = {
        "strategy_label":                   "Strategy",
        "scenario":                         "Scenario",
        "total_retention_pct":              "Total retention (%)",
        "fault_line_retention_pct":         "Fault line retention (liberal %)",
        "strict_fault_line_retention_pct":  "ERROR+ line retention (%)",
        "fault_visibility_pct":             "Fault template visibility (%)",
        "novelty_retention_pct":            "Novelty retention (%)",
        "novelty_false_negative_pct":       "Novelty false-negative (%)",
        "fault_window_retention_pct":       "Fault window retention (%)",
        "input_fault_lines":                "Input fault lines (liberal)",
        "output_fault_lines":               "Output fault lines (liberal)",
        "input_strict_fault_lines":         "Input ERROR+ lines",
        "output_strict_fault_lines":        "Output ERROR+ lines",
        "during_fault_line_retention_pct":        "During: fault line retention (%)",
        "during_strict_fault_line_retention_pct": "During: ERROR+ line retention (%)",
        "during_novel_template_retention_pct":    "During: novel fault template retention (%)",
        "during_input_fault_lines":               "During: input fault lines",
        "during_output_fault_lines":              "During: output fault lines",
        "during_input_novel_fault_templates":     "During: input novel fault templates",
        "during_output_novel_fault_templates":    "During: output novel fault templates",
    }
    out_df = out_df.rename(columns={k: v for k, v in rename.items() if k in out_df.columns})
    out_df["Scenario"] = out_df["Scenario"].map(lambda s: SCENARIO_LABELS.get(s, s))
    path = TABLES_DIR / "rq2c_visibility_summary.csv"
    out_df.to_csv(path, index=False, float_format="%.2f")
    print(f"  [rq2c] Saved {path}")


def run():
    print("[rq2c] Loading visibility metrics (multi-run)...")
    mean_df, std_df, n_runs = load_multi_run_visibility()
    if not _check_data(mean_df, "rq2c"):
        return
    if n_runs > 1:
        print(f"  [rq2c] {n_runs} runs found — showing 95% CI error bars.")
    save_summary_table(mean_df)
    plot_fault_visibility_bar(mean_df, std_df, n_runs)
    plot_fault_window_retention(mean_df, std_df, n_runs)
    plot_novelty_retention(mean_df, std_df, n_runs)
    plot_during_fault_retention(mean_df, std_df, n_runs)
    print("[rq2c] Done.")


if __name__ == "__main__":
    run()
