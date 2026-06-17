#!/usr/bin/env bash
# B-log-strategies/01-collect/run.sh
#
# Collect raw Open5GS logs for each of ten scenarios:
#   steady                          — 10 min, 50 UEs, no fault injection   (live Loki query)
#   bursty                          — 10 min, UE scale up/down cycles       (live Loki query)
#   fault-pod-crash-amf             — from data/C-fault-detection/03-pod-crash-amf
#   fault-memory-pressure-upf       — from data/C-fault-detection/02-memory-pressure-upf
#   fault-network-delay-nrf         — from data/C-fault-detection/09-network-delay-nrf
#   fault-network-partition-amf-scp — from data/C-fault-detection/05-network-partition-amf-scp
#   fault-packet-loss-upf           — from data/C-fault-detection/06-packet-loss-upf
#   fault-upf-infra-packet-loss     — from data/C-fault-detection/14-upf-infrastructure-packet-loss
#   fault-nrf-cascade               — from data/C-fault-detection/15-nrf-cascade
#   fault-udm-pod-crash             — from data/C-fault-detection/19-udm-pod-crash
#
# Fault scenarios reuse the ready-collected C-phase data
#
# Output:
#   $DATA_DIR/B-log-strategies/01-collect/<scenario>/

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../../lib/common.sh"
DATA_DIR="${EXPERIMENT_DATA_DIR:-$DATA_DIR}"

FAULTS_ONLY=false
if [[ "${1:-}" == "--faults-only" ]]; then FAULTS_ONLY=true; fi

B_LIB="$SCRIPT_DIR/../lib"
OUT_BASE="$DATA_DIR/B-log-strategies/01-collect"
UE_COUNT=50
STEADY_DURATION=600    # 10 min
BURSTY_DURATION=600    # 10 min
COLLECT_WARMUP=${COLLECT_WARMUP:-0}   

# ──────────────────────────────────────────────────────────────────────────────
# Helper: collect raw Loki logs and write all_logs.csv to out_dir
# ──────────────────────────────────────────────────────────────────────────────
collect_raw() {
    local start="$1" end="$2" out_dir="$3"
    mkdir -p "$out_dir"
    start_portforward monitoring svc/loki 3100 3100
    python3 "$B_LIB/collect_raw_loki.py" \
        --url   "$LOKI_URL" \
        --start "$start" \
        --end   "$end" \
        --out   "$out_dir"
}

if [[ "$FAULTS_ONLY" == false ]]; then
    check_cluster_ready
    ensure_portforward_loki

    # Disable Beyla during collection
    echo "[setup] Disabling Beyla daemonset..."
    kubectl patch daemonset beyla -n open5gs \
        --type=json \
        -p='[{"op":"add","path":"/spec/template/spec/nodeSelector","value":{"non-existing":"true"}}]' \
        2>/dev/null || true
    echo "[setup] Beyla disabled"
fi

echo ""
echo "============================================================"
echo " B-01: Raw log collection"
echo "============================================================"

if [[ "$FAULTS_ONLY" == false ]]; then
# ──────────────────────────────────────────────────────────────────────────────
# Steady-state
# ──────────────────────────────────────────────────────────────────────────────
echo ""
echo "--- Steady-state (${STEADY_DURATION}s, ${UE_COUNT} UEs) ---"
reset_experiment_state "B-collect-steady" "$UE_COUNT"
scale_ues "$UE_COUNT"
wait_for_pods_stable open5gs 120
if [[ "$COLLECT_WARMUP" -gt 0 ]]; then
    sleep_with_progress "$COLLECT_WARMUP" "waiting for AMF/SCP to reach steady logging rate"
fi

T0=$(now_ts)
sleep_with_progress "$STEADY_DURATION" "steady collection"
T1=$(now_ts)

collect_raw "$T0" "$T1" "$OUT_BASE/steady"

# ──────────────────────────────────────────────────────────────────────────────
# Bursty
# ──────────────────────────────────────────────────────────────────────────────
echo ""
echo "--- Bursty (${BURSTY_DURATION}s, UE scale cycles) ---"
reset_experiment_state "B-collect-bursty" "$UE_COUNT"
scale_ues "$UE_COUNT"

T0=$(now_ts)
BURSTY_END=$(( T0 + BURSTY_DURATION ))
(
    while [[ $(date +%s) -lt $BURSTY_END ]]; do
        scale_ues "$UE_COUNT" 2>/dev/null || true
        sleep 30
        scale_ues 5 2>/dev/null || true
        sleep 30
    done
) &
BURST_PID=$!

sleep_with_progress "$BURSTY_DURATION" "bursty collection"
T1=$(now_ts)
wait "$BURST_PID" 2>/dev/null || true

collect_raw "$T0" "$T1" "$OUT_BASE/bursty"

else
    echo "[skip] Steady and bursty scenarios (--faults-only)"
fi

if [[ "$FAULTS_ONLY" == false ]]; then
    echo "[restore] Re-enabling Beyla daemonset..."
    kubectl patch daemonset beyla -n open5gs \
        --type=json \
        -p='[{"op":"remove","path":"/spec/template/spec/nodeSelector/non-existing"}]' 2>/dev/null || true
    kubectl rollout status daemonset/beyla -n open5gs --timeout=2m 2>/dev/null || true
    echo "[restore] Beyla restored"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Use pre-collected C-fault-detection data
# ──────────────────────────────────────────────────────────────────────────────
derive_fault_from_C() {
    local b_name="$1" c_name="$2"
    local c_dir="$REPO_ROOT/data/C-fault-detection/$c_name"
    local out_dir="$OUT_BASE/$b_name"

    echo ""
    echo "--- Fault scenario: $b_name (from C: $c_name) ---"

    if [[ ! -d "$c_dir" ]]; then
        echo "[error] C output not found: $c_dir" >&2
        exit 1
    fi

    mkdir -p "$out_dir"

    { cat "$c_dir/loki/pre/all.csv"
      tail -n +2 "$c_dir/loki/during/all.csv"
      tail -n +2 "$c_dir/loki/post/all.csv"
    } > "$out_dir/all_logs.csv"

    cp "$c_dir/timeline.json" "$out_dir/timeline.json"

    echo "  -> $(wc -l < "$out_dir/all_logs.csv") lines in all_logs.csv"
}

derive_fault_from_C "fault-pod-crash-amf"             "03-pod-crash-amf"
derive_fault_from_C "fault-memory-pressure-upf"       "02-memory-pressure-upf"
derive_fault_from_C "fault-network-delay-nrf"          "09-network-delay-nrf"
derive_fault_from_C "fault-network-partition-amf-scp"  "05-network-partition-amf-scp"
derive_fault_from_C "fault-packet-loss-upf"            "06-packet-loss-upf"
derive_fault_from_C "fault-upf-infra-packet-loss"      "14-upf-infrastructure-packet-loss"
derive_fault_from_C "fault-nrf-cascade"                "15-nrf-cascade"
derive_fault_from_C "fault-udm-pod-crash"              "19-udm-pod-crash"

echo ""
echo "============================================================"
echo " B-01 collection complete. Data in: $OUT_BASE"
echo "============================================================"
