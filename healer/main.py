"""healer-pod entrypoint: `python -m healer.main` (equivalent to, and the
preferred name over, `python -m healer.worker`).

A separate module from `healer/worker.py` because SPEC.md names `healer/
main.py` specifically as "backend selection", and `run_worker()` already logs
which backend (`USE_CLAUDE_CODE=true` free mode vs. API mode) it selected at
startup (see `healer.worker._select_backend`) — this file is just the named,
documented process entrypoint for it.
"""

from __future__ import annotations

from healer.worker import main

if __name__ == "__main__":
    main()
