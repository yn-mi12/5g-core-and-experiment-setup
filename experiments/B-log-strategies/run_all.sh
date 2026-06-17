#!/usr/bin/env bash
# B-log-strategies/run_all.sh
#
# Five-pass experiment runner. Each pass is an independent scenario execution with its own
# Loki ground truth, so online strategies are never measured against offline raw data.
#
# Data flow to RQ2 analysis (analysis/RQ2/):
#
#   Pass 1 — Collect raw logs from Loki (fault scenarios reuse C-fault data).
#             Apply LogShrink + Denum offline; CPU via getrusage in apply.py.
#             Produces: 02-logshrink/<scenario>/metrics.json  → RQ2a, RQ2b, RQ2d
#                       03-denum/<scenario>/metrics.json      → RQ2a, RQ2b, RQ2d
#                       01-collect/<scenario>/all_logs.csv    → Pass 5 ground truth
#
#   Pass 2 — SALO DaemonSet (keyword + regex filter on live pod logs).
#             CPU via Prometheus container_cpu_usage_seconds_total.
#             Produces: 05-daemonSet/run-salo/salo-stream/<scenario>/metrics.json → RQ2a, RQ2b, RQ2d
#                       05-daemonSet/run-salo/raw/<scenario>/all_logs.csv          → Pass 5 ground truth
#
#   Pass 3 — Log Preprocessing DaemonSet (template dedup + noise filter).
#             Same as Pass 2.
#             Produces: 05-daemonSet/run-preproc/preproc-stream/<scenario>/metrics.json → RQ2a, RQ2b, RQ2d
#                       05-daemonSet/run-preproc/raw/<scenario>/all_logs.csv             → Pass 5 ground truth
#
#   Pass 4 — Drain DaemonSet (online Drain3 log-cluster dedup).
#             Same as Pass 2.
#             Produces: 05-daemonSet/run-drain/drain-stream/<scenario>/metrics.json → RQ2a, RQ2b, RQ2d
#                       05-daemonSet/run-drain/raw/<scenario>/all_logs.csv           → Pass 5 ground truth
#
#   Pass 5 — Visibility comparison (measure.py). Each strategy compared against
#             its own pass ground truth to avoid cross-run bias.
#             Produces: 04-visibility/<scenario>/visibility_metrics.json → RQ2c, RQ2d
#
# Usage:
#   bash run_all.sh                      # full run (~21h; each daemonset pass ~6-8h)
#   bash run_all.sh --from 2             # skip Pass 1, resume from SALO pass
#   bash run_all.sh --from 5             # skip to visibility pass only
#   bash run_all.sh --faults-only        # skip steady/bursty in all passes
#   bash run_all.sh --base-faults-only   # faults only in Pass 1; Passes 2-4 run all scenarios

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/common.sh"

FROM=1
RUNS=1
RUN_START=1
FAULTS_ONLY=false
BASE_FAULTS_ONLY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --from)             FROM="$2"; shift 2 ;;
        --runs)             RUNS="$2"; shift 2 ;;
        --run-start)        RUN_START="$2"; shift 2 ;;
        --faults-only)      FAULTS_ONLY=true; shift ;;
        --base-faults-only) BASE_FAULTS_ONLY=true; shift ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

_ORIGINAL_DATA_DIR="$DATA_DIR"

preflight() {
    local ok=1
    echo "[preflight] checking dependencies..."
    for pkg in pandas numpy pyppmd regex projects textdistance tqdm; do
        if ! python3 -c "import $pkg" 2>/dev/null; then
            echo "  MISSING python package: $pkg  →  pip install -r experiments/B-log-strategies/requirements.txt"
            ok=0
        fi
    done
    if ! command -v g++ >/dev/null 2>&1; then
        echo "  MISSING g++  →  sudo apt install build-essential"
        ok=0
    fi
    if ! command -v docker >/dev/null 2>&1; then
        echo "  MISSING docker  →  install Docker 29+"
        ok=0
    fi
    if [[ "$ok" -eq 0 ]]; then
        echo "[preflight] fix the above and re-run."
        exit 1
    fi
    echo "[preflight] all OK"
}

preflight

git -C "$SCRIPT_DIR" submodule update --init || true

INTER_PASS_SLEEP=120

COLLECT_ARGS=""
DAEMONSET_ARGS=""
if $FAULTS_ONLY; then
    COLLECT_ARGS="--faults-only"
    DAEMONSET_ARGS="--faults-only"
elif $BASE_FAULTS_ONLY; then
    COLLECT_ARGS="--faults-only"
fi

run_pass() {
    local num="$1" name="$2"
    if [[ $num -lt $FROM ]]; then
        echo "[skip] Pass $num — $name"
        return 1
    fi
    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo " Pass $num — $name"
    echo "════════════════════════════════════════════════════════════"
    return 0
}

# ──────────────────────────────────────────────────────────────────────────────
# Multi-run loop
# ──────────────────────────────────────────────────────────────────────────────

