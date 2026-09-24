"""The monitored demo app (app-pod): a tiny catalog/orders API with 7 seeded bugs.

Runtime auto-fixes may only ever touch files under this package (see
SPEC.md's mcp-pod section) — that's what keeps the healer from ever being
able to break itself.
"""
