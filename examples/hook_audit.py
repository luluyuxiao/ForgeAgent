"""Example PostToolUse hook: append a compact, content-free Tool audit record."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

payload = json.load(sys.stdin)
tool_input = payload.get("tool_input") or {}
response = str(payload.get("tool_response") or "")
record = {
    "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    "tool_name": str(payload.get("tool_name") or ""),
    "input_keys": sorted(str(key) for key in tool_input),
    "result_chars": len(response),
    "result": "error" if response.lstrip().lower().startswith("error") else "ok",
}

target = Path(".forgeagent/hook-audit.jsonl")
target.parent.mkdir(parents=True, exist_ok=True)
line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
descriptor = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
try:
    os.write(descriptor, line)
finally:
    os.close(descriptor)
