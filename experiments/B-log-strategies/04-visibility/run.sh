#!/usr/bin/env bash
# B-log-strategies/04-visibility/run.sh
#
# Measure fault-event visibility for every strategy × scenario.
#
# Three-pass mode (used by run_all.sh):
#   Each strategy group has its own ground truth from its own experiment run.
#   Triggered automatically when --salo-raw-base, --preproc-raw-base, or
#   --drain-raw-base differ from --raw-base.  measure.py is called once per
#   group; results are merged into a single visibility_metrics.json per scenario.
#
# Usage:
#   bash 04-visibility/run.sh                                   # single-pass defaults
#   bash 04-visibility/run.sh --raw-base <path>                 # override ground truth
#   bash 04-visibility/run.sh \                                 # four-pass
#       --raw-base            $DATA/01-collect                  \
#       --salo-raw-base       $DATA/05-daemonSet/run-salo/raw   \
#       --preproc-raw-base    $DATA/05-daemonSet/run-preproc/raw \
#       --drain-raw-base      $DATA/05-daemonSet/run-drain/raw  \
#       --salo-stream-base    $DATA/05-daemonSet/run-salo/salo-stream    \
#       --preproc-stream-base $DATA/05-daemonSet/run-preproc/preproc-stream \
#       --drain-stream-base   $DATA/05-daemonSet/run-drain/drain-stream

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../../lib/common.sh"
DATA_DIR="${EXPERIMENT_DATA_DIR:-$DATA_DIR}"

BASE="$DATA_DIR/B-log-strategies"

# ── Ground-truth directories per strategy group ───────────────────────────────
RAW_BASE="$BASE/01-collect"
SALO_RAW_BASE=""
PREPROC_RAW_BASE=""
DRAIN_RAW_BASE=""

# ── Strategy output directories ──────────────────────────────────────────────
LOGSHRINK_BASE="$BASE/02-logshrink"
DENUM_BASE="$BASE/03-denum"
SALO_STREAM_BASE="$BASE/05-daemonSet/run-salo/salo-stream"
PREPROC_STREAM_BASE="$BASE/05-daemonSet/run-preproc/preproc-stream"
DRAIN_STREAM_BASE="$BASE/05-daemonSet/run-drain/drain-stream"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --raw-base)            RAW_BASE="$2";            shift 2 ;;
        --salo-raw-base)       SALO_RAW_BASE="$2";       shift 2 ;;
        --preproc-raw-base)    PREPROC_RAW_BASE="$2";    shift 2 ;;
        --drain-raw-base)      DRAIN_RAW_BASE="$2";      shift 2 ;;
        --logshrink-base)      LOGSHRINK_BASE="$2";      shift 2 ;;
        --denum-base)          DENUM_BASE="$2";          shift 2 ;;
        --salo-stream-base)    SALO_STREAM_BASE="$2";    shift 2 ;;
        --preproc-stream-base) PREPROC_STREAM_BASE="$2"; shift 2 ;;
        --drain-stream-base)   DRAIN_STREAM_BASE="$2";   shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

SALO_RAW="${SALO_RAW_BASE:-$RAW_BASE}"
PREPROC_RAW="${PREPROC_RAW_BASE:-$RAW_BASE}"
DRAIN_RAW="${DRAIN_RAW_BASE:-$RAW_BASE}"

THREE_PASS=false
[[ "$SALO_RAW" != "$RAW_BASE" || "$PREPROC_RAW" != "$RAW_BASE" || "$DRAIN_RAW" != "$RAW_BASE" ]] && THREE_PASS=true

SCENARIOS=(steady bursty
           fault-pod-crash-amf fault-memory-pressure-upf fault-network-delay-nrf
           fault-network-partition-amf-scp fault-packet-loss-upf
           fault-upf-infra-packet-loss fault-nrf-cascade fault-udm-pod-crash)

BASELINE_CSV="$RAW_BASE/steady/all_logs.csv"
SALO_BASELINE_CSV="$SALO_RAW/steady/all_logs.csv"
PREPROC_BASELINE_CSV="$PREPROC_RAW/steady/all_logs.csv"
DRAIN_BASELINE_CSV="$DRAIN_RAW/steady/all_logs.csv"

echo ""
echo "============================================================"
echo " B-04: Visibility measurement"
$THREE_PASS && echo " Mode: multi-pass (separate ground truths per strategy group)"
echo "============================================================"

# ── Helpers ───────────────────────────────────────────────────────────────────

measure_group() {
    local scenario="$1" outdir="$2" original_csv="$3" out_file="$4" baseline="$5"
    shift 5
    if [[ ! -f "$original_csv" ]]; then
        return
    fi
    local bl_arg=""
    [[ -f "$baseline" ]] && bl_arg="--baseline $baseline"
    # shellcheck disable=SC2086
    python3 "$SCRIPT_DIR/measure.py" \
        --original  "$original_csv" \
        --outdir    "$outdir" \
        --scenario  "$scenario" \
        --out-file  "$out_file" \
        $bl_arg \
        "$@"
}

# Merge all *_metrics.json files in outdir into visibility_metrics.json.
merge_metrics() {
    local outdir="$1" scenario="$2"
    python3 - <<PYEOF
import json, pathlib
d = pathlib.Path("$outdir")
merged = {"scenario": "$scenario", "strategies": {}}
for f in sorted(d.glob("*_metrics.json")):
    if f.name == "visibility_metrics.json":
        continue
    merged["strategies"].update(json.loads(f.read_text()).get("strategies", {}))
(d / "visibility_metrics.json").write_text(json.dumps(merged, indent=2))
print(f"[merge] {len(merged['strategies'])} strategies → visibility_metrics.json")
PYEOF
}

