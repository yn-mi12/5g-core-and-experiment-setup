#!/usr/bin/env bash
# B-log-strategies/daemonSet/run.sh
#
# Deploys one or all three streaming filter DaemonSets, runs every scenario with
# the DaemonSets active (they tail /var/log/pods/ in real time), collects raw
# logs as ground truth plus the filtered streams, then tears down.
#
# Strategy modes (default: all)
#   --strategy salo    — deploy + collect SALO DaemonSet only
#   --strategy preproc — deploy + collect preprocessing DaemonSet only
#   --strategy drain   — deploy + collect Drain3 DaemonSet only
#   --strategy all     — deploy + collect all three DaemonSets in a single pass
#                        (each stream goes to 05-daemonSet/run-all/{strategy}-stream/)
#
# NOTE: run_all.sh invokes each strategy as a separate pass with its own cluster
# reset and --run-tag run-{strategy}. Use --strategy all only for manual combined
# runs; the analysis scripts do not auto-discover run-all output directories.
#
#
# Output layout (example for --strategy salo --run-tag run-salo):
#   $DATA_DIR/B-log-strategies/05-daemonSet/run-salo/
#     raw/<scenario>/all_logs.csv
#     salo-stream/<scenario>/filtered.csv
#                                        metrics.json
#
#   PRE_DURATION (default 600s)  FAULT_DURATION (default 300s)  POST_DURATION (default 300s)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
B_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$B_DIR/../lib/common.sh"
DATA_DIR="${EXPERIMENT_DATA_DIR:-$DATA_DIR}"

B_LIB="$B_DIR/lib"
OUT_BASE="$DATA_DIR/B-log-strategies"

UE_COUNT=50
STEADY_DURATION=600
BURSTY_DURATION=600
COLLECT_WARMUP=${COLLECT_WARMUP:-0}
PRE_DURATION="${PRE_DURATION:-600}"
FAULT_DURATION="${FAULT_DURATION:-300}"
POST_DURATION="${POST_DURATION:-300}"

STRATEGY="all"
RUN_TAG=""
ONLY_SCENARIO=""
FROM_SCENARIO=""
FAULTS_ONLY=false
SKIP_DEPLOY=false
SKIP_VISIBILITY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --strategy)        STRATEGY="$2";        shift 2 ;;
        --run-tag)         RUN_TAG="$2";         shift 2 ;;
        --scenario)        ONLY_SCENARIO="$2";   shift 2 ;;
        --from-scenario)   FROM_SCENARIO="$2";   shift 2 ;;
        --faults-only)     FAULTS_ONLY=true;     shift   ;;
        --skip-deploy)     SKIP_DEPLOY=true;     shift   ;;
        --skip-visibility) SKIP_VISIBILITY=true; shift   ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

if [[ "$STRATEGY" != "salo" && "$STRATEGY" != "preproc" && "$STRATEGY" != "drain" && "$STRATEGY" != "all" ]]; then
    echo "ERROR: --strategy must be salo, preproc, drain, or all"
    exit 1
fi

if [[ -z "$RUN_TAG" ]]; then
    RUN_TAG="run-${STRATEGY}"
fi

if [[ -n "$RUN_TAG" ]]; then
    SC_BASE="$OUT_BASE/05-daemonSet/$RUN_TAG"
else
    SC_BASE="$OUT_BASE/05-daemonSet"
fi
RAW_BASE="$SC_BASE/raw"

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

_FROM_REACHED=false
should_run() {
    local sc="$1"
    if [[ -n "$FROM_SCENARIO" ]]; then
        [[ "$sc" == "$FROM_SCENARIO" ]] && _FROM_REACHED=true
        [[ "$_FROM_REACHED" == "true" ]] || return 1
    fi
    [[ -z "$ONLY_SCENARIO" || "$ONLY_SCENARIO" == "$sc" ]]
}

collect_salo()    { [[ "$STRATEGY" == "salo"    || "$STRATEGY" == "all" ]]; }
collect_preproc() { [[ "$STRATEGY" == "preproc" || "$STRATEGY" == "all" ]]; }
collect_drain()   { [[ "$STRATEGY" == "drain"   || "$STRATEGY" == "all" ]]; }


