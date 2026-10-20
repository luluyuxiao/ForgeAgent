from __future__ import annotations

import json

from forgeagent.trace import JsonlTrace
from forgeagent.trace_viewer import render_trace_html


def test_jsonl_trace_sequences_and_redacts_content(tmp_path):
    path = tmp_path / "trace.jsonl"
    trace = JsonlTrace(path)
    trace.emit("one", {"content": "secret", "count": 1})
    trace.emit("two", {"arguments": {"token": "secret"}})

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert [row["sequence"] for row in rows] == [1, 2]
    assert rows[0]["content"].startswith("[redacted")
    assert "secret" not in path.read_text(encoding="utf-8")

    second_instance = JsonlTrace(path)
    second_instance.emit("three", {})
    last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["sequence"] == 3


def test_trace_viewer_renders_summary_and_escapes_fields(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "sequence": 1,
                "event": "tool_completed",
                "tool": "read_file",
                "result": "<script>alert(1)</script>",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    output = render_trace_html(path, tmp_path / "trace.html")
    rendered = output.read_text(encoding="utf-8")

    assert "ForgeAgent Trace" in rendered
    assert "tool_completed" in rendered
    assert "&lt;script&gt;" in rendered
    assert "<script>alert(1)</script>" not in rendered
