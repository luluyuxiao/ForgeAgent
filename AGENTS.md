# ForgeAgent repository instructions

- Support Python 3.10 through 3.13 and keep public APIs typed.
- Preserve the separation between model transport, policy, scheduling, and execution.
- Treat prompts and Skills as guidance, never as authorization or sandbox boundaries.
- New Tools must declare their effect, capabilities, resource claims, and JSON Schema.
- Keep file and command execution confined to the selected workspace or sandbox.
- Add focused offline tests for behavior changes.
- Before finishing, run `pytest -q`, `ruff check src tests examples`, and
  `python -m compileall -q src tests examples`.
