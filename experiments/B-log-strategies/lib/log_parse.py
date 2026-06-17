#!/usr/bin/env python3
"""
B-log-strategies/lib/log_parse.py

Shared log-parsing utilities: Open5GS log regex, severity levels,
variable-token patterns, and template normalisation.
"""
import json as _json
import re

_MONGO_SEV = {
    "I": "INFO", "D": "DEBUG",
    "W": "WARNING", "E": "ERROR", "F": "FATAL",
}


def normalize_mongodb(line: str) -> str:
    """Convert a MongoDB JSON log line to plain text suitable for template mining."""
    try:
        d = _json.loads(line)
    except (_json.JSONDecodeError, ValueError):
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

MONGO_NORM_RE = re.compile(
    r'^MONGO\s+(?P<level>INFO|DEBUG|WARNING|ERROR|FATAL)\s+\S+\s+(?P<message>.*)',
    re.DOTALL,
)

LOG_RE = re.compile(
    r'^(?P<date>\d{2}/\d{2})\s+'
    r'(?P<time>\d{2}:\d{2}:\d{2}\.\d+):\s+'
    r'\[(?P<component>[^\]]+)\]\s+'
    r'(?P<level>\w+):\s*'
    r'(?P<message>.*)',
    re.DOTALL,
)

UERANSIM_RE = re.compile(
    r'^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}\.\d+\]\s+'
    r'\[(?P<component>[^\]]+)\]\s+'
    r'\[(?P<level>\w+)\]\s*'
    r'(?P<message>.*)',
    re.DOTALL,
)

LEVEL_ORDER = {
    "DEBUG": 0, "INFO": 1,
    "WARNING": 2, "WARN": 2,
    "ERROR": 3, "CRITICAL": 4, "FATAL": 4,
}

_VAR_PATS = [
    re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
               re.IGNORECASE),              # UUID
    re.compile(r'\d+\.\d+\.\d+\.\d+(:\d+)?'),  # IP(:port)
    re.compile(r'0x[0-9a-fA-F]+'),         # hex literal
    re.compile(r'imsi-\S+'),               # IMSI
    re.compile(r'suci-\S+'),               # SUCI
    re.compile(r'\bsupi-\S+'),             # SUPI
    re.compile(r'\(\.\./[^)]+\)'),         # (src/file.c:N)
    re.compile(r'\b\d{2}/\d{2}\b'),        # date MM/DD
    re.compile(r'\d{2}:\d{2}:\d{2}\.\d+'), # time HH:MM:SS.mmm
    re.compile(r'\b\d+\b'),                # standalone integer
    re.compile(r'\b[a-zA-Z]+\d+\b'),      # word+digit token (e.g. conn81, ue5)
]


def make_template(msg: str) -> str:
    t = msg
    for pat in _VAR_PATS:
        t = pat.sub('<*>', t)
    return re.sub(r'(<\*>\s*)+', '<*> ', t).strip()
