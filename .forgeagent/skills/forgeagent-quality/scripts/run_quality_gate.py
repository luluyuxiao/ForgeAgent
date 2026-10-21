"""Run ForgeAgent's offline quality checks and emit a concise result."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Check:
    name: str
    arguments: tuple[str, ...]


def _checks() -> tuple[Check, ...]:
    python = sys.executable
    skill_scripts = ".forgeagent/skills/forgeagent-quality/scripts"
    return (
        Check("tests", (python, "-m", "pytest", "-q")),
        Check(
            "lint",
            (python, "-m", "ruff", "check", "src", "tests", "examples", skill_scripts),
        ),
        Check(
            "compile",
            (
                python,
                "-m",
                "compileall",
                "-q",
                "src",
                "tests",
                "examples",
                skill_scripts,
            ),
        ),
    )


def run(root: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for check in _checks():
        try:
            completed = subprocess.run(
                check.arguments,
                cwd=root,
                text=True,
                capture_output=True,
                timeout=600,
                check=False,
            )
            results.append(
                {
                    "name": check.name,
                    "command": list(check.arguments),
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout.rstrip(),
                    "stderr": completed.stderr.rstrip(),
                }
            )
        except subprocess.TimeoutExpired:
            results.append(
                {
                    "name": check.name,
                    "command": list(check.arguments),
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "timed out after 600 seconds",
                }
            )
    return results


def _render(results: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for result in results:
        status = "PASS" if result["exit_code"] == 0 else "FAIL"
        sections.append(f"[{status}] {result['name']} (exit={result['exit_code']})")
        for stream in ("stdout", "stderr"):
            content = str(result[stream]).strip()
            if content:
                sections.append(f"{stream}:\n{content}")
    return "\n".join(sections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[4],
        help="repository root; defaults to the root containing this project Skill",
    )
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"repository root is not a directory: {root}")

    results = run(root)
    if args.json:
        print(json.dumps({"passed": all(row["exit_code"] == 0 for row in results), "checks": results}))
    else:
        print(_render(results))
    return 0 if all(row["exit_code"] == 0 for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
