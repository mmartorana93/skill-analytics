#!/usr/bin/env python3
"""
hook.py — PostToolUse hook. NOT a data source: it only touches a marker file so
the dashboard's /api/heartbeat notices a change and auto-refreshes live.

Wired in ~/.claude/settings.json:
  "hooks": { "PostToolUse": [ { "matcher": "Skill|mcp__",
    "hooks": [ { "type": "command", "command": "python3 ~/.claude/skill-analytics/hook.py" } ] } ] }

Must be fast and never fail the tool call: always exits 0.
"""
import sys
import os
import json

SIGNAL = os.path.join(os.path.expanduser("~"), ".claude", "skill-analytics", "live-signal")


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    tool = payload.get("tool_name", "")
    if tool == "Skill" or tool.startswith("mcp__"):
        try:
            os.makedirs(os.path.dirname(SIGNAL), exist_ok=True)
            # Touch + record the last signal; content is optional/best-effort.
            with open(SIGNAL, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "tool": tool,
                    "cwd": payload.get("cwd"),
                    "session": payload.get("session_id"),
                    "ts": payload.get("timestamp"),
                }))
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
