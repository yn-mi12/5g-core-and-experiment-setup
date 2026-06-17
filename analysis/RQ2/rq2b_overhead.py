"""
RQ2b — Processing overhead: CPU time, peak memory, query latency, and
CPU throughput for each reduction strategy.

Produces:
  figures/rq2b_cpu_time_mean.png                   mean CPU time across all scenarios (split offline/online)
  figures/rq2b_cpu_time_all_scenarios_offline.png  per-scenario CPU, offline strategies
  figures/rq2b_cpu_time_all_scenarios_online.png   per-scenario CPU, online strategies
  figures/rq2b_peak_memory_offline.png
  figures/rq2b_peak_memory_online.png
  figures/rq2b_latency_throughput_offline.png      query latency + CPU throughput, offline
  figures/rq2b_cpu_per_mb.png
  tables/rq2b_overhead_summary.csv
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
    SCENARIOS,
    FIGURE_DPI, FIGURE_EXT,
    FONT_SIZE_TITLE, FONT_SIZE_LABEL, FONT_SIZE_TICK, FONT_SIZE_LEGEND,
    LOSSLESS_STRATEGIES,
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


def _clamped_errs(vals: list, errs: list) -> list:
    """Clamp lower error bar so it never goes below 0."""
    result = []
    for v, e in zip(vals, errs):
        if np.isnan(v) or np.isnan(e):
            result.append(e)
        else:
            result.append(min(e, v))
    return result


def _split_strategy_fig(height: float = 4.5) -> tuple:
    """Two side-by-side axes: offline (left) | online (right)."""
    fig, (ax_off, ax_on) = plt.subplots(
        1, 2, figsize=(9, height), dpi=FIGURE_DPI,
        gridspec_kw={"width_ratios": [2, 3]},
    )
    ax_off.set_title("Offline (LogShrink, Denum)", fontsize=FONT_SIZE_TICK)
    ax_on.set_title("Online (SALO, Preprocessing, Drain)", fontsize=FONT_SIZE_TICK)
    return fig, ax_off, ax_on


def plot_cpu_time_mean(mean_df: pd.DataFrame, std_df: pd.DataFrame, n_runs: int):
    """CPU time averaged across all scenarios, split offline/online."""
    all_m = mean_df.groupby("strategy").mean(numeric_only=True).reset_index()
    all_s = (std_df.groupby("strategy").mean(numeric_only=True).reset_index()
             if not std_df.empty else pd.DataFrame())

    def _draw(ax, strat_list):
        strats = [s for s in strat_list if s in all_m["strategy"].values]
        if not strats:
            ax.text(0.5, 0.5, "no data", ha="center", va="center", transform=ax.transAxes)
            return

        vals, errs = [], []
        for s in strats:
            row_m = all_m[all_m["strategy"] == s]
            row_s = all_s[all_s["strategy"] == s] if not all_s.empty else pd.DataFrame()
            v = float(row_m["cpu_s"].values[0]) if not row_m.empty else np.nan
            e = float(row_s["cpu_s"].values[0]) if not row_s.empty else np.nan
            vals.append(v)
            errs.append(_ci(e, n_runs))

        if all(np.isnan(v) for v in vals):
            ax.text(0.5, 0.5, "no cpu_s data", ha="center", va="center", transform=ax.transAxes)
            return

        x = np.arange(len(strats))
        bars = ax.bar(x, vals, 0.55,
                      color=[PALETTE[s] for s in strats], alpha=0.85,
                      yerr=errs, capsize=4, error_kw={"elinewidth": 1.2})
        for bar, v in zip(bars, vals):
            if not np.isnan(v):
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.01,
                        f"{v:.2f}s", ha="center", va="bottom", fontsize=FONT_SIZE_TICK - 1)
        ax.set_xticks(x)
        ax.set_xticklabels([STRATEGY_LABELS[s] for s in strats], fontsize=FONT_SIZE_TICK)
        ax.set_ylabel("CPU time (s)", fontsize=FONT_SIZE_LABEL)
        ax.yaxis.grid(True, linestyle="--", alpha=0.5)
        ax.set_axisbelow(True)

    ci_note = f" (n={n_runs}, error bars = 95% CI)" if n_runs > 1 else ""
    fig, ax_off, ax_on = _split_strategy_fig()
    fig.suptitle(f"RQ2b — Mean CPU time across all scenarios{ci_note}",
                 fontsize=FONT_SIZE_TITLE, y=1.02)
    _draw(ax_off, OFFLINE_STRATEGY_LIST)
    _draw(ax_on, ONLINE_STRATEGY_LIST)
    fig.tight_layout()
    out = FIGURES_DIR / f"rq2b_cpu_time_mean.{FIGURE_EXT}"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  [rq2b] Saved {out}")


def plot_peak_memory(mean_df: pd.DataFrame, std_df: pd.DataFrame, n_runs: int):
    pivot_m = mean_df.pivot_table(index="scenario", columns="strategy",
                                  values="peak_mem_mb", aggfunc="mean")
    pivot_s = std_df.pivot_table(index="scenario", columns="strategy",
                                 values="peak_mem_mb", aggfunc="mean") \
              if not std_df.empty else pd.DataFrame()
    pivot_m = pivot_m.reindex(index=SCENARIOS)

    present_scen = [s for s in SCENARIOS if s in pivot_m.index
                    and not pivot_m.loc[s].isna().all()]
    if not present_scen:
        print("  [rq2b] No peak_mem_mb data — skipping.")
        return

    def _draw_scen(ax, strat_list, scen_list):
        strats = [s for s in strat_list if s in pivot_m.columns]
        n_s = len(strats)
        x = np.arange(len(scen_list))
        width = 0.7 / max(n_s, 1)
        for i, strat in enumerate(strats):
            vals = [float(pivot_m.loc[sc, strat])
                    if sc in pivot_m.index and not pd.isna(pivot_m.loc[sc, strat])
                    else np.nan for sc in scen_list]
            errs = [
                _ci(pivot_s.loc[sc, strat] if sc in pivot_s.index else np.nan, n_runs)
                if (not pivot_s.empty and strat in pivot_s.columns) else 0.0
                for sc in scen_list
            ]
            offset = (i - n_s / 2 + 0.5) * width
            ax.bar(x + offset, vals, width * 0.9,
                   label=STRATEGY_LABELS[strat], color=PALETTE[strat], alpha=0.85,
                   yerr=errs, capsize=3, error_kw={"elinewidth": 1})
        ax.set_xticks(x)
        ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scen_list],
                           fontsize=FONT_SIZE_TICK, rotation=15, ha="right")
        ax.set_ylabel("Peak RSS memory (MiB)", fontsize=FONT_SIZE_LABEL)
        ax.legend(fontsize=FONT_SIZE_LEGEND, loc="upper right")
        ax.yaxis.grid(True, linestyle="--", alpha=0.5)
        ax.set_axisbelow(True)

    for strat_list, suffix, scen_filter in [
        (OFFLINE_STRATEGY_LIST, "offline", ["steady"]),
        (ONLINE_STRATEGY_LIST,  "online",  present_scen),
    ]:
        scen_here = [s for s in scen_filter if s in present_scen]
        if not scen_here:
            continue
        n_s = len([s for s in strat_list if s in pivot_m.columns])
        fig_w = max(4.0, n_s * len(scen_here) * 0.5)
        fig, ax = plt.subplots(figsize=(fig_w, 5), dpi=FIGURE_DPI)
        _draw_scen(ax, strat_list, scen_here)
        fig.tight_layout()
        out = FIGURES_DIR / f"rq2b_peak_memory_{suffix}.{FIGURE_EXT}"
        fig.savefig(out, bbox_inches="tight")
        plt.close(fig)
        print(f"  [rq2b] Saved {out}")


def plot_query_latency_and_throughput(mean_df: pd.DataFrame, std_df: pd.DataFrame, n_runs: int):
    """Query latency and CPU throughput for offline strategies (steady-state)."""
    steady_m = mean_df[mean_df["scenario"] == "steady"].copy()
    steady_s = std_df[std_df["scenario"] == "steady"].copy() if not std_df.empty else pd.DataFrame()
    if steady_m.empty:
        steady_m = mean_df.groupby("strategy").mean(numeric_only=True).reset_index()
        steady_s = std_df.groupby("strategy").mean(numeric_only=True).reset_index() \
                   if not std_df.empty else pd.DataFrame()

    def _vals_errs(strats, col):
        vals, errs = [], []
        for s in strats:
            row_m = steady_m[steady_m["strategy"] == s]
            row_s = steady_s[steady_s["strategy"] == s] if not steady_s.empty else pd.DataFrame()
            v = float(row_m[col].values[0]) if not row_m.empty and col in row_m.columns else np.nan
            e = float(row_s[col].values[0]) if not row_s.empty and col in row_s.columns else np.nan
            vals.append(v)
            errs.append(_ci(e, n_runs))
        return vals, errs

    strats_off = [s for s in OFFLINE_STRATEGY_LIST if s in steady_m["strategy"].values]
    lat_h, lat_e   = _vals_errs(strats_off, "query_latency_s")
    lat_e = _clamped_errs(lat_h, lat_e)
    tput_h, tput_e = _vals_errs(strats_off, "cpu_throughput_mb_s")

    if all(np.isnan(v) for v in lat_h) and all(np.isnan(v) for v in tput_h):
        print("  [rq2b] No query_latency_s or cpu_throughput_mb_s data — skipping.")
        return

    fig, (ax_lat, ax_tput) = plt.subplots(1, 2, figsize=(8, 4), dpi=FIGURE_DPI,
                                           gridspec_kw={"wspace": 0.38})
    x = np.arange(len(strats_off))
    width = 0.5

    for i, (s, v, e) in enumerate(zip(strats_off, lat_h, lat_e)):
        if np.isnan(v):
            continue
        ax_lat.bar(i, v, width, color=PALETTE[s], alpha=0.85,
                   yerr=e, capsize=3, error_kw={"elinewidth": 1})
        ax_lat.text(i, v * 1.12, f"{v:.4f}s",
                    ha="center", va="bottom", fontsize=FONT_SIZE_TICK - 1)
    ax_lat.set_xticks(x)
    ax_lat.set_xticklabels([STRATEGY_LABELS[s] for s in strats_off], fontsize=FONT_SIZE_TICK)
    ax_lat.set_ylabel("Query latency (s)", fontsize=FONT_SIZE_LABEL)
    ax_lat.set_title("Query latency (steady-state)", fontsize=FONT_SIZE_TICK)
    ax_lat.yaxis.grid(True, linestyle="--", alpha=0.5)
    ax_lat.set_axisbelow(True)

    bars = ax_tput.bar(x, tput_h, width,
                       color=[PALETTE[s] for s in strats_off], alpha=0.85,
                       yerr=tput_e, capsize=3, error_kw={"elinewidth": 1})
    for bar, v in zip(bars, tput_h):
        if not np.isnan(v):
            ax_tput.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.01,
                         f"{v:.2f}", ha="center", va="bottom", fontsize=FONT_SIZE_TICK)
    ax_tput.set_xticks(x)
    ax_tput.set_xticklabels([STRATEGY_LABELS[s] for s in strats_off], fontsize=FONT_SIZE_TICK)
    ax_tput.set_ylabel("MB input / CPU-second", fontsize=FONT_SIZE_LABEL)
    ax_tput.set_title("CPU throughput (steady-state)", fontsize=FONT_SIZE_TICK)
    ax_tput.yaxis.grid(True, linestyle="--", alpha=0.5)
    ax_tput.set_axisbelow(True)

    ci_note = f" (n={n_runs}, bars = 95% CI)" if n_runs > 1 else ""
    fig.suptitle(f"RQ2b — Offline strategies: query latency and CPU throughput{ci_note}",
                 fontsize=FONT_SIZE_TITLE, y=1.02)
    fig.tight_layout()
    out = FIGURES_DIR / f"rq2b_latency_throughput_offline.{FIGURE_EXT}"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  [rq2b] Saved {out}")


def plot_cpu_per_mb(mean_df: pd.DataFrame, std_df: pd.DataFrame, n_runs: int):
    """CPU seconds per MB of input (steady-state), split offline/online."""
    steady_m = mean_df[mean_df["scenario"] == "steady"].copy()
    steady_s = std_df[std_df["scenario"] == "steady"].copy() if not std_df.empty else pd.DataFrame()
    if steady_m.empty:
        steady_m = mean_df.groupby("strategy").mean(numeric_only=True).reset_index()
        steady_s = std_df.groupby("strategy").mean(numeric_only=True).reset_index() \
                   if not std_df.empty else pd.DataFrame()
        title_suffix = "(mean across scenarios)"
    else:
        title_suffix = "(steady-state)"

    if "cpu_per_mb" not in steady_m.columns:
        print("  [rq2b] No cpu_per_mb data — skipping.")
        return

    def _draw(ax, strat_list, group_note):
        strats = [s for s in strat_list if s in steady_m["strategy"].values]
        if not strats:
            ax.text(0.5, 0.5, "no data", ha="center", va="center", transform=ax.transAxes)
            return
        vals, errs = [], []
        for s in strats:
            row_m = steady_m[steady_m["strategy"] == s]
            row_s = steady_s[steady_s["strategy"] == s] if not steady_s.empty else pd.DataFrame()
            v = float(row_m["cpu_per_mb"].values[0]) if not row_m.empty else np.nan
            e = float(row_s["cpu_per_mb"].values[0]) if not row_s.empty else np.nan
            vals.append(v)
            errs.append(_ci(e, n_runs))
        if all(np.isnan(v) for v in vals):
            ax.text(0.5, 0.5, "no data", ha="center", va="center", transform=ax.transAxes)
            return
        x = np.arange(len(strats))
        bars = ax.bar(x, vals, 0.55, color=[PALETTE[s] for s in strats], alpha=0.85,
                      yerr=errs, capsize=4, error_kw={"elinewidth": 1.2})
        for bar, v in zip(bars, vals):
            if not np.isnan(v):
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.0005,
                        f"{v:.4f}", ha="center", va="bottom", fontsize=FONT_SIZE_TICK - 1)
        ax.set_xticks(x)
        ax.set_xticklabels([STRATEGY_LABELS[s] for s in strats], fontsize=FONT_SIZE_TICK)
        ax.set_ylabel("CPU time per MB of input (s/MB)", fontsize=FONT_SIZE_LABEL)
        ax.set_title(group_note, fontsize=FONT_SIZE_TICK)
        ax.yaxis.grid(True, linestyle="--", alpha=0.5)
        ax.set_axisbelow(True)

    fig, ax_off, ax_on = _split_strategy_fig(height=4.0)
    _draw(ax_off, OFFLINE_STRATEGY_LIST, "Offline")
    _draw(ax_on,  ONLINE_STRATEGY_LIST,  "Online")
    fig.tight_layout()
    out = FIGURES_DIR / f"rq2b_cpu_per_mb.{FIGURE_EXT}"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  [rq2b] Saved {out}")


def plot_cpu_time_all_scenarios(mean_df: pd.DataFrame, std_df: pd.DataFrame, n_runs: int):
    """Grouped bar of CPU time for every scenario — separate files for offline and online."""
    if "cpu_s" not in mean_df.columns:
        print("  [rq2b] No cpu_s data — skipping all-scenario CPU plot.")
        return

    pivot_m = mean_df.pivot_table(index="scenario", columns="strategy",
                                  values="cpu_s", aggfunc="mean")
    pivot_s = std_df.pivot_table(index="scenario", columns="strategy",
                                 values="cpu_s", aggfunc="mean") \
              if not std_df.empty else pd.DataFrame()
    pivot_m = pivot_m.reindex(index=SCENARIOS)

    present_scen = [s for s in SCENARIOS if s in pivot_m.index
                    and not pivot_m.loc[s].isna().all()]
    if not present_scen:
        print("  [rq2b] No data for all-scenario CPU plot — skipping.")
        return

    def _draw(ax, strat_list, scen_list):
        strats = [s for s in strat_list if s in pivot_m.columns]
        n_s = len(strats)
        if not strats:
            ax.text(0.5, 0.5, "no data", ha="center", va="center", transform=ax.transAxes)
            return
        x = np.arange(len(scen_list))
        width = 0.7 / max(n_s, 1)
        for i, strat in enumerate(strats):
            vals = [
                float(pivot_m.loc[sc, strat])
                if sc in pivot_m.index and strat in pivot_m.columns
                   and not pd.isna(pivot_m.loc[sc, strat])
                else np.nan
                for sc in scen_list
            ]
            errs = [
                _ci(
                    float(pivot_s.loc[sc, strat])
                    if (not pivot_s.empty and sc in pivot_s.index
                        and strat in pivot_s.columns
                        and not pd.isna(pivot_s.loc[sc, strat]))
                    else np.nan,
                    n_runs,
                )
                for sc in scen_list
            ]
            errs = _clamped_errs(vals, errs)
            offset = (i - n_s / 2 + 0.5) * width
            ax.bar(x + offset, vals, width * 0.9,
                   label=STRATEGY_LABELS[strat], color=PALETTE[strat], alpha=0.85,
                   yerr=errs, capsize=3, error_kw={"elinewidth": 1})
        ax.set_xticks(x)
        ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scen_list],
                           fontsize=FONT_SIZE_TICK, rotation=15, ha="right")
        ax.set_ylabel("CPU time (s)", fontsize=FONT_SIZE_LABEL)
        ax.legend(fontsize=FONT_SIZE_LEGEND, loc="upper left")
        ax.yaxis.grid(True, linestyle="--", alpha=0.5)
        ax.set_axisbelow(True)

    ci_note = f" (n={n_runs}, error bars = 95% CI)" if n_runs > 1 else ""
    for strat_list, suffix, title in [
        (OFFLINE_STRATEGY_LIST, "offline",
         f"Offline strategies — CPU time per scenario{ci_note}"),
        (ONLINE_STRATEGY_LIST,  "online",
         f"Online strategies — CPU time per scenario{ci_note}"),
    ]:
        strats_here = [s for s in strat_list if s in pivot_m.columns]
        scen_here = [s for s in present_scen
                     if any(not pd.isna(pivot_m.loc[s, st]) for st in strats_here)]
        if not scen_here:
            continue
        fig_w = max(9.0, len(strats_here) * len(scen_here) * 0.5)
        fig, ax = plt.subplots(figsize=(fig_w, 4.5), dpi=FIGURE_DPI)
        ax.set_title(title, fontsize=FONT_SIZE_TITLE)
        _draw(ax, strat_list, scen_here)
        fig.tight_layout()
        out = FIGURES_DIR / f"rq2b_cpu_time_all_scenarios_{suffix}.{FIGURE_EXT}"
        fig.savefig(out, bbox_inches="tight")
        plt.close(fig)
        print(f"  [rq2b] Saved {out}")


def save_summary_table(mean_df: pd.DataFrame):
    cols = ["strategy_label", "strategy", "scenario", "cpu_s", "cpu_per_mb",
            "peak_mem_mb", "decompression_latency_s", "query_latency_s",
            "throughput_mb_s", "log_throughput_mb_s", "cpu_throughput_mb_s"]
    out_df = mean_df[[c for c in cols if c in mean_df.columns]].copy()
    out_df["cpu_measurement"] = out_df["strategy"].map(
        lambda s: "Offline batch (getrusage)" if s in LOSSLESS_STRATEGIES
        else "Live DaemonSet (Prometheus cgroup)"
    )
    out_df = out_df.drop(columns=["strategy"])
    rename = {
        "strategy_label":          "Strategy",
        "scenario":                "Scenario",
        "cpu_measurement":         "CPU measurement method",
        "cpu_s":                   "CPU time (s)",
        "cpu_per_mb":              "CPU time per MB (s/MB)",
        "peak_mem_mb":             "Peak memory (MiB)",
        "decompression_latency_s": "Decompression latency (s)",
        "query_latency_s":         "Query latency (s)",
        "throughput_mb_s":         "Throughput MB/s vs CSV (offline)",
        "log_throughput_mb_s":     "Throughput MB/s vs raw log (offline)",
        "cpu_throughput_mb_s":     "CPU throughput (MB/CPU-s)",
    }
    out_df = out_df.rename(columns={k: v for k, v in rename.items() if k in out_df.columns})
    out_df["Scenario"] = out_df["Scenario"].map(lambda s: SCENARIO_LABELS.get(s, s))
    path = TABLES_DIR / "rq2b_overhead_summary.csv"
    out_df.to_csv(path, index=False, float_format="%.4f")
    print(f"  [rq2b] Saved {path}")


def run():
    print("[rq2b] Loading strategy metrics (multi-run)...")
    mean_df, std_df, n_runs = load_multi_run_metrics()
    if not _check_data(mean_df, "rq2b"):
        return
    if n_runs > 1:
        print(f"  [rq2b] {n_runs} runs found — showing 95% CI error bars.")
    save_summary_table(mean_df)
    plot_cpu_time_mean(mean_df, std_df, n_runs)
    plot_cpu_time_all_scenarios(mean_df, std_df, n_runs)
    plot_peak_memory(mean_df, std_df, n_runs)
    plot_query_latency_and_throughput(mean_df, std_df, n_runs)
    plot_cpu_per_mb(mean_df, std_df, n_runs)
    print("[rq2b] Done.")


if __name__ == "__main__":
    run()