restart_daemonSets() {
    echo "[daemonSet] Restarting DaemonSet(s) for clean filter state ..."
    local agents=()
    collect_salo    && agents+=("log-filter-agent-salo")
    collect_preproc && agents+=("log-filter-agent-preproc")
    collect_drain   && agents+=("log-filter-agent-drain")
    for ds in "${agents[@]}"; do
        kubectl rollout restart daemonset/"$ds" -n open5gs 2>/dev/null || true
        kubectl rollout status  daemonset/"$ds" -n open5gs --timeout=60s 2>/dev/null || true
    done
    sleep 5
}

bring_up_cluster() {
    echo "[reset] Full cluster restart (cluster-start.sh)..."
    bash "$B_DIR/../../cluster-start.sh"
    bash "$LIB_DIR/provision_ues.sh" "$UE_COUNT"
    ensure_portforward_prometheus
    ensure_portforward_loki

    echo "  [reset] Waiting for all ${UE_COUNT} UE tunnels (180s max)..."
    local gnb_ue_pod tun_count tun_deadline
    gnb_ue_pod=$(kubectl get pods -n open5gs -l app.kubernetes.io/name=ueransim-gnb \
        --no-headers 2>/dev/null | grep gnb-ues | awk '{print $1}' | head -1)
    tun_deadline=$(($(date +%s) + 180))
    tun_count=0
    while [[ $(date +%s) -lt $tun_deadline ]]; do
        if [[ -n "$gnb_ue_pod" ]]; then
            tun_count=$(kubectl exec -n open5gs "$gnb_ue_pod" -- \
                ip link show 2>/dev/null | grep -c uesimtun || true)
        fi
        [[ "${tun_count:-0}" -ge "$UE_COUNT" ]] && break
        sleep 5
    done
    echo "  [reset] gnb-ues tunnels: ${tun_count:-0}/${UE_COUNT}"
}

refresh_portforward_loki() {
    stop_portforward 3100
    sleep 1
    start_portforward monitoring svc/loki 3100 3100
    local i
    for i in 1 2 3; do
        if curl -sf "http://127.0.0.1:3100/ready" >/dev/null 2>&1; then
            echo "[pf] Loki ready"
            return 0
        fi
        sleep 2
    done
    echo "[pf] WARNING: Loki /ready did not respond after port-forward restart" >&2
}

collect_scenario() {
    local scenario="$1" t0="$2" t1="$3"
    local raw_dir="$RAW_BASE/$scenario"
    mkdir -p "$raw_dir"
    refresh_portforward_loki

    echo "[collect] $scenario  raw ..."
    python3 "$B_LIB/collect_raw_loki.py" \
        --url "$LOKI_URL" --start "$t0" --end "$t1" --out "$raw_dir"

    if collect_salo; then
        local salo_dir="$SC_BASE/salo-stream/$scenario"
        mkdir -p "$salo_dir"
        echo "[collect] $scenario  salo-stream ..."
        python3 "$SCRIPT_DIR/collect.py" \
            --url "$LOKI_URL" --strategy salo \
            --start "$t0" --end "$t1" \
            --out "$salo_dir" --scenario "$scenario" \
            --raw-csv "$raw_dir/all_logs.csv" \
            ${PROM_URL:+--prom-url "$PROM_URL"}
    fi

    if collect_preproc; then
        local preproc_dir="$SC_BASE/preproc-stream/$scenario"
        mkdir -p "$preproc_dir"
        echo "[collect] $scenario  preproc-stream ..."
        python3 "$SCRIPT_DIR/collect.py" \
            --url "$LOKI_URL" --strategy preproc \
            --start "$t0" --end "$t1" \
            --out "$preproc_dir" --scenario "$scenario" \
            --raw-csv "$raw_dir/all_logs.csv" \
            ${PROM_URL:+--prom-url "$PROM_URL"}
    fi

    if collect_drain; then
        local drain_dir="$SC_BASE/drain-stream/$scenario"
        mkdir -p "$drain_dir"
        echo "[collect] $scenario  drain-stream ..."
        python3 "$SCRIPT_DIR/collect.py" \
            --url "$LOKI_URL" --strategy drain \
            --start "$t0" --end "$t1" \
            --out "$drain_dir" --scenario "$scenario" \
            --raw-csv "$raw_dir/all_logs.csv" \
            ${PROM_URL:+--prom-url "$PROM_URL"}
    fi

}