for (( _RUN_I=RUN_START; _RUN_I<RUN_START+RUNS; _RUN_I++ )); do
    if [[ $RUNS -gt 1 || $RUN_START -gt 1 ]]; then
        _RUN_TAG="run-$(printf '%02d' "$_RUN_I")"
        export DATA_DIR="$_ORIGINAL_DATA_DIR/$_RUN_TAG"
        export EXPERIMENT_DATA_DIR="$DATA_DIR"
        mkdir -p "$DATA_DIR/B-log-strategies"
        echo ""
        echo "╔══════════════════════════════════════════════════════════╗"
        echo "  Repetition $_RUN_I of $((RUN_START+RUNS-1))  →  $DATA_DIR"
        echo "╚══════════════════════════════════════════════════════════╝"
    else
        export DATA_DIR="$_ORIGINAL_DATA_DIR"
        export EXPERIMENT_DATA_DIR="$DATA_DIR"
    fi

    BASE="$DATA_DIR/B-log-strategies"

    # ──────────────────────────────────────────────────────────────────────────
    # Pass 1 — Raw collection + LogShrink + Denum
    # ──────────────────────────────────────────────────────────────────────────

    if run_pass 1 "Raw collection + LogShrink + Denum"; then
        bash "$SCRIPT_DIR/01-collect/run.sh"   $COLLECT_ARGS
        bash "$SCRIPT_DIR/02-logshrink/run.sh"
        bash "$SCRIPT_DIR/03-denum/run.sh"
        echo ""
        echo "[done] Pass 1 complete. Cooling down ${INTER_PASS_SLEEP}s ..."
        sleep "$INTER_PASS_SLEEP"
    fi

    # ──────────────────────────────────────────────────────────────────────────
    # Pass 2 — SALO
    # ──────────────────────────────────────────────────────────────────────────

    if run_pass 2 "SALO → $BASE/05-daemonSet/run-salo/"; then
        bash "$SCRIPT_DIR/daemonSet/run.sh" \
            --strategy       salo \
            --run-tag        run-salo \
            --skip-visibility \
            $DAEMONSET_ARGS
        echo ""
        echo "[done] Pass 2 complete. Cooling down ${INTER_PASS_SLEEP}s ..."
        sleep "$INTER_PASS_SLEEP"
    fi

    # ──────────────────────────────────────────────────────────────────────────
    # Pass 3 — Preprocessing
    # ──────────────────────────────────────────────────────────────────────────

    if run_pass 3 "Preprocessing → $BASE/05-daemonSet/run-preproc/"; then
        bash "$SCRIPT_DIR/daemonSet/run.sh" \
            --strategy       preproc \
            --run-tag        run-preproc \
            --skip-visibility \
            $DAEMONSET_ARGS
        echo ""
        echo "[done] Pass 3 complete. Cooling down ${INTER_PASS_SLEEP}s ..."
        sleep "$INTER_PASS_SLEEP"
    fi

    # ──────────────────────────────────────────────────────────────────────────
    # Pass 4 — Drain
    # ──────────────────────────────────────────────────────────────────────────

    if run_pass 4 "Drain → $BASE/05-daemonSet/run-drain/"; then
        bash "$SCRIPT_DIR/daemonSet/run.sh" \
            --strategy       drain \
            --run-tag        run-drain \
            --skip-visibility \
            $DAEMONSET_ARGS
        echo ""
        echo "[done] Pass 4 complete. Cooling down ${INTER_PASS_SLEEP}s ..."
        sleep "$INTER_PASS_SLEEP"
    fi

    # ──────────────────────────────────────────────────────────────────────────
    # Pass 5 — Visibility comparison
    # ──────────────────────────────────────────────────────────────────────────

    if run_pass 5 "Visibility comparison"; then
        bash "$SCRIPT_DIR/04-visibility/run.sh" \
            --raw-base            "$BASE/01-collect" \
            --salo-raw-base       "$BASE/05-daemonSet/run-salo/raw" \
            --preproc-raw-base    "$BASE/05-daemonSet/run-preproc/raw" \
            --drain-raw-base      "$BASE/05-daemonSet/run-drain/raw" \
            --logshrink-base      "$BASE/02-logshrink" \
            --denum-base          "$BASE/03-denum" \
            --salo-stream-base    "$BASE/05-daemonSet/run-salo/salo-stream" \
            --preproc-stream-base "$BASE/05-daemonSet/run-preproc/preproc-stream" \
            --drain-stream-base   "$BASE/05-daemonSet/run-drain/drain-stream"
    fi

    if [[ $RUNS -gt 1 || $RUN_START -gt 1 ]]; then
        echo ""
        echo "[multi-run] Repetition $_RUN_I complete."
        [[ $_RUN_I -lt $((RUN_START+RUNS-1)) ]] && sleep "$INTER_PASS_SLEEP"
    fi

done  

echo ""
echo "════════════════════════════════════════════════════════════"
echo " Group B complete (${RUNS} run(s))."
echo "════════════════════════════════════════════════════════════"