# ── Main loop ─────────────────────────────────────────────────────────────────

for scenario in "${SCENARIOS[@]}"; do
    echo ""
    echo "--- $scenario ---"

    outdir="$BASE/04-visibility/$scenario"
    mkdir -p "$outdir"

    if ! $THREE_PASS; then
        csv="$RAW_BASE/$scenario/all_logs.csv"
        if [[ ! -f "$csv" ]]; then
            echo "[skip] $scenario — no baseline CSV"
            continue
        fi

        LS_ARG="" DN_ARG="" SS_ARG="" PS_ARG="" DR_ARG="" TL_ARG="" BL_ARG=""
        [[ -d "$LOGSHRINK_BASE/$scenario" ]]     && LS_ARG="--logshrink-dir     $LOGSHRINK_BASE/$scenario"
        [[ -d "$DENUM_BASE/$scenario" ]]          && DN_ARG="--denum-dir          $DENUM_BASE/$scenario"
        [[ -d "$SALO_STREAM_BASE/$scenario" ]]    && SS_ARG="--salo-stream-dir    $SALO_STREAM_BASE/$scenario"
        [[ -d "$PREPROC_STREAM_BASE/$scenario" ]] && PS_ARG="--preproc-stream-dir $PREPROC_STREAM_BASE/$scenario"
        [[ -d "$DRAIN_STREAM_BASE/$scenario" ]]   && DR_ARG="--drain-stream-dir   $DRAIN_STREAM_BASE/$scenario"

        timeline="$RAW_BASE/$scenario/timeline.json"
        [[ -f "$timeline" ]] && TL_ARG="--timeline $timeline"
        [[ -f "$BASELINE_CSV" ]] && BL_ARG="--baseline $BASELINE_CSV"

        python3 "$SCRIPT_DIR/measure.py" \
            --original  "$csv" \
            --outdir    "$outdir" \
            --scenario  "$scenario" \
            $LS_ARG $DN_ARG $SS_ARG $PS_ARG $DR_ARG $TL_ARG $BL_ARG

    else
        # ── Multi-pass ────────────────────────────────────────────────────────

        # Group 1 — logshrink + denum vs pass-1 raw
        offline_csv="$RAW_BASE/$scenario/all_logs.csv"
        if [[ -f "$offline_csv" ]]; then
            LS_ARG="" DN_ARG="" TL_ARG=""
            [[ -d "$LOGSHRINK_BASE/$scenario" ]] && LS_ARG="--logshrink-dir $LOGSHRINK_BASE/$scenario"
            [[ -d "$DENUM_BASE/$scenario" ]]      && DN_ARG="--denum-dir     $DENUM_BASE/$scenario"
            tl="$RAW_BASE/$scenario/timeline.json"
            [[ -f "$tl" ]] && TL_ARG="--timeline $tl"
            if [[ -n "$LS_ARG" || -n "$DN_ARG" ]]; then
                measure_group "$scenario" "$outdir" "$offline_csv" \
                    offline_metrics.json "$BASELINE_CSV" $LS_ARG $DN_ARG $TL_ARG
            fi
        fi

        # Group 2 — SALO vs its own raw
        salo_csv="$SALO_RAW/$scenario/all_logs.csv"
        salo_baseline="$SALO_BASELINE_CSV"
        salo_dir="$SALO_STREAM_BASE/$scenario"
        if [[ -f "$salo_csv" && -d "$salo_dir" ]]; then
            TL_ARG=""
            tl="$SALO_RAW/$scenario/timeline.json"
            [[ -f "$tl" ]] && TL_ARG="--timeline $tl"
            measure_group "$scenario" "$outdir" "$salo_csv" \
                salo_metrics.json "$salo_baseline" --salo-stream-dir "$salo_dir" $TL_ARG
        fi

        # Group 3 — preproc vs its own raw
        preproc_csv="$PREPROC_RAW/$scenario/all_logs.csv"
        preproc_baseline="$PREPROC_BASELINE_CSV"
        preproc_dir="$PREPROC_STREAM_BASE/$scenario"
        if [[ -f "$preproc_csv" && -d "$preproc_dir" ]]; then
            TL_ARG=""
            tl="$PREPROC_RAW/$scenario/timeline.json"
            [[ -f "$tl" ]] && TL_ARG="--timeline $tl"
            measure_group "$scenario" "$outdir" "$preproc_csv" \
                preproc_metrics.json "$preproc_baseline" --preproc-stream-dir "$preproc_dir" $TL_ARG
        fi

        # Group 4 — drain vs its own raw
        drain_csv="$DRAIN_RAW/$scenario/all_logs.csv"
        drain_baseline="$DRAIN_BASELINE_CSV"
        drain_dir="$DRAIN_STREAM_BASE/$scenario"
        if [[ -f "$drain_csv" && -d "$drain_dir" ]]; then
            TL_ARG=""
            tl="$DRAIN_RAW/$scenario/timeline.json"
            [[ -f "$tl" ]] && TL_ARG="--timeline $tl"
            measure_group "$scenario" "$outdir" "$drain_csv" \
                drain_metrics.json "$drain_baseline" --drain-stream-dir "$drain_dir" $TL_ARG
        fi

        merge_metrics "$outdir" "$scenario"
    fi
done

echo ""
echo "============================================================"
echo " B-04: Visibility complete."
echo "============================================================"