run_fault_scenario() {
    local b_name="$1" chaos_yaml="$2"
    local raw_dir="$RAW_BASE/$b_name"

    echo ""
    echo "────────────────────────────────────────────────────────────"
    echo " Scenario: $b_name"
    echo "────────────────────────────────────────────────────────────"

    echo "[reset] Bringing up cluster for $b_name..."
    local buc_ok=0
    for _buc in 1 2 3; do
        bring_up_cluster && { buc_ok=1; break; }
        echo "  [reset] bring_up_cluster attempt ${_buc}/3 failed — retrying after 60s..."
        sleep 60
    done
    [[ "$buc_ok" -eq 1 ]] || { echo "  [reset] FATAL — cluster never came up after 3 attempts" >&2; exit 1; }

    echo "[daemonSet] Re-deploying filter-agent DaemonSet(s) after cluster restart..."
    bash "$SCRIPT_DIR/deploy.sh" --strategy "$STRATEGY"

    mkdir -p "$raw_dir"
    local recreate_left=2 health_ok=0
    while :; do
        local h
        for h in 1 2 3; do
            if bash "$LIB_DIR/health_check.sh" "pre-${b_name}" \
                    "$raw_dir/health_pre.json" 2>/dev/null; then
                health_ok=1; break
            fi
            echo "  [gate] pre-fault health check failed (retry $h/3) —" \
                 "waiting 30s for UEs to settle..."
            sleep 30
        done
        [[ "$health_ok" -eq 1 ]] && break
        if [[ "$recreate_left" -le 0 ]]; then
            echo "[ABORT] health check still failing after recreates for $b_name" >&2
            echo "[ABORT] Re-run with: --scenario $b_name" >&2
            exit 1
        fi
        echo "  [gate] health unrecovered — full cluster recreate" \
             "(${recreate_left} recreate(s) left)..."
        recreate_left=$((recreate_left - 1))
        bring_up_cluster
        bash "$SCRIPT_DIR/deploy.sh" --strategy "$STRATEGY"
    done

    bash "$LIB_DIR/run_fault.sh" \
        --name           "$b_name" \
        --manifest       "$CHAOS_DIR/$chaos_yaml" \
        --out            "$raw_dir" \
        --pre-duration   "$PRE_DURATION" \
        --fault-duration "$FAULT_DURATION" \
        --post-duration  "$POST_DURATION"

    local loki_pre="$raw_dir/loki/pre/all.csv"
    local loki_during="$raw_dir/loki/during/all.csv"
    local loki_post="$raw_dir/loki/post/all.csv"
    if [[ -f "$loki_pre" && -f "$loki_during" && -f "$loki_post" ]]; then
        {
            cat "$loki_pre"
            tail -n +2 "$loki_during"
            tail -n +2 "$loki_post"
        } > "$raw_dir/all_logs.csv"
        echo "[collect] $b_name  all_logs.csv: $(wc -l < "$raw_dir/all_logs.csv") lines"
    else
        echo "[collect] WARNING: per-phase Loki CSVs not found — falling back to full-window query"
        local t0 t1
        t0=$(python3 -c "import json; d=json.load(open('$raw_dir/timeline.json')); print(d['pre']['start'])")
        t1=$(python3 -c "import json; d=json.load(open('$raw_dir/timeline.json')); print(d['post']['end'])")
        refresh_portforward_loki
        python3 "$B_LIB/collect_raw_loki.py" \
            --url "$LOKI_URL" --start "$t0" --end "$t1" --out "$raw_dir"
    fi

    collect_salo    && { mkdir -p "$SC_BASE/salo-stream/$b_name";
                         cp "$raw_dir/timeline.json" "$SC_BASE/salo-stream/$b_name/timeline.json"; }
    collect_preproc && { mkdir -p "$SC_BASE/preproc-stream/$b_name";
                         cp "$raw_dir/timeline.json" "$SC_BASE/preproc-stream/$b_name/timeline.json"; }
    collect_drain   && { mkdir -p "$SC_BASE/drain-stream/$b_name";
                         cp "$raw_dir/timeline.json" "$SC_BASE/drain-stream/$b_name/timeline.json"; }

    local t0 t1
    t0=$(python3 -c "import json; d=json.load(open('$raw_dir/timeline.json')); print(d['pre']['start'])")
    t1=$(python3 -c "import json; d=json.load(open('$raw_dir/timeline.json')); print(d['post']['end'])")
    refresh_portforward_loki

    if collect_salo; then
        echo "[collect] $b_name  salo-stream ..."
        python3 "$SCRIPT_DIR/collect.py" \
            --url "$LOKI_URL" --strategy salo \
            --start "$t0" --end "$t1" \
            --out "$SC_BASE/salo-stream/$b_name" --scenario "$b_name" \
            --raw-csv "$raw_dir/all_logs.csv" \
            ${PROM_URL:+--prom-url "$PROM_URL"}
    fi

    if collect_preproc; then
        echo "[collect] $b_name  preproc-stream ..."
        python3 "$SCRIPT_DIR/collect.py" \
            --url "$LOKI_URL" --strategy preproc \
            --start "$t0" --end "$t1" \
            --out "$SC_BASE/preproc-stream/$b_name" --scenario "$b_name" \
            --raw-csv "$raw_dir/all_logs.csv" \
            ${PROM_URL:+--prom-url "$PROM_URL"}
    fi

    if collect_drain; then
        echo "[collect] $b_name  drain-stream ..."
        python3 "$SCRIPT_DIR/collect.py" \
            --url "$LOKI_URL" --strategy drain \
            --start "$t0" --end "$t1" \
            --out "$SC_BASE/drain-stream/$b_name" --scenario "$b_name" \
            --raw-csv "$raw_dir/all_logs.csv" \
            ${PROM_URL:+--prom-url "$PROM_URL"}
    fi

    bash "$LIB_DIR/health_check.sh" "post-${b_name}" "$raw_dir/health_post.json" || true
}

