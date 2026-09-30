"""Claude Code status line script.

Claude Code pipes session JSON to this script on every status line refresh.
It saves the plan rate limits to latest.json (read by widget.pyw) and prints
a short status line.
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "latest.json")

try:
    data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
except ValueError:
    data = {}

model = (data.get("model") or {}).get("display_name") or "Claude"
limits = data.get("rate_limits") or {}

if limits:
    snapshot = {"captured_at": time.time(), "model": model, "rate_limits": limits}
    try:
        fd, tmp = tempfile.mkstemp(dir=HERE, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, OUT)
    except OSError:
        pass

parts = [model]
ctx = (data.get("context_window") or {}).get("used_percentage")
if ctx is not None:
    parts.append(f"ctx {ctx:.0f}%")
for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
    pct = (limits.get(key) or {}).get("used_percentage")
    if pct is not None:
        parts.append(f"{label} {pct:.0f}%")
print(" | ".join(parts))
