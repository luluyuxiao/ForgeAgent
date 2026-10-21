from __future__ import annotations

from pathlib import Path

from forgeagent.guidance import SkillRegistry

PROJECT_ROOT = Path(__file__).parents[1]


def _write_skill(root, name, description, body):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


def test_project_skill_overrides_global_and_activation_is_restorable(tmp_path):
    global_dir = tmp_path / "global"
    project_dir = tmp_path / "project"
    _write_skill(global_dir, "review", "global review", "Use the global workflow.")
    _write_skill(project_dir, "review", "project review", "Use project checks.")

    skills = SkillRegistry.discover(
        tmp_path,
        global_dir=global_dir,
        project_dir=project_dir,
    )

    assert "project review" in skills.catalog()
    loaded = skills.activate("review")
    assert loaded.scope == "project"
    assert "grants no Tool" in skills.render_active()
    skills.restore([])
    assert skills.active_names() == ()


def test_bundled_quality_skill_and_resources_are_discoverable(tmp_path):
    skills = SkillRegistry.discover(PROJECT_ROOT, global_dir=tmp_path / "global")

    assert "forgeagent-quality" in skills.names()
    loaded = skills.activate("forgeagent-quality")
    assert loaded.scope == "project"
    assert "scripts/run_quality_gate.py" in loaded.instructions

    root = loaded.path.parent
    assert (root / "scripts/run_quality_gate.py").is_file()
    assert (root / "references/failure-triage.md").is_file()
    assert (root / "assets/quality-report.md").is_file()
