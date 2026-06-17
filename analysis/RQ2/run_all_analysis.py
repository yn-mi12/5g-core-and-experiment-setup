"""
RQ2 analysis — master runner.

Usage (from the repo root):
    cd analysis/RQ2
    pip install -r requirements.txt   # first time only
    python run_all_analysis.py

Outputs land in:
    analysis/RQ2/figures/   PNG figures
    analysis/RQ2/tables/    CSV summary tables

Data must be present at:
    data/B-log-strategies/   (single run)
  or
    data/run-NN/B-log-strategies/   (multiple runs, N≥01)

See README.md for the full reproduction guide.
"""

import importlib
import importlib.util
import sys
import time
import traceback
from pathlib import Path


def _check_dependencies():
    required = ["pandas", "matplotlib", "numpy", "seaborn", "scipy"]
    missing = [pkg for pkg in required if importlib.util.find_spec(pkg) is None]
    if missing:
        print("Missing Python packages:", ", ".join(missing))
        print("Install with:  pip install -r requirements.txt")
        raise SystemExit(1)


def _check_data():
    repo_root = Path(__file__).parent.parent.parent
    primary   = repo_root / "data" / "B-log-strategies"
    multi_run = sorted((repo_root / "data").glob("run-[0-9][0-9]"))

    if primary.exists():
        return
    if any((d / "B-log-strategies").exists() for d in multi_run):
        return

    print("No experiment data found.")
    print(f"Expected one of:")
    print(f"  {primary}")
    print(f"  {repo_root / 'data' / 'run-01' / 'B-log-strategies'}")
    print("Run the B-log-strategies experiment first:  bash experiments/B-log-strategies/run_all.sh")
    raise SystemExit(1)


def main() -> None:
    _check_dependencies()
    _check_data()

    import rq2a_volume_reduction
    import rq2b_overhead
    import rq2c_visibility
    import rq2d_tradeoffs

    MODULES = [
        ("RQ2a — Telemetry volume reduction",     rq2a_volume_reduction),
        ("RQ2b — Processing overhead",            rq2b_overhead),
        ("RQ2c — Retained visibility",            rq2c_visibility),
        ("RQ2d — Trade-off analysis",             rq2d_tradeoffs),
    ]

    from config import FIGURES_DIR, TABLES_DIR
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    TABLES_DIR.mkdir(parents=True, exist_ok=True)

    total_start = time.perf_counter()
    results = []

    for label, module in MODULES:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
        t0 = time.perf_counter()
        status = "OK"
        try:
            module.run()
        except Exception:
            status = "FAILED"
            traceback.print_exc()
        elapsed = time.perf_counter() - t0
        results.append((label, elapsed, status))
        print(f"  → {status}  ({elapsed:.1f}s)")

    total = time.perf_counter() - total_start
    print(f"\n{'='*60}")
    print("  Summary")
    print(f"{'='*60}")
    for label, elapsed, status in results:
        mark = "✓" if status == "OK" else "✗"
        print(f"  {mark}  {label:<44}  {elapsed:5.1f}s  {status}")
    print(f"\n  Total: {total:.1f}s")

    failed = [lbl for lbl, _, s in results if s != "OK"]
    if failed:
        print(f"\n  FAILED modules ({len(failed)}):")
        for lbl in failed:
            print(f"    - {lbl}")
        raise SystemExit(1)

    print(f"\n  Figures: {FIGURES_DIR}")
    print(f"  Tables:  {TABLES_DIR}")


if __name__ == "__main__":
    main()
