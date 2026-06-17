#!/usr/bin/env python3
"""
B-log-strategies/daemonSet/export_pretrained.py

Extracts pre-trained data from the offline pipeline runs so the
live filter_agent can load it at startup.
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
B_DIR      = SCRIPT_DIR.parent
LIB_DIR    = B_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))
from log_parse import LOG_RE, MONGO_NORM_RE, UERANSIM_RE, LEVEL_ORDER, make_template, normalize_mongodb
from measure_overhead import strip_ansi

RARITY_THRESHOLD       = 0.05
APRIORI_WINDOW_SECS    = 10
APRIORI_MIN_SUPPORT    = 0.05
APRIORI_MIN_CONFIDENCE = 0.90
WARN_ORD               = LEVEL_ORDER["WARNING"]
EXCLUDE_APPS           = {"beyla"}


# ──────────────────────────────────────────────────────────────────────────────
# Shared CSV loading
# ──────────────────────────────────────────────────────────────────────────────

def load_csv(csv_path: Path) -> list:
    rows = []
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            app = row.get("app", "")
            if app in EXCLUDE_APPS:
                continue
            ts_ns = int(row.get("timestamp_ns", 0))
            raw   = strip_ansi(row.get("line", "")).strip()
            line  = normalize_mongodb(raw) if app == "mongodb" else raw
            m     = LOG_RE.match(line) or MONGO_NORM_RE.match(line) or UERANSIM_RE.match(line)
            if m:
                level = m.group("level").upper()
                tmpl  = make_template(m.group("message"))
            else:
                level = "INFO"
                tmpl  = make_template(line)
            rows.append({
                "ts_ns":  ts_ns,
                "pod":    row.get("pod", ""),
                "app":    row.get("app", ""),
                "level":  level,
                "lev_ord": LEVEL_ORDER.get(level, 1),
                "tmpl":   tmpl,
                "win60":  (ts_ns // 1_000_000_000) // 60,
                "win_a":  (ts_ns // 1_000_000_000) // APRIORI_WINDOW_SECS,
            })
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# SALO: compute rare templates across all scenarios
# ──────────────────────────────────────────────────────────────────────────────

def export_rare_templates(collect_dir: Path, out_path: Path):
    tmpl_windows: dict = defaultdict(set)
    all_windows:  set  = set()

    for csv_path in sorted(collect_dir.glob("*/all_logs.csv")):
        print(f"  [salo] reading {csv_path.parent.name} ...")
        for r in load_csv(csv_path):
            tmpl_windows[r["tmpl"]].add((csv_path.parent.name, r["win60"]))
            all_windows.add((csv_path.parent.name, r["win60"]))

    n = len(all_windows)
    max_w = int(RARITY_THRESHOLD * n)
    rare  = [t for t, wins in tmpl_windows.items() if len(wins) <= max(max_w, 1)]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(rare, f, indent=2)

    print(f"  [salo] {len(rare)}/{len(tmpl_windows)} templates are rare → {out_path}")


# ──────────────────────────────────────────────────────────────────────────────
# Preproc: mine Apriori rules from all scenarios, export as template-string pairs
# ──────────────────────────────────────────────────────────────────────────────

def _apriori(rows: list) -> set:
    """
    Returns set of (cause_tmpl, effect_tmpl) directional rules.

    Direction is established by temporal precedence: a rule a → b is mined
    only when a appears before b within the same Apriori window
    """
    sorted_rows = sorted(rows, key=lambda r: r["ts_ns"])

    window_seqs: dict = defaultdict(list)
    tid_max_lev: dict = defaultdict(int)
    for r in sorted_rows:
        key = (r["app"], r["win_a"])
        seq = window_seqs[key]
        if not seq or seq[-1] != r["tmpl"]:
            seq.append(r["tmpl"])
        if r["lev_ord"] > tid_max_lev[r["tmpl"]]:
            tid_max_lev[r["tmpl"]] = r["lev_ord"]

    n = len(window_seqs)
    if n < 5:
        return set()

    single:  dict = defaultdict(int)
    ordered: dict = defaultdict(int)
    for seq in window_seqs.values():
        seen: dict = {}
        deduped = []
        for tmpl in seq:
            if tmpl not in seen:
                seen[tmpl] = len(deduped)
                deduped.append(tmpl)
        for tmpl in deduped:
            single[tmpl] += 1

        for i, a in enumerate(deduped):
            for b in deduped[i + 1:]:
                ordered[(a, b)] += 1

    min_cnt = APRIORI_MIN_SUPPORT * n
    rules: set = set()
    for (a, b), cnt in ordered.items():
        if cnt < min_cnt:
            continue
        conf_ab = cnt / single[a] if single[a] else 0
        if conf_ab >= APRIORI_MIN_CONFIDENCE and tid_max_lev.get(b, 0) < WARN_ORD:
            rules.add((a, b))
    return rules


def export_static_rules(collect_dir: Path, out_path: Path):
    all_rows = []
    for csv_path in sorted(collect_dir.glob("*/all_logs.csv")):
        print(f"  [preproc] reading {csv_path.parent.name} ...")
        all_rows.extend(load_csv(csv_path))

    rules = _apriori(all_rows)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump([list(r) for r in rules], f, indent=2)

    print(f"  [preproc] {len(rules)} directional rules → {out_path}")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True,
                    help="Path to data/B-log-strategies/")
    ap.add_argument("--out-dir",  default=str(SCRIPT_DIR / "pretrained"),
                    help="Output directory (default: daemonSet/pretrained/)")
    args = ap.parse_args()

    data_dir    = Path(args.data_dir)
    collect_dir = data_dir / "01-collect"
    out_dir     = Path(args.out_dir)

    if not collect_dir.exists():
        sys.exit(f"ERROR: {collect_dir} not found — run 01-collect first")

    print("[export] Extracting SALO rare templates ...")
    export_rare_templates(collect_dir, out_dir / "rare_templates.json")

    print("[export] Mining preprocessing Apriori rules ...")
    export_static_rules(collect_dir, out_dir / "static_rules.json")

    print(f"\n[export] Done.  Load these at agent startup via env vars:")
    print(f"  RARE_TEMPLATES_JSON={out_dir}/rare_templates.json")
    print(f"  STATIC_RULES_JSON={out_dir}/static_rules.json")


if __name__ == "__main__":
    main()
