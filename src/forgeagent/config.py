"""Environment-backed runtime configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class ProviderRoute:
    model: str
    base_url: str | None = None
    api_key_env: str = "FORGE_API_KEY"


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    base_url: str | None
    max_rounds: int = 24
    context_tokens: int = 128_000
    max_output_tokens: int = 8192
    session_dir: Path = Path.home() / ".forgeagent" / "sessions"
    fallback_routes: tuple[ProviderRoute, ...] = ()
    sandbox: str = "local"
    sandbox_image: str = "forgeagent-sandbox:latest"
    sandbox_network: str = "none"
    mcp_config: Path | None = None
    trace_path: Path | None = None
    llm_summary: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(override=False)
        fallback_routes = _parse_fallbacks(os.getenv("FORGE_FALLBACKS", ""))
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
            fallback_routes=fallback_routes,
            sandbox=os.getenv("FORGE_SANDBOX", "local"),
            sandbox_image=os.getenv("FORGE_SANDBOX_IMAGE", "forgeagent-sandbox:latest"),
            sandbox_network=os.getenv("FORGE_SANDBOX_NETWORK", "none"),
            mcp_config=(
                Path(os.environ["FORGE_MCP_CONFIG"]).expanduser()
                if os.getenv("FORGE_MCP_CONFIG")
                else None
            ),
            trace_path=(
                Path(os.environ["FORGE_TRACE"]).expanduser()
                if os.getenv("FORGE_TRACE")
                else None
            ),
            llm_summary=os.getenv("FORGE_LLM_SUMMARY", "1").lower()
            not in {"0", "false", "no"},
        )


def _parse_fallbacks(raw: str) -> tuple[ProviderRoute, ...]:
    if not raw.strip():
        return ()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise TypeError("FORGE_FALLBACKS must be a JSON array")
    routes = []
    for item in data:
        if not isinstance(item, dict) or not item.get("model"):
            raise ValueError("each FORGE_FALLBACKS item requires model")
        routes.append(
            ProviderRoute(
                model=str(item["model"]),
                base_url=str(item["base_url"]) if item.get("base_url") else None,
                api_key_env=str(item.get("api_key_env") or "FORGE_API_KEY"),
            )
        )
    return tuple(routes)
