"""Claude Code status line script.

Claude Code pipes session JSON to this script on every status line refresh.
It saves the plan rate limits to latest.json (read by widget.pyw) and prints
a short status line. Readings older than what latest.json already holds are
ignored, so an idle session can't roll the meter back.
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "latest.json")


def is_stale(new, old):
    """True if `new` is an older reading than `old`. Several Claude Code sessions
    share latest.json, and an idle one still reports usage from its last request.
    Within one window usage only goes up, so a lower % there, or a reading from
    an earlier window, is out of date."""
    try:
        new_reset, old_reset = float(new["resets_at"]), float(old["resets_at"])
        if abs(new_reset - old_reset) < 60:  # same window
            return float(new["used_percentage"]) < float(old["used_percentage"])
        return new_reset < old_reset
    except (KeyError, TypeError, ValueError):
        return False


def save(limits, model=None, fresh=False):
    """Merge `limits` into latest.json and return what was kept. `fresh` marks a
    reading just fetched from the API, which always updates the timestamp."""
    try:
        with open(OUT, encoding="utf-8") as f:
            previous = json.load(f)
        previous_limits = previous.get("rate_limits") or {}
    except (OSError, ValueError, AttributeError):
        previous, previous_limits = {}, {}
    limits = dict(limits)
    stale = [k for k in limits if k in previous_limits and is_stale(limits[k], previous_limits[k])]
    for key in stale:
        limits[key] = previous_limits[key]
    if limits and (fresh or len(stale) < len(limits)):
        snapshot = {"captured_at": time.time(),
                    "model": model or previous.get("model") or "Claude",
                    "rate_limits": limits}
        try:
            fd, tmp = tempfile.mkstemp(dir=HERE, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(snapshot, f)
            os.replace(tmp, OUT)
        except OSError:
            pass
    return limits


if __name__ == "__main__":
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}

    model = (data.get("model") or {}).get("display_name") or "Claude"
    limits = data.get("rate_limits") or {}
    if limits:
        limits = save(limits, model)

    parts = [model]
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx {ctx:.0f}%")
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        pct = (limits.get(key) or {}).get("used_percentage")
        if pct is not None:
            parts.append(f"{label} {pct:.0f}%")
    print(" | ".join(parts))
