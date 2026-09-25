"""Measure real idle RSS of the 4 pods (SPEC.md ACCEPTANCE CRITERIA: "Total
idle RAM, excluding Postgres, is under 300 MB, and this is measured and
shown in the README").

Usage: start the 4 pods yourself (`honcho start`, or one `uvicorn`/`python
-m` command per terminal — see README "Running the pods"), let them settle
for a few seconds, then run:

    python scripts/measure_ram.py

It finds each pod's process by the port it's listening on (via `psutil`,
cross-platform, no `netstat`/`ss` shelling out) and reports RSS in MB per
pod plus the total, excluding PostgreSQL entirely (a separate, pre-existing
system service, not one of "the 4 pods" this constraint is about).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psutil

REPO_ROOT = Path(__file__).resolve().parent.parent

PODS = {
    "app": 8001,
    "sentinel": 8002,
    "mcp": 8003,
    "healer": 8000,
}


def _find_listening_pid(port: int) -> int | None:
    for conn in psutil.net_connections(kind="inet"):
        if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
            return conn.pid
    return None


def main() -> int:
    results: dict[str, dict[str, object]] = {}
    total_mb = 0.0
    missing = []

    for name, port in PODS.items():
        pid = _find_listening_pid(port)
        if pid is None:
            missing.append(name)
            results[name] = {"port": port, "running": False}
            continue
        try:
            proc = psutil.Process(pid)
            rss_mb = proc.memory_info().rss / (1024 * 1024)
        except psutil.NoSuchProcess:
            results[name] = {"port": port, "running": False}
            missing.append(name)
            continue
        results[name] = {
            "port": port,
            "running": True,
            "pid": pid,
            "rss_mb": round(rss_mb, 1),
        }
        total_mb += rss_mb

    print(json.dumps(results, indent=2))
    print(f"\nTotal RSS across running pods: {total_mb:.1f} MB")
    if missing:
        print(f"NOT running / not measured: {', '.join(missing)}")
        print("(start all 4 pods first for a complete measurement)")
        return 1

    under_budget = total_mb < 300.0
    print(f"Under 300MB budget: {'YES' if under_budget else 'NO'}")

    out_path = REPO_ROOT / "ram_measurement.json"
    out_path.write_text(json.dumps({"pods": results, "total_mb": round(total_mb, 1)}, indent=2))
    print(f"Wrote {out_path}")
    return 0 if under_budget else 1


if __name__ == "__main__":
    sys.exit(main())
