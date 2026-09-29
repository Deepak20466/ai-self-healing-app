"""Generic HEALTHCHECK helper: GET $PORT_ENV_VAR's /healthz, exit 0 on 200.

Shared by Dockerfile.app / Dockerfile.sentinel / Dockerfile.healer (each
passes its own port env var name as argv[1] with a default in argv[2]) --
kept as one small script rather than three near-identical inline `python
-c` one-liners, which get awkward to quote correctly inside a Dockerfile's
shell-form CMD/HEALTHCHECK string.
"""

from __future__ import annotations

import os
import sys
import urllib.request

port_env_var = sys.argv[1]
default_port = sys.argv[2]
port = os.environ.get(port_env_var, default_port)
try:
    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
    sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
