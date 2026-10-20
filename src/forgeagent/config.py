"""Environment-backed runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    base_url: str | None
    max_rounds: int = 24
    context_tokens: int = 128_000
    max_output_tokens: int = 8192
    session_dir: Path = Path.home() / ".forgeagent" / "sessions"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(override=False)
        return cls(
            api_key=os.getenv("FORGE_API_KEY", ""),
            model=os.getenv("FORGE_MODEL", ""),
            base_url=os.getenv("FORGE_BASE_URL") or None,
            max_rounds=int(os.getenv("FORGE_MAX_ROUNDS", "24")),
            context_tokens=int(os.getenv("FORGE_CONTEXT_TOKENS", "128000")),
            max_output_tokens=int(os.getenv("FORGE_MAX_OUTPUT_TOKENS", "8192")),
            session_dir=Path(
                os.getenv("FORGE_SESSION_DIR", str(Path.home() / ".forgeagent" / "sessions"))
            ).expanduser(),
        )

