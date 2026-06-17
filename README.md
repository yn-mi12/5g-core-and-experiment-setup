# Cloud-Native 5G Telemetry Reduction — Reproduction Pack

Covers the **B-log-strategies experiment (RQ2)**: How can different log reduction strategies be effectively applied to lower the volume of logs collected from cloud-native 5G core networks?

> **Extended notes:** [`EXTENSIONS.md`](./EXTENSIONS.md) — extra collectors, traffic generators, per-fault hooks, bug fixes.

---

## Quick Start

```bash
# Full pipeline: cluster + 8 C-fault scenarios + B experiment + figures (~24 h)
./run_all.sh

# Step by step:
./cluster-start.sh                                                              # bring up cluster (~10–15 min)
./experiments/C-fault-detection/run_all.sh --only 2,3,5,6,9,14,15,19          # collect 8 fault scenarios (~3 h)
./experiments/B-log-strategies/run_all.sh                                       # run experiment    (~21 h)
cd analysis/RQ2 && python3 run_all_analysis.py                                  # generate figures  (~1 min)
```

Figures → `analysis/RQ2/figures/`, tables → `analysis/RQ2/tables/`.

> **Docker Hub auth:** create `kind/.dockerhub-auth` (two lines: username + read-only PAT) to avoid the 100/6h unauthenticated pull-rate limit.

---

## 1. Stack

| Layer           | Tool                                     | Version       |
| --------------- | ---------------------------------------- | ------------- |
| 5G core         | Open5GS (Gradiant Helm chart)            | 2.3.4         |
| RAN sim         | UERANSIM gNB + UEs (Gradiant Helm chart) | 0.2.6 / 0.1.2 |
| Orchestration   | kind (Kubernetes in Docker)              | latest        |
| Metrics         | kube-prometheus-stack                    | latest        |
| Logs            | Loki + Promtail                          | latest        |
| Traces          | Jaeger (all-in-one, in-memory)           | v4.7.0        |
| Span source     | Beyla (eBPF auto-instrumentation)        | ≥ 3.9.5       |
| Fault injection | Chaos Mesh                               | 2.7.2         |
| Collection      | Python 3 + `urllib`                      | —             |

---

## 2. Host prerequisites (Linux + Docker)

- Docker 29+ installed and running.
- `lsof` installed (`sudo apt install lsof`).
- Raise inotify limits (required for Promtail + Chaos Mesh, otherwise both crash):

```bash
sudo sysctl fs.inotify.max_user_instances=512
sudo sysctl fs.inotify.max_user_watches=524288

# Persist across reboots
echo "fs.inotify.max_user_instances=512"  | sudo tee -a /etc/sysctl.conf
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.conf
```

- Python 3.10+ packages:

```bash
pip install -r experiments/B-log-strategies/requirements.txt
pip install -r analysis/RQ2/requirements.txt
```

- C++ compiler (for the LogShrink Elastic parser):

```bash
sudo apt install build-essential
```

- Git submodules (LogShrink and Denum):

```bash
git submodule update --init
```

---

## 3. Install CLIs

```bash
# kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl

# kind
curl -Lo ./kind https://kind.sigs.k8s.io/dl/latest/kind-linux-amd64
sudo install -o root -g root -m 0755 kind /usr/local/bin/kind && rm kind

# helm
curl -L https://get.helm.sh/helm-v3.20.2-linux-amd64.tar.gz | tar xz
sudo mv linux-amd64/helm /usr/local/bin/helm && rm -rf linux-amd64
```

---

## 4. Daily lifecycle — `./cluster-start.sh`

After every reboot or Docker restart:

```bash
./cluster-start.sh              # recreate cluster + redeploy everything (~10–15 min)
./cluster-start.sh --skip-deploy  # recreate cluster only
```

The script: deletes and recreates the kind cluster, checks/raises inotify limits, redeploys the full stack (Open5GS, UERANSIM, kube-prometheus-stack, Loki, Jaeger, Beyla, Chaos Mesh), and sanity-checks all namespaces.

