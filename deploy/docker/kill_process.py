"""Kill the first process (other than PID 1) whose cmdline contains argv[1].

Used by k8s-ci.yml's liveness/restart test: `kill -9 1` inside a container
does NOT work in general -- the Linux kernel specially exempts PID 1 of a
PID namespace from the default action of unhandled signals, including
SIGKILL, unless it has installed a handler for that signal (a well-known
container gotcha, confirmed live: a real k8s-ci run tried `kill -9 1`
against a `sh -c "uvicorn ..."` container -- sh is PID 1 there -- and
restartCount never moved). Killing the actual server process instead makes
PID 1's `sh -c` exit once its foregrounded child dies, which still
triggers the same `restartPolicy: Always` recovery a failed liveness probe
would.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys

needle = sys.argv[1].encode()
for p in os.listdir("/proc"):
    if not p.isdigit() or int(p) == 1:
        continue
    with contextlib.suppress(OSError):
        with open(f"/proc/{p}/cmdline", "rb") as f:
            if needle in f.read():
                os.kill(int(p), signal.SIGKILL)
