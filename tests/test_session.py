from __future__ import annotations

import json

import pytest

from forgeagent.contracts import Usage
from forgeagent.policies import RunMode
from forgeagent.session import SessionState, SessionStore


def test_session_roundtrip_preserves_both_histories(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    state = SessionState(
        session_id="demo-1",
        model="test-model",
        mode=RunMode.PLANNING,
        transcript=[{"role": "user", "content": "full"}],
        active_messages=[{"role": "system", "content": "compacted"}],
        usage=Usage(12, 4),
        checkpoints=[{"call_id": "c1", "tool": "write_file", "files": []}],
        active_tools=["now", "read_file"],
    )

    path = store.save(state)
    restored = store.load("demo-1")

    assert path.stat().st_mode & 0o077 == 0
    assert restored.transcript == state.transcript
    assert restored.active_messages == state.active_messages
    assert restored.mode is RunMode.PLANNING
    assert restored.usage.total_tokens == 16
    assert restored.checkpoints == state.checkpoints
    assert restored.active_tools == state.active_tools


def test_session_id_cannot_escape_store(tmp_path):
    store = SessionStore(tmp_path)

    with pytest.raises(ValueError):
        store.save(SessionState(session_id="../escape", model="x"))


def test_unknown_session_and_invalid_document_are_clear_errors(tmp_path):
    store = SessionStore(tmp_path)
    with pytest.raises(KeyError):
        store.load("missing")

    (tmp_path / "bad.json").write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with pytest.raises(TypeError):
        store.load("bad")