> **Note:** kind has no start/stop — the cluster lives as Docker containers. When Docker stops, recreate with `cluster-start.sh`.

**Don't restart Docker while experiments are running** — stop them first.

---

## 5. Run the B-log-strategies experiment

### What RQ2 asks

RQ2 evaluates whether log-reduction strategies can reduce 5G core telemetry volume without losing the signal needed to detect faults. It breaks into four sub-questions:

- **RQ2a — Volume reduction:** How much storage does each strategy save, and does reduction hold across both normal and fault-stressed scenarios?
- **RQ2b — Processing overhead:** How much CPU time, memory, and query latency does each strategy add? Offline strategies (LogShrink, Denum) are measured via `getrusage` on a batch run; online DaemonSet strategies (SALO, Preprocessing, Drain) are measured from the Prometheus cgroup during live operation.
- **RQ2c — Fault visibility:** Do filtered logs still contain the lines and log templates that signal a fault? Online strategies are lossy and may drop fault-relevant lines; offline strategies are lossless (visibility = 100% by construction).
- **RQ2d — Trade-offs:** Summary heatmaps comparing all strategies across reduction, overhead, and visibility simultaneously.

### Experiment structure

Evaluates five log-reduction strategies (LogShrink, Denum, SALO, Log Preprocessing, Drain) across 10 scenarios in five passes:

| Pass | What runs | CPU measured via |
|------|-----------|-----------------|
| 1 | Raw collection → LogShrink + Denum (offline batch) | `getrusage` during `apply.py` |
| 2 | SALO DaemonSet (live filter) | Prometheus cgroup |
| 3 | Log Preprocessing DaemonSet | Prometheus cgroup |
| 4 | Drain DaemonSet (online Drain3 clustering) | Prometheus cgroup |
| 5 | Visibility comparison across all strategies | — |

> **Prerequisite — C-fault data:** Pass 1 reads pre-collected fault logs from `data/C-fault-detection/`. Collect them first if running B manually: `./experiments/C-fault-detection/run_all.sh --only 2,3,5,6,9,14,15,19` (~3 h). Pass 1 will `exit 1` if this data is missing.

```bash
./experiments/B-log-strategies/run_all.sh                  # single run (~21 h)
./experiments/B-log-strategies/run_all.sh --runs 3         # three runs for 95% CI error bars
./experiments/B-log-strategies/run_all.sh --from 2         # resume from pass 2 (SALO)
./experiments/B-log-strategies/run_all.sh --faults-only    # skip steady/bursty
```

Output: `data/B-log-strategies/` (single run) or `data/run-NN/B-log-strategies/` (multiple runs).

### Scenarios

| Scenario | Description |
|----------|-------------|
| `steady` | Normal steady-state load |
| `bursty` | Elevated burst traffic |
| `fault-pod-crash-amf` | AMF pod killed (PodChaos) |
| `fault-memory-pressure-upf` | UPF OOM kill (StressChaos + in-pod alloc) |
| `fault-network-delay-nrf` | NRF 500 ms delay (NetworkChaos) |
| `fault-network-partition-amf-scp` | AMF↔SCP partition (NetworkChaos; targets SCP — Model D) |
| `fault-packet-loss-upf` | UPF packet loss (NetworkChaos) |
| `fault-upf-infra-packet-loss` | Infra-level packet loss at UPF node |
| `fault-nrf-cascade` | NRF kill → NF re-registration cascade |
| `fault-udm-pod-crash` | UDM pod killed |

---

## 6. RQ2 analysis

```bash
cd analysis/RQ2
python3 run_all_analysis.py
```

Individual sub-questions:

```bash
python3 rq2a_volume_reduction.py   # RQ2a: storage reduction per strategy × scenario
python3 rq2b_overhead.py           # RQ2b: CPU time, memory, query latency, throughput
python3 rq2c_visibility.py         # RQ2c: fault visibility and line retention (online only)
python3 rq2d_tradeoffs.py          # RQ2d: summary heatmaps across all three dimensions
```

