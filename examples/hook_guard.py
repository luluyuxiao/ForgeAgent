"""Example PreToolUse hook: protect local credential files."""

from __future__ import annotations

import json
import sys

payload = json.load(sys.stdin)
path = str((payload.get("tool_input") or {}).get("path") or "")
if path in {".env", ".env.local"}:
    print("credential files are protected", file=sys.stderr)
    raise SystemExit(2)
