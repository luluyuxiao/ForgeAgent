from __future__ import annotations

from forgeagent.guidance import SkillRegistry


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
