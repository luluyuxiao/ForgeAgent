"""Render ForgeAgent JSONL traces as a portable, human-readable HTML report."""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_trace(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path).expanduser().resolve()
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON on trace line {number}: {error}") from error
        if not isinstance(row, dict):
            raise TypeError(f"trace line {number} must be a JSON object")
        rows.append(row)
    return rows


def render_trace_html(path: str | Path, output: str | Path) -> Path:
    source = Path(path).expanduser().resolve()
    target = Path(output).expanduser().resolve()
    rows = load_trace(source)
    counts = Counter(str(row.get("event") or "unknown") for row in rows)
    prompt_tokens = sum(
        int(row.get("prompt_tokens") or 0)
        for row in rows
        if row.get("event") == "model_completed"
    )
    completion_tokens = sum(
        int(row.get("completion_tokens") or 0)
        for row in rows
        if row.get("event") == "model_completed"
    )
    denied = sum(
        1
        for row in rows
        if row.get("event") == "tool_decided" and not bool(row.get("allowed"))
    )
    cards = [
        ("Events", len(rows)),
        ("Runs", counts["run_started"]),
        ("Tool calls", counts["tool_started"]),
        ("Denied", denied),
        ("Prompt tokens", prompt_tokens),
        ("Completion tokens", completion_tokens),
    ]
    body = "\n".join(_render_row(row) for row in rows) or (
        '<p class="empty">The trace contains no events.</p>'
    )
    card_html = "\n".join(
        f'<div class="card"><span>{html.escape(label)}</span><strong>{value}</strong></div>'
        for label, value in cards
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ForgeAgent Trace</title>
  <style>
    :root {{ color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    body {{ max-width: 1100px; margin: 0 auto; padding: 32px 20px 80px; background: #0b1020; color: #e8ecf4; }}
    h1 {{ margin-bottom: 4px; }} .source {{ color: #9ba7bd; margin-top: 0; word-break: break-all; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(140px,1fr)); gap: 12px; margin: 24px 0; }}
    .card {{ background: #151d31; border: 1px solid #2b3753; border-radius: 12px; padding: 14px; }}
    .card span {{ color: #9ba7bd; display: block; font-size: 12px; }} .card strong {{ font-size: 24px; }}
    details {{ background: #11182a; border: 1px solid #26324d; border-radius: 10px; margin: 9px 0; overflow: hidden; }}
    summary {{ cursor: pointer; display: flex; gap: 12px; align-items: center; padding: 12px 14px; }}
    .seq {{ color: #7f8ca5; width: 52px; }} .event {{ color: #72d7ff; font-weight: 700; }} .time {{ color: #9ba7bd; margin-left: auto; font-size: 12px; }}
    pre {{ margin: 0; padding: 14px; overflow: auto; border-top: 1px solid #26324d; color: #cdd7ea; white-space: pre-wrap; }}
    .denied .event {{ color: #ff8c9b; }} .error .event {{ color: #ffb86b; }} .empty {{ color: #9ba7bd; }}
  </style>
</head>
<body>
  <h1>ForgeAgent Trace</h1>
  <p class="source">{html.escape(str(source))}</p>
  <section class="cards">{card_html}</section>
  <main>{body}</main>
</body>
</html>
"""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    return target


def _render_row(row: dict[str, Any]) -> str:
    event = str(row.get("event") or "unknown")
    css = ""
    if event == "tool_decided" and not bool(row.get("allowed")):
        css = "denied"
    elif bool(row.get("error")) or event.endswith("failed"):
        css = "error"
    fields = {
        key: value
        for key, value in row.items()
        if key not in {"sequence", "timestamp", "event"}
    }
    rendered = html.escape(json.dumps(fields, ensure_ascii=False, indent=2, default=str))
    return (
        f'<details class="{css}"><summary>'
        f'<span class="seq">#{html.escape(str(row.get("sequence", "?")))}</span>'
        f'<span class="event">{html.escape(event)}</span>'
        f'<span class="time">{html.escape(str(row.get("timestamp", "")))}</span>'
        f"</summary><pre>{rendered}</pre></details>"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a ForgeAgent JSONL trace as HTML")
    parser.add_argument("trace")
    parser.add_argument("--output", "-o")
    args = parser.parse_args(argv)
    source = Path(args.trace).expanduser()
    output = Path(args.output).expanduser() if args.output else source.with_suffix(".html")
    target = render_trace_html(source, output)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
