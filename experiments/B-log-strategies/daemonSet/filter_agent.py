#!/usr/bin/env python3
"""
B-log-strategies/daemonSet/filter_agent.py

Streaming log filter agent — runs as a Kubernetes DaemonSet.

Tails pod log files from /var/log/pods/ (the same source as Promtail),
applies a streaming filter, and writes kept lines to a local CSV file.
Filtered lines are also pushed to Loki as a secondary/optional output.

Strategies
----------
  salo    — SALO two-level filter (tier threshold + adaptive error-burst flagging)
  preproc — Preprocessing Steps 1+2 (template dedup: temporal + spatial)
  drain   — Drain online log-cluster dedup (one pass per cluster per window)
"""

import csv
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import inotify_simple
    _INOTIFY_AVAILABLE = True
except ImportError:
    _INOTIFY_AVAILABLE = False

# ──────────────────────────────────────────────────────────────────────────────
# Environment config
# ──────────────────────────────────────────────────────────────────────────────

FILTER_STRATEGY  = os.environ.get("FILTER_STRATEGY",  "salo")
OUTPUT_FILE      = os.environ.get("OUTPUT_FILE",      "/data/filtered.csv")
LOKI_URL         = os.environ.get("LOKI_URL",         "http://loki:3100")
LOG_DIR          = os.environ.get("LOG_DIR",          "/var/log/pods")
NAMESPACE        = os.environ.get("NAMESPACE",        "open5gs")
BATCH_INTERVAL_S = float(os.environ.get("BATCH_INTERVAL_S", "1.0"))
BATCH_LINES      = int(os.environ.get("BATCH_LINES",   "200"))
POLL_INTERVAL_S  = float(os.environ.get("POLL_INTERVAL_S", "0.25"))

PUSH_URL = f"{LOKI_URL}/loki/api/v1/push"

# ──────────────────────────────────────────────────────────────────────────────
# Log parsing 
# ──────────────────────────────────────────────────────────────────────────────

_LOG_RE = re.compile(
    r'^(?P<date>\d{2}/\d{2})\s+'
    r'(?P<time>\d{2}:\d{2}:\d{2}\.\d+):\s+'
    r'\[(?P<component>[^\]]+)\]\s+'
    r'(?P<level>\w+):\s*'
    r'(?P<message>.*)',
    re.DOTALL,
)

LEVEL_ORDER = {
    "DEBUG": 0, "INFO": 1,
    "WARNING": 2, "WARN": 2,
    "ERROR": 3, "CRITICAL": 4, "FATAL": 4,
}

_GO_LEVEL_RE = re.compile(r'(?:^|\s)level=(\w+)', re.IGNORECASE)
_GO_MSG_RE   = re.compile(r'\bmsg="([^"]*)"|\bmsg=(\S+)')

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfnihlp]')

_VAR_PATS = [
    re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I),
    re.compile(r'\d+\.\d+\.\d+\.\d+(:\d+)?'),
    re.compile(r'0x[0-9a-fA-F]+'),
    re.compile(r'imsi-\S+|suci-\S+|\bsupi-\S+'),
    re.compile(r'\(\.\./[^)]+\)'),
    re.compile(r'\b\d{2}/\d{2}\b'),
    re.compile(r'\d{2}:\d{2}:\d{2}\.\d+'),
    re.compile(r'\b\d+\b'),
]


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s)


def _make_template(msg: str) -> str:
    t = msg
    for pat in _VAR_PATS:
        t = pat.sub('<*>', t)
    return re.sub(r'(<\*>\s*)+', '<*> ', t).strip()


_MONGO_SEV = {
    "I": "INFO", "D": "DEBUG",
    "W": "WARNING", "E": "ERROR", "F": "FATAL",
}


