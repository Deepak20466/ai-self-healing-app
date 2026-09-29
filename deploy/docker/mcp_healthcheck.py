"""HEALTHCHECK helper for mcp-pod's Docker image.

mcp-pod has no `/healthz` route (see Dockerfile.mcp's comment for why) --
this treats *any* HTTP response from `/mcp`, even a 4xx, as healthy, and
only a connection failure (nothing listening) as unhealthy.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

port = os.environ.get("MCP_PORT", "8003")
try:
    urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp", timeout=2)
except urllib.error.HTTPError:
    sys.exit(0)
except Exception:
    sys.exit(1)
else:
    sys.exit(0)