# ──────────────────────────────────────────────────────────────────────────────
# Step 1 — Deploy
# ──────────────────────────────────────────────────────────────────────────────

check_cluster_ready
ensure_portforward_prometheus

if ! $SKIP_DEPLOY; then
    echo ""
    echo "============================================================"
    echo " Deploying filter-agent DaemonSet(s) (strategy=$STRATEGY)"
    echo "============================================================"
    bash "$SCRIPT_DIR/deploy.sh" --strategy "$STRATEGY" \
        --data-dir "$OUT_BASE"
fi

echo ""
echo "============================================================"
echo " B-05: daemonSet live evaluation  (strategy=$STRATEGY)"
echo " output: $SC_BASE"
echo " scenarios: steady  bursty"
echo "            fault-pod-crash-amf  fault-memory-pressure-upf  fault-network-delay-nrf"
echo "            fault-network-partition-amf-scp  fault-packet-loss-upf"
echo "            fault-upf-infra-packet-loss  fault-nrf-cascade  fault-udm-pod-crash"
echo "============================================================"

# ──────────────────────────────────────────────────────────────────────────────
# Scenarios
# ──────────────────────────────────────────────────────────────────────────────

if $FAULTS_ONLY; then
    echo "[skip] Steady and bursty scenarios (--faults-only)"
