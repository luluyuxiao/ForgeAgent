from __future__ import annotations

from forgeagent.guidance import load_repository_instructions


def test_loads_root_instructions_and_indexes_scoped_files(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Run focused tests.", encoding="utf-8")
    nested = tmp_path / "packages" / "api"
    nested.mkdir(parents=True)
    (nested / "AGENTS.md").write_text("API-only rule", encoding="utf-8")
    ignored = tmp_path / ".git"
    ignored.mkdir()
    (ignored / "AGENTS.md").write_text("ignore me", encoding="utf-8")

    instructions = load_repository_instructions(tmp_path)

    assert "Run focused tests." in instructions
    assert "packages/api/AGENTS.md" in instructions
    assert "API-only rule" not in instructions
    assert ".git/AGENTS.md" not in instructions