def _normalize_mongodb(line: str) -> str:
    """Convert a MongoDB JSON log line to plain text for template mining."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return line
    if "msg" not in d:
        return line
    sev  = _MONGO_SEV.get((d.get("s") or "I").strip(), "INFO")
    comp = (d.get("c") or "-").strip()
    msg  = d.get("msg", "")
    attr = d.get("attr") or {}
    parts = [f"MONGO {sev} {comp} {msg}"]
    if isinstance(attr, dict):
        for k, v in attr.items():
            if isinstance(v, (str, int, float, bool)):
                parts.append(f"{k}={v}")
    return " ".join(parts)


_MONGO_NORM_RE = re.compile(
    r'^MONGO\s+(?P<level>INFO|DEBUG|WARNING|ERROR|FATAL)\s+\S+\s+(?P<message>.*)',
    re.DOTALL,
)

_UERANSIM_RE = re.compile(
    r'^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}\.\d+\]\s+'
    r'\[(?P<component>[^\]]+)\]\s+'
    r'\[(?P<level>\w+)\]\s*'
    r'(?P<message>.*)',
    re.DOTALL,
)


def parse_line(line: str) -> tuple:
    """Returns (level_str, level_ord, template)."""
    clean = _strip_ansi(line).strip()
    m = _LOG_RE.match(clean) or _MONGO_NORM_RE.match(clean) or _UERANSIM_RE.match(clean)
    if m:
        level = m.group("level").upper()
        tmpl = _make_template(m.group("message"))
    else:
        lm = _GO_LEVEL_RE.search(clean)
        if lm:
            level = lm.group(1).upper()
            gm = _GO_MSG_RE.search(clean)
            if gm:
                tmpl = _make_template(gm.group(1) or gm.group(2) or "")
            else:
                tmpl = _make_template(clean)
        else:
            level = "INFO"
            tmpl = _make_template(clean)
    return level, LEVEL_ORDER.get(level, 1), tmpl


# ──────────────────────────────────────────────────────────────────────────────
# SALO 
# ──────────────────────────────────────────────────────────────────────────────

_SALO_WINDOW_SECS       = 60
_LOOKFORWARD_WINDOWS    = 2
_RARITY_THRESHOLD       = 0.05
_RARITY_MIN_WINDOWS     = 5     

_BURST_EMA_ALPHA        = 0.3   
_BURST_MULTIPLIER       = 2.5   
_BURST_MIN_HISTORY      = 3     
_BURST_ABS_MIN          = 5     

CORE_NFS    = {"amf", "smf", "upf", "nrf"}
SUPPORT_NFS = {"udm", "udr", "pcf", "ausf", "bsf", "nssf"}
EXCLUDE_APPS = {"beyla"}

_TIER_THRESHOLD = {
    "core":           LEVEL_ORDER["WARNING"],
    "support":        LEVEL_ORDER["ERROR"],
    "infrastructure": LEVEL_ORDER["ERROR"],
    "unknown":        LEVEL_ORDER["ERROR"],
}


def _location_tier(app: str) -> str:
    a = app.lower()
    if a in CORE_NFS:          return "core"
    if a in SUPPORT_NFS:       return "support"
    if "mongo" in a or "db" in a: return "infrastructure"

    if "gnb" in a or "ueransim" in a or a in {"ue", "ues"} or a.startswith("ue-"):
        return "core"
    return "unknown"


class SALOFilter:
    """
    Stateful streaming SALO filter — shared across all pods.

    rare_templates can be pre-loaded from the offline run's data.
    Dynamic rarity detection supplements the pretrained set: templates
    seen in fewer than _RARITY_THRESHOLD fraction of completed windows
    are treated as rare and kept.
    """

    def __init__(self, rare_templates: set = None):
        self.rare_templates = rare_templates or set()
        self._error_counts: dict = defaultdict(int)      
        self._flagged: dict = {}
        self._tmpl_win_count: dict = defaultdict(int)    
        self._total_windows: int = 0
        self._prev_window: int | None = None
        self._cur_win_tmpls: set = set()
        self._app_prev_win: dict = {}                    
        self._burst_ema: dict = {}                      
        self._burst_ema_n: dict = defaultdict(int)       

    def _commit_app_window(self, app: str, old_window: int) -> None:
        """Incorporate old_window's error count into the per-app EMA."""
        count = float(self._error_counts.get((app, old_window), 0))
        n = self._burst_ema_n[app]
        if n == 0:
            self._burst_ema[app] = count
        else:
            self._burst_ema[app] = (
                _BURST_EMA_ALPHA * count
                + (1.0 - _BURST_EMA_ALPHA) * self._burst_ema[app]
            )
        self._burst_ema_n[app] = n + 1

    def _burst_threshold(self, app: str) -> float:
        """Error-count threshold above which the current window is flagged."""
        if self._burst_ema_n.get(app, 0) < _BURST_MIN_HISTORY:
            return float("inf")   
        return max(self._burst_ema[app] * _BURST_MULTIPLIER, float(_BURST_ABS_MIN))

    def keep(self, app: str, ts_ns: int, line: str) -> bool:
        if app in EXCLUDE_APPS:
            return False

        if app == "mongodb":
            line = _normalize_mongodb(line)

        window = (ts_ns // 1_000_000_000) // _SALO_WINDOW_SECS
        _, lev_ord, tmpl = parse_line(line)

        if self._prev_window is None:
            self._prev_window = window
        if window != self._prev_window:
            for t in self._cur_win_tmpls:
                self._tmpl_win_count[t] += 1
            self._total_windows += 1
            self._cur_win_tmpls = set()
            self._prev_window = window
        self._cur_win_tmpls.add(tmpl)

        app_prev = self._app_prev_win.get(app)
        if app_prev is None:
            self._app_prev_win[app] = window
        elif window != app_prev:
            self._commit_app_window(app, app_prev)
            self._app_prev_win[app] = window

        if lev_ord >= LEVEL_ORDER["ERROR"]:
            self._error_counts[(app, window)] += 1
            if self._error_counts[(app, window)] > self._burst_threshold(app):
                for offset in range(_LOOKFORWARD_WINDOWS + 1):
                    self._flagged[(app, window + offset)] = True

        if self._flagged.get((app, window)):
            return True

        if lev_ord >= _TIER_THRESHOLD[_location_tier(app)]:
            return True

        if tmpl in self.rare_templates:
            return True

        if self._total_windows >= _RARITY_MIN_WINDOWS:
            freq = self._tmpl_win_count.get(tmpl, 0) / self._total_windows
            if freq < _RARITY_THRESHOLD:
                return True

        return False


# ──────────────────────────────────────────────────────────────────────────────
# Preprocessing
# ──────────────────────────────────────────────────────────────────────────────

_TEMPORAL_WINDOW_SECS = 15
_APRIORI_WINDOW_SECS  = 10
_EVICT_LAG            = 2   


class PreprocFilter:
    """
    Stateful streaming preprocessing filter.

    Step 1 — template extraction
    Step 2 — temporal + spatial deduplication 
    Step 3 — optional Apriori suppression rules pre-mined offline
    """

    def __init__(self, static_rules: set = None):
        self._tmpl_to_tid: dict = {}
        self._tid_n = 0
        self._seen_temporal: dict = {}   
        self._seen_spatial:  dict = {}   
        self._win_active_tids: dict = defaultdict(set)  

        self._effect_to_causes: dict = defaultdict(set)
        self._effect_tids: set = set()
        if static_rules:
            for cause, effect in static_rules:
                self._effect_to_causes[effect].add(cause)
                self._effect_tids.add(effect)

    def _tid(self, tmpl: str) -> int:
        if tmpl not in self._tmpl_to_tid:
            self._tmpl_to_tid[tmpl] = self._tid_n
            self._tid_n += 1
        return self._tmpl_to_tid[tmpl]

    def _evict(self, current_win: int, state: dict):
        stale = [w for w in state if w < current_win - _EVICT_LAG]
        for w in stale:
            del state[w]

    def keep(self, pod: str, app: str, ts_ns: int, line: str) -> bool:
        if app in EXCLUDE_APPS:
            return False

        if app == "mongodb":
            line = _normalize_mongodb(line)

        secs  = ts_ns // 1_000_000_000
        win_t = secs // _TEMPORAL_WINDOW_SECS
        win_a = secs // _APRIORI_WINDOW_SECS

        _, lev_ord, tmpl = parse_line(line)
        if lev_ord >= LEVEL_ORDER["ERROR"]:
            return True

        tid = self._tid(tmpl)

        self._evict(win_t, self._seen_temporal)
        self._evict(win_t, self._seen_spatial)
        self._evict(win_a, self._win_active_tids)

        t_key = (pod, tid)
        if t_key in self._seen_temporal.get(win_t, ()):
            return False
        self._seen_temporal.setdefault(win_t, set()).add(t_key)

        s_key = (app, tid)
        if s_key in self._seen_spatial.get(win_t, ()):
            return False
        self._seen_spatial.setdefault(win_t, set()).add(s_key)

        if self._effect_tids and tid in self._effect_tids:
            causes = self._effect_to_causes[tid]
            active = self._win_active_tids.get(win_a, ())
            if any(c in active for c in causes):
                return False
        self._win_active_tids[win_a].add(tid)

        return True


# ──────────────────────────────────────────────────────────────────────────────
# Drain 
# ──────────────────────────────────────────────────────────────────────────────

_DRAIN_WINDOW_SECS  = 15   
_DRAIN_DEPTH        = 4
_DRAIN_SIM_TH       = 0.4
_DRAIN_MAX_CLUSTERS = 500


class DrainFilter:
    """
    Online log filter using the Drain log-parsing algorithm.

    Each line is assigned to a template cluster.
    A line is kept when:
      - level >= ERROR (always), or
      - its cluster is new / its template just changed (structural novelty), or
      - its cluster has not appeared from the same app in _DRAIN_WINDOW_SECS.

    Separate miners per app prevent cross-contamination of templates between
    different 5G network functions.
    """

    def __init__(self):
        try:
            from drain3.template_miner import TemplateMiner
            from drain3.template_miner_config import TemplateMinerConfig
        except ImportError as exc:
            raise ImportError(
                "drain3 is required for the 'drain' strategy: pip install drain3"
            ) from exc
        self._TemplateMiner = TemplateMiner
        self._Config        = TemplateMinerConfig
        self._miners: dict  = {}         
        self._last_seen: dict = {}       

    def _miner(self, app: str):
        if app not in self._miners:
            cfg = self._Config()
            cfg.drain_depth            = _DRAIN_DEPTH
            cfg.drain_sim_th           = _DRAIN_SIM_TH
            cfg.drain_max_clusters     = _DRAIN_MAX_CLUSTERS
            cfg.parametrize_numeric_tokens = True
            self._miners[app] = self._TemplateMiner(config=cfg)
        return self._miners[app]

    def keep(self, pod: str, app: str, ts_ns: int, line: str) -> bool:
        if app in EXCLUDE_APPS:
            return False

        if app == "mongodb":
            line = _normalize_mongodb(line)

        _, lev_ord, _ = parse_line(line)

        if lev_ord >= LEVEL_ORDER["ERROR"]:
            return True

        clean = _strip_ansi(line).strip()
        try:
            result = self._miner(app).add_log_message(clean)
        except Exception:
            return True   

        cluster_id  = result["cluster_id"]
        change_type = result.get("change_type")   

        secs = ts_ns // 1_000_000_000
        key  = (app, cluster_id)
        last = self._last_seen.get(key)
        self._last_seen[key] = secs

        if change_type != "none":
            return True

        return last is None or (secs - last) >= _DRAIN_WINDOW_SECS


# ──────────────────────────────────────────────────────────────────────────────
# Loki push
# ──────────────────────────────────────────────────────────────────────────────

def loki_push(streams: list) -> bool:
    """
    POST streams to Loki's HTTP push endpoint.
    Returns True on success, False on any failure.
    """
    if not streams:
        return True
    payload = json.dumps({"streams": streams}).encode()
    req = urllib.request.Request(
        PUSH_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        print(f"[push] Loki HTTP {exc.code} {exc.reason}: {body}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"[push] Loki error: {exc}", file=sys.stderr)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Pod log file tailer
# ──────────────────────────────────────────────────────────────────────────────

def _parse_kubelet_ts(time_str: str) -> int:
    """Parse kubelet RFC3339 timestamp to nanoseconds."""
    try:
        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1_000_000_000)
    except Exception:
        return time.time_ns()


class PodTailer:
    """
    Tail a single pod container log file and apply the filter function.
    """

    def __init__(self, path: Path, filter_fn):
        self.path = path
        self.filter_fn = filter_fn   
        container = path.parent.name
        self.app  = container.removeprefix("open5gs-")
        self.pod  = path.parts[-3]      
        self._fh  = None
        self._inode: int = -1

    def _open(self):
        try:
            st = os.stat(self.path)
            self._fh = open(self.path, "r", errors="replace")
            self._inode = st.st_ino
        except OSError:
            self._fh = None

    def _parse_entry(self, raw: str):
        """Parse a raw kubelet log entry into (ts_ns, line)."""
        try:
            entry = json.loads(raw)
            line  = entry.get("log", raw).rstrip("\n")
            ts_ns = _parse_kubelet_ts(entry.get("time", ""))
        except (json.JSONDecodeError, ValueError):
            parts = raw.split(" ", 3)
            if len(parts) >= 4:
                ts_ns = _parse_kubelet_ts(parts[0])
                line  = parts[3]
            else:
                ts_ns = time.time_ns()
                line  = raw
        return ts_ns, line

    def _drain(self) -> list:
        """Read remaining content and return kept pairs."""
        kept = []
        if self._fh is None:
            return kept
        try:
            for raw in self._fh:
                ts_ns, line = self._parse_entry(raw.rstrip("\n"))
                if self.filter_fn(self.pod, self.app, ts_ns, line):
                    kept.append((str(ts_ns), _strip_ansi(line)))
        except OSError:
            pass
        return kept

    def read_new(self) -> list:
        """Return kept (ts_ns_str, line) pairs from newly appended log data."""
        if self._fh is None:
            self._open()
            if self._fh is None:
                return []

        try:
            cur_inode = os.stat(self.path).st_ino
        except OSError:
            kept = self._drain()
            self._fh.close()
            self._fh = None
            return kept

        if cur_inode != self._inode:
            kept = self._drain()
            self._fh.close()
            self._open()
            return kept

        kept = []
        for raw in self._fh:
            ts_ns, line = self._parse_entry(raw.rstrip("\n"))
            if self.filter_fn(self.pod, self.app, ts_ns, line):
                kept.append((str(ts_ns), _strip_ansi(line)))

        return kept

    def drain_and_close(self) -> list:
        """Read all remaining bytes then close the handle."""
        kept = self._drain()
        if self._fh:
            self._fh.close()
            self._fh = None
        return kept

    def close(self):
        if self._fh:
            self._fh.close()


# ──────────────────────────────────────────────────────────────────────────────
# File discovery
# ──────────────────────────────────────────────────────────────────────────────

def find_log_files(log_dir: str, namespace: str) -> list:
    """
    Return all active *.log files for pods in the target namespace.
    """
    pattern = os.path.join(log_dir, f"{namespace}_*", "*", "*.log")
    return [Path(p) for p in glob.glob(pattern) if not p.endswith(".tmp")]


# ──────────────────────────────────────────────────────────────────────────────
# Filter factory
# ──────────────────────────────────────────────────────────────────────────────

def build_filter(strategy: str):
    """
    Return a (pod, app, ts_ns, line) -> bool callable.
    """
    if strategy == "salo":
        rare: set = set()
        rt_path = os.environ.get("RARE_TEMPLATES_JSON")
        if rt_path and os.path.exists(rt_path):
            with open(rt_path) as f:
                rare = set(json.load(f))
            print(f"[filter-agent] loaded {len(rare)} rare templates from {rt_path}")
        filt = SALOFilter(rare_templates=rare)
        return lambda pod, app, ts_ns, line: filt.keep(app, ts_ns, line)

    elif strategy == "preproc":
        rules: set = set()
        rules_path = os.environ.get("STATIC_RULES_JSON")
        if rules_path and os.path.exists(rules_path):
            with open(rules_path) as f:
                rules = {tuple(r) for r in json.load(f)}
            print(f"[filter-agent] loaded {len(rules)} static causal rules from {rules_path}")
        filt = PreprocFilter(static_rules=rules or None)
        return lambda pod, app, ts_ns, line: filt.keep(pod, app, ts_ns, line)

    elif strategy == "drain":
        filt = DrainFilter()
        return lambda pod, app, ts_ns, line: filt.keep(pod, app, ts_ns, line)

    else:
        raise ValueError(f"Unknown FILTER_STRATEGY={strategy!r}  "
                         f"(use 'salo', 'preproc', or 'drain')")


# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────

def _check_loki():
    """Verify Loki is reachable at startup; log a clear warning if not."""
    try:
        req = urllib.request.Request(f"{LOKI_URL}/ready", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        print(f"[filter-agent] Loki reachable at {LOKI_URL}")
    except Exception as exc:
        print(f"[filter-agent] WARNING: Loki not reachable at {LOKI_URL}: {exc} "
              f"— filtered lines will be written to {OUTPUT_FILE} only",
              file=sys.stderr)


def _flush_loki_pending(pending: dict, loki_ok: bool, lines_written: int,
                        n_tailers: int) -> bool:
    """Push pending lines to Loki; return updated (loki_ok, lines_written)."""
    if pending:
        streams = [
            {"stream": {"pod": pod, "app": app,
                        "job": f"filter-agent/{FILTER_STRATEGY}",
                        "namespace": NAMESPACE},
             "values": list(vals)}
            for (pod, app), vals in pending.items() if vals
        ]
        ok = loki_push(streams)
        if not ok and loki_ok:
            print(f"[filter-agent] Loki push failing — "
                  f"data is still written to {OUTPUT_FILE}",
                  file=sys.stderr)
        loki_ok = ok
        pending.clear()
    if lines_written % 500 == 0 and lines_written > 0:
        print(f"[filter-agent] {lines_written} lines kept so far "
              f"(tracking {n_tailers} files)")
    return loki_ok


def main():
    print(f"[filter-agent] strategy={FILTER_STRATEGY}  output={OUTPUT_FILE}  "
          f"push={PUSH_URL}  log_dir={LOG_DIR}  namespace={NAMESPACE}  "
          f"inotify={'yes' if _INOTIFY_AVAILABLE else 'no (polling fallback)'}")

    _check_loki()

    out_path = Path(OUTPUT_FILE)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    csv_fh = open(out_path, "w", newline="", buffering=1)
    csv_writer = csv.writer(csv_fh)
    csv_writer.writerow(["timestamp_ns", "pod", "app", "line"])
    lines_written = 0

    filter_fn = build_filter(FILTER_STRATEGY)
    tailers: dict = {}
    pending: dict = defaultdict(list)
    last_flush = time.monotonic()
    loki_ok = True

    _IN_MASK = (
        inotify_simple.flags.MODIFY
        | inotify_simple.flags.CLOSE_WRITE
        | inotify_simple.flags.DELETE_SELF
        | inotify_simple.flags.MOVE_SELF
    ) if _INOTIFY_AVAILABLE else 0

    inotify = inotify_simple.INotify() if _INOTIFY_AVAILABLE else None
    wd_to_tailer: dict = {}   

    def _register(tailer: PodTailer):
        if inotify is None:
            return
        try:
            wd = inotify.add_watch(str(tailer.path), _IN_MASK)
            wd_to_tailer[wd] = tailer
        except OSError:
            pass  # file vanished between discovery and watch registration

    def _unregister(wd: int, tailer: PodTailer, reason: str):
        print(f"[filter-agent] -untracking ({reason})  app={tailer.app}  {tailer.path}")
        tailers.pop(tailer.path, None)
        wd_to_tailer.pop(wd, None)
        if inotify is not None:
            try:
                inotify.rm_watch(wd)
            except OSError:
                pass

    def _emit(tailer: PodTailer, rows: list):
        nonlocal lines_written
        for ts_ns_str, line in rows:
            csv_writer.writerow([ts_ns_str, tailer.pod, tailer.app, line])
            lines_written += 1
            pending[(tailer.pod, tailer.app)].append((ts_ns_str, line))

    try:
        while True:
            # ── Discover new pod log files ────────────────────────────────────
            for path in find_log_files(LOG_DIR, NAMESPACE):
                if path not in tailers:
                    t = PodTailer(path, filter_fn)
                    if t.app not in EXCLUDE_APPS:
                        tailers[path] = t
                        _register(t)
                        print(f"[filter-agent] +tracking  app={t.app}  {path}")

            # ── Read new lines ────────────────────────────────────────────────
            if _INOTIFY_AVAILABLE:
                try:
                    events = inotify.read(timeout=int(POLL_INTERVAL_S * 1000))
                except OSError:
                    events = []

                for event in events:
                    tailer = wd_to_tailer.get(event.wd)
                    if tailer is None:
                        continue
                    mask = event.mask
                    closing = mask & (inotify_simple.flags.CLOSE_WRITE
                                      | inotify_simple.flags.DELETE_SELF
                                      | inotify_simple.flags.MOVE_SELF)
                    if closing:
                        _emit(tailer, tailer.drain_and_close())
                        _unregister(event.wd, tailer, "closed/deleted")
                    elif mask & inotify_simple.flags.MODIFY:
                        rows = tailer.read_new()
                        _emit(tailer, rows)
                        if tailer._fh is None:
                            _unregister(event.wd, tailer, "vanished")
            else:
                dead = []
                for path, tailer in list(tailers.items()):
                    rows = tailer.read_new()
                    _emit(tailer, rows)
                    if tailer._fh is None:
                        dead.append(path)
                for path in dead:
                    print(f"[filter-agent] -untracking  app={tailers[path].app}  {path}")
                    del tailers[path]
                time.sleep(POLL_INTERVAL_S)

            # ── Flush to Loki ─────────────────────────────────────────────────
            now = time.monotonic()
            total = sum(len(v) for v in pending.values())
            if now - last_flush >= BATCH_INTERVAL_S or total >= BATCH_LINES:
                loki_ok = _flush_loki_pending(pending, loki_ok, lines_written,
                                              len(tailers))
                last_flush = now

    finally:
        csv_fh.flush()
        csv_fh.close()
        if inotify is not None:
            inotify.close()
        print(f"[filter-agent] exiting — {lines_written} lines written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
