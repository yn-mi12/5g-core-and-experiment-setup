#!/usr/bin/env bash
# Full pipeline: start cluster → collect 8 C-fault scenarios → run B-log-strategies experiment → generate figures.
#
# Usage:
#   bash run_all.sh                    # single run, all scenarios (~21 h total)
#   bash run_all.sh --runs 3           # three runs for 95% CI error bars
#   bash run_all.sh --skip-cluster     # skip cluster creation (already running)
#   bash run_all.sh --from-pass 2      # resume experiment from pass 2 (SALO)
#   bash run_all.sh --faults-only      # skip steady/bursty scenarios in all passes
#
# Outputs:
#   data/B-log-strategies/     raw metrics (single run)
#   data/run-NN/               additional runs (--runs N > 1)
#   analysis/RQ2/figures/                  PNG figures
#   analysis/RQ2/tables/                   CSV summary tables

set -euo pipefail
chmod +x "$0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SKIP_CLUSTER=false
EXPERIMENT_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-cluster) SKIP_CLUSTER=true; shift ;;
        --runs)         EXPERIMENT_ARGS+=(--runs "$2"); shift 2 ;;
        --from-pass)    EXPERIMENT_ARGS+=(--from "$2"); shift 2 ;;
        --faults-only)  EXPERIMENT_ARGS+=(--faults-only); shift ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

_banner() { echo ""; echo "════════════════════════════════════════════════════"; echo "  $1"; echo "════════════════════════════════════════════════════"; }

_banner "Step 0 — Ensure git submodules are initialized"
git -C "$SCRIPT_DIR" submodule update --init

if ! $SKIP_CLUSTER; then
    _banner "Step 1 — Start cluster"
    bash "$SCRIPT_DIR/cluster-start.sh"
fi

_banner "Step 2 — Collect C-fault-detection data (required by B Pass 1)"

bash "$SCRIPT_DIR/experiments/C-fault-detection/run_all.sh" --only 2,3,5,6,9,14,15,19

_banner "Step 3 — Run B-log-strategies experiment"
bash "$SCRIPT_DIR/experiments/B-log-strategies/run_all.sh" "${EXPERIMENT_ARGS[@]}"

_banner "Step 4 — Generate analysis figures"
cd "$SCRIPT_DIR/analysis/RQ2"
python3 run_all_analysis.py

echo ""
echo "════════════════════════════════════════════════════"
echo "  Done."
echo "  Figures: $SCRIPT_DIR/analysis/RQ2/figures/"
echo "  Tables:  $SCRIPT_DIR/analysis/RQ2/tables/"
echo "════════════════════════════════════════════════════"
