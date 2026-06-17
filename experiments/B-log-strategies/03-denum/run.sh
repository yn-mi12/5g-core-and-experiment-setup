#!/usr/bin/env bash
# B-log-strategies/03-denum/run.sh
#
# Apply Denum to each collected scenario and record metrics.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../../lib/common.sh"
DATA_DIR="${EXPERIMENT_DATA_DIR:-$DATA_DIR}"

IN_BASE="$DATA_DIR/B-log-strategies/01-collect"
OUT_BASE="$DATA_DIR/B-log-strategies/03-denum"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --raw-base) IN_BASE="$2";  shift 2 ;;
        --out-base) OUT_BASE="$2"; shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

SCENARIOS=(steady bursty
           fault-pod-crash-amf fault-memory-pressure-upf fault-network-delay-nrf
           fault-network-partition-amf-scp fault-packet-loss-upf
           fault-upf-infra-packet-loss fault-nrf-cascade fault-udm-pod-crash)

echo ""
echo "============================================================"
echo " B-03: Denum"
echo "============================================================"

for scenario in "${SCENARIOS[@]}"; do
    csv="$IN_BASE/$scenario/all_logs.csv"
    if [[ ! -f "$csv" ]]; then
        echo "[skip] $scenario — no CSV found"
        continue
    fi
    echo ""
    echo "--- $scenario ---"
    python3 "$SCRIPT_DIR/apply.py" \
        --csv      "$csv" \
        --outdir   "$OUT_BASE/$scenario" \
        --scenario "$scenario"
done

echo ""
echo "============================================================"
echo " B-03 Denum complete."
echo "============================================================"