else

    # Steady
    if should_run steady; then
        echo ""
        echo "────────────────────────────────────────────────────────────"
        echo " Scenario: steady (${STEADY_DURATION}s, ${UE_COUNT} UEs)"
        echo "────────────────────────────────────────────────────────────"
        reset_experiment_state "B-daemonSet-steady" "$UE_COUNT"
        scale_ues "$UE_COUNT"
        wait_for_pods_stable open5gs 120
        restart_daemonSets
        if [[ "$COLLECT_WARMUP" -gt 0 ]]; then
            sleep_with_progress "$COLLECT_WARMUP" "waiting for AMF/SCP to reach steady logging rate"
        fi
        log_experiment_start "B-daemonSet-steady" "$RAW_BASE/steady"

        T0=$(now_ts)
        sleep_with_progress "$STEADY_DURATION" "steady"
        T1=$(now_ts)

        collect_scenario steady "$T0" "$T1"
        log_experiment_end "$RAW_BASE/steady"
    fi

    # Bursty
    if should_run bursty; then
        echo ""
        echo "────────────────────────────────────────────────────────────"
        echo " Scenario: bursty (${BURSTY_DURATION}s, UE scale cycles)"
        echo "────────────────────────────────────────────────────────────"
        reset_experiment_state "B-daemonSet-bursty" "$UE_COUNT"
        scale_ues "$UE_COUNT"
        restart_daemonSets
        log_experiment_start "B-daemonSet-bursty" "$RAW_BASE/bursty"

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

        sleep_with_progress "$BURSTY_DURATION" "bursty"
        T1=$(now_ts)
        wait "$BURST_PID" 2>/dev/null || true

        collect_scenario bursty "$T0" "$T1"
        log_experiment_end "$RAW_BASE/bursty"
    fi

fi  

# Fault scenarios
if should_run fault-pod-crash-amf;            then run_fault_scenario fault-pod-crash-amf            03-pod-crash-amf.yaml;                    fi
if should_run fault-memory-pressure-upf;      then run_fault_scenario fault-memory-pressure-upf      02-memory-pressure-upf.yaml;              fi
if should_run fault-network-delay-nrf;        then run_fault_scenario fault-network-delay-nrf        09-network-delay-nrf.yaml;                fi
if should_run fault-network-partition-amf-scp; then run_fault_scenario fault-network-partition-amf-scp 05-network-partition-amf-scp.yaml;      fi
if should_run fault-packet-loss-upf;          then run_fault_scenario fault-packet-loss-upf          06-packet-loss-upf.yaml;                  fi
if should_run fault-upf-infra-packet-loss;    then run_fault_scenario fault-upf-infra-packet-loss    14-upf-infrastructure-packet-loss.yaml;   fi
if should_run fault-nrf-cascade;              then run_fault_scenario fault-nrf-cascade              15-nrf-cascade.yaml;                      fi
if should_run fault-udm-pod-crash;            then run_fault_scenario fault-udm-pod-crash            19-udm-pod-crash.yaml;                    fi

# ──────────────────────────────────────────────────────────────────────────────
# Teardown
# ──────────────────────────────────────────────────────────────────────────────

echo ""
echo "════════════════════════════════════════════════════════════"
echo " Tearing down filter-agent DaemonSet(s)"
echo "════════════════════════════════════════════════════════════"
bash "$SCRIPT_DIR/deploy.sh" --teardown || true

# ──────────────────────────────────────────────────────────────────────────────
# Visibility (skipped when called from run_all.sh)
# ──────────────────────────────────────────────────────────────────────────────

if ! $SKIP_VISIBILITY; then
    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo " Running visibility analysis"
    echo "════════════════════════════════════════════════════════════"
    bash "$B_DIR/04-visibility/run.sh" \
        --raw-base            "$RAW_BASE" \
        --salo-stream-base    "$SC_BASE/salo-stream" \
        --preproc-stream-base "$SC_BASE/preproc-stream" \
        --drain-stream-base   "$SC_BASE/drain-stream"
fi

echo ""
echo "============================================================"
echo " B-05 daemonSet evaluation complete."
echo " Filtered CSVs : $SC_BASE"
echo " Visibility    : $OUT_BASE/04-visibility"
echo "============================================================"
