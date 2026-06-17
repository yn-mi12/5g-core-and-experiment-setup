from pathlib import Path

REPO_ROOT   = Path(__file__).parent.parent.parent
DATA_ROOT   = REPO_ROOT / "data" / "B-log-strategies"
FIGURES_DIR = Path(__file__).parent / "figures"
TABLES_DIR  = Path(__file__).parent / "tables"

STRATEGIES = ["logshrink", "denum", "salo", "preprocessing", "drain"]

OFFLINE_STRATEGY_LIST = ["logshrink", "denum"]
ONLINE_STRATEGY_LIST  = ["salo", "preprocessing", "drain"]

LOSSLESS_STRATEGIES = {"logshrink", "denum"}

def strategy_dirs_for_root(data_root: Path) -> dict:
    return {
        "logshrink":     data_root / "02-logshrink",
        "denum":         data_root / "03-denum",
        "salo":          data_root / "05-daemonSet" / "run-salo"    / "salo-stream",
        "preprocessing": data_root / "05-daemonSet" / "run-preproc" / "preproc-stream",
        "drain":         data_root / "05-daemonSet" / "run-drain"   / "drain-stream",
    }


def visibility_dir_for_root(data_root: Path) -> Path:
    return data_root / "04-visibility"

RUN_FILTER = None

def discover_run_dirs() -> list:
    """Return [(run_label, data_root)] for every experiment run found under data/run-NN/."""
    runs = []
    experiments_dir = REPO_ROOT / "data"
    for d in sorted(experiments_dir.glob("run-[0-9][0-9]")):
        candidate = d / "B-log-strategies"
        if candidate.exists():
            runs.append((d.name, candidate))
    if not runs and DATA_ROOT.exists():
        runs.append(("run-01", DATA_ROOT))
    if RUN_FILTER:
        filtered = [r for r in runs if r[0] == RUN_FILTER]
        if filtered:
            return filtered
    return runs or [("run-01", DATA_ROOT)]

VISIBILITY_KEYS = {
    "logshrink":     "logshrink",
    "denum":         "denum",
    "salo":          "salo-stream",
    "preprocessing": "preproc-stream",
    "drain":         "drain-stream",
}

STRATEGY_FROM_VIS_KEY = {v: k for k, v in VISIBILITY_KEYS.items()}

SCENARIOS = [
    "steady",
    "bursty",
    "fault-pod-crash-amf",
    "fault-memory-pressure-upf",
    "fault-network-delay-nrf",
    "fault-network-partition-amf-scp",
    "fault-packet-loss-upf",
    "fault-upf-infra-packet-loss",
    "fault-nrf-cascade",
    "fault-udm-pod-crash",
]

FAULT_SCENARIOS = [
    "fault-pod-crash-amf",
    "fault-memory-pressure-upf",
    "fault-network-delay-nrf",
    "fault-network-partition-amf-scp",
    "fault-packet-loss-upf",
    "fault-upf-infra-packet-loss",
    "fault-nrf-cascade",
    "fault-udm-pod-crash",
]

SCENARIO_LABELS = {
    "steady":                          "Steady-state",
    "bursty":                          "Bursty",
    "fault-pod-crash-amf":             "Fault: pod crash (AMF)",
    "fault-memory-pressure-upf":       "Fault: mem pressure (UPF)",
    "fault-network-delay-nrf":         "Fault: net delay (NRF)",
    "fault-network-partition-amf-scp": "Fault: net partition (AMF-SCP)",
    "fault-packet-loss-upf":           "Fault: packet loss (UPF)",
    "fault-upf-infra-packet-loss":     "Fault: infra pkt loss (UPF)",
    "fault-nrf-cascade":               "Fault: NRF cascade",
    "fault-udm-pod-crash":             "Fault: pod crash (UDM)",
}

STRATEGY_LABELS = {
    "logshrink":     "LogShrink",
    "denum":         "Denum",
    "salo":          "SALO",
    "preprocessing": "Log Preprocessing",
    "drain":         "Drain",
}

PALETTE = {
    "logshrink":     "#1565C0",
    "denum":         "#E65100",
    "salo":          "#2E7D32",
    "preprocessing": "#6A1B9A",
    "drain":         "#00838F",
}

FIGURE_EXT = "png"
FIGURE_DPI = 150

FONT_SIZE_TITLE  = 11
FONT_SIZE_LABEL  = 10
FONT_SIZE_TICK   = 9
FONT_SIZE_LEGEND = 9