Data consumed per script:
- **RQ2a, RQ2b, RQ2d** — `metrics.json` per strategy × scenario (written by `apply.py` for offline; `collect.py` for online)
- **RQ2c, RQ2d** — `visibility_metrics.json` per scenario (written by `04-visibility/measure.py`)

- **Multi-run CI:** if `data/run-01/`, `run-02/`, … exist, the analysis averages across them and adds 95% CI error bars. Set `RUN_FILTER = "run-02"` in `config.py` to analyse a single run.

---

## 7. Recovery

```bash
# UPF after OOM (clears stale PFCP)
kubectl rollout restart deployment/open5gs-smf -n open5gs

# AMF kill (rebuilds gNB SCTP and UE PDU sessions)
kubectl rollout restart deployment/ueransim-gnb deployment/ueransim-gnb-ues -n open5gs

# General sanity
kubectl exec -n open5gs deployment/ueransim-gnb-ues -- ping -I uesimtun0 -c 3 8.8.8.8
kubectl get pods -n open5gs
```

If `kubectl delete` of a chaos resource hangs, patch the finalizers:

```bash
kubectl patch <kind>/<name> -n open5gs --type=json \
  -p='[{"op":"remove","path":"/metadata/finalizers"}]'
```

---

## 8. File map

```
.
├── run_all.sh                       ← end-to-end pipeline (cluster + experiment + figures)
├── cluster-start.sh                 ← daily lifecycle (recreate cluster + redeploy)
├── kind/
│   ├── kind-config.yaml             ← kind cluster config: 3 nodes, eviction thresholds
│   ├── open5gs-values.yaml          ← Helm values: NF resource limits, MCC/MNC, slice
│   ├── monitoring/
│   │   └── beyla-daemonset.yaml     ← eBPF tracer DaemonSet (privileged, hostPID)
│   └── chaos/                       ← Chaos Mesh YAML for each fault scenario
├── experiments/
│   ├── lib/                         ← shared shell + Python helpers (port-forwards, Loki/Prom collection)
│   ├── C-fault-detection/           ← fault collection (22 faults; B needs 8)
│   └── B-log-strategies/
│       ├── run_all.sh               ← five-pass experiment runner
│       ├── requirements.txt
│       ├── 01-collect/              ← raw Loki collection per scenario
│       ├── 02-logshrink/            ← LogShrink compression
│       ├── 03-denum/                ← Denum compression
│       ├── 04-visibility/           ← cross-strategy visibility comparison
│       ├── daemonSet/               ← SALO / Preprocessing / Drain DaemonSet runners
│       │   ├── Dockerfile           ← image built and loaded into kind by deploy.sh
│       │   ├── requirements.txt     ← Python deps installed inside the container (not host)
│       │   ├── deploy.sh            ← builds image, loads into kind, applies DaemonSet YAML
│       │   ├── run.sh               ← orchestrates scenario loop + metrics collection
│       │   ├── collect.py           ← Prometheus CPU + metrics collection
│       │   ├── filter_agent.py      ← per-strategy log filter logic
│       │   └── pretrained/          ← pre-trained Drain templates (avoid cold-start bias)
│       ├── lib/                     ← shared Python helpers (log parsing, Loki collection)
│       └── cloned_repos/
│           ├── LogShrink/           ← git submodule (fork: yn-mi12/LogShrink)
│           └── Denum/               ← git submodule
├── data/
│   ├── C-fault-detection/           ← C-phase fault data
│   ├── B-log-strategies/            ← single-run output
│   └── run-NN/B-log-strategies/     ← additional runs for CI (optional)
└── analysis/
    └── RQ2/
        ├── run_all_analysis.py
        ├── config.py                ← paths, strategy names, plot styling
        ├── load_data.py
        ├── rq2a_volume_reduction.py
        ├── rq2b_overhead.py
        ├── rq2c_visibility.py
        ├── rq2d_tradeoffs.py
        ├── figures/                 ← output PNG figures
        └── tables/                  ← output CSV summary tables
```
