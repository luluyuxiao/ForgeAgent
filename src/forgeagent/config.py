"""Environment-backed runtime configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .contracts import Usage


class BudgetExceededError(RuntimeError):
    """Raised before a model request would exceed the configured cost ceiling."""


@dataclass(frozen=True)
class CostBudget:
    """Estimate request cost from provider token usage and explicit model prices."""

    max_usd: float = 0.0
    input_per_million: float = 0.0
    output_per_million: float = 0.0

    def __post_init__(self) -> None:
        if min(self.max_usd, self.input_per_million, self.output_per_million) < 0:
            raise ValueError("cost budget and prices must not be negative")
        if self.max_usd > 0 and self.input_per_million <= 0 and self.output_per_million <= 0:
            raise ValueError("an enabled cost budget requires input or output pricing")

    @property
    def enabled(self) -> bool:
        return self.max_usd > 0

    def cost(self, usage: Usage) -> float:
        return (
            usage.prompt_tokens * self.input_per_million
            + usage.completion_tokens * self.output_per_million
        ) / 1_000_000

    def ensure_available(
        self,
        current: Usage,
        *,
        next_prompt_tokens: int,
        output_reserve: int,
    ) -> float:
        projected = self.cost(
            Usage(
                current.prompt_tokens + max(0, next_prompt_tokens),
                current.completion_tokens + max(0, output_reserve),
            )
        )
        if self.enabled and projected > self.max_usd:
            raise BudgetExceededError(
                f"projected cost ${projected:.6f} exceeds budget ${self.max_usd:.6f}"
            )
        return projected


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
    context_input_ratio: float = 0.85
    max_output_tokens: int = 8192
    session_dir: Path = Path.home() / ".forgeagent" / "sessions"
    fallback_routes: tuple[ProviderRoute, ...] = ()
    sandbox: str = "local"
    sandbox_image: str = "forgeagent-sandbox:latest"
    sandbox_network: str = "none"
    mcp_config: Path | None = None
    trace_path: Path | None = None
    llm_summary: bool = True
    hooks_config: Path | None = None
    max_cost_usd: float = 0.0
    input_price_per_million: float = 0.0
    output_price_per_million: float = 0.0
    tool_embedding_model: str | None = None
    tool_embedding_base_url: str | None = None
    tool_embedding_api_key: str = ""

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
            context_input_ratio=float(os.getenv("FORGE_CONTEXT_INPUT_RATIO", "0.85")),
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
            hooks_config=(
                Path(os.environ["FORGE_HOOKS_CONFIG"]).expanduser()
                if os.getenv("FORGE_HOOKS_CONFIG")
                else None
            ),
            max_cost_usd=float(os.getenv("FORGE_MAX_COST_USD", "0")),
            input_price_per_million=float(
                os.getenv("FORGE_INPUT_PRICE_PER_MILLION", "0")
            ),
            output_price_per_million=float(
                os.getenv("FORGE_OUTPUT_PRICE_PER_MILLION", "0")
            ),
            tool_embedding_model=os.getenv("FORGE_TOOL_EMBEDDING_MODEL") or None,
            tool_embedding_base_url=(
                os.getenv("FORGE_TOOL_EMBEDDING_BASE_URL") or None
            ),
            tool_embedding_api_key=os.getenv("FORGE_TOOL_EMBEDDING_API_KEY", ""),
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
