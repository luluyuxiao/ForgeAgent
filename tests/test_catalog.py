from __future__ import annotations

import json

from forgeagent.tools import (
    Tool,
    ToolCatalog,
    ToolEffect,
    ToolRegistry,
    build_builtin_tools,
    install_catalog_tools,
)


class _NamedTool(Tool):
    def __init__(
        self,
        name: str,
        description: str,
        *,
        effect: ToolEffect = ToolEffect.READ,
        capabilities: frozenset[str] = frozenset(),
    ):
        self.name = name
        self.description = description
        self.effect = effect
        self.capabilities = capabilities

    def execute(self) -> str:
        return "ok"


class _SemanticEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            lowered = text.casefold()
            related = "knowledge" in lowered or "manual" in lowered
            vectors.append([1.0, 0.0] if related else [0.0, 1.0])
        return vectors


class _FailingEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("provider unavailable")


def test_catalog_hides_searches_and_activates_tools(tmp_path):
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    catalog = ToolCatalog(registry, active={"now"})
    install_catalog_tools(catalog)

    assert "read_file" not in catalog.names()
    assert catalog.search("read UTF-8 text file")[0]["name"] == "read_file"
    assert catalog.activate(["read_file", "missing"]) == ["read_file"]
    assert "read_file" in catalog.names()
    assert {row["function"]["name"] for row in catalog.schemas()} >= {
        "now",
        "read_file",
        "search_tools",
        "activate_tools",
    }


def test_catalog_bm25_ranks_name_and_description_matches():
    registry = ToolRegistry(
        [
            _NamedTool("search_code", "Search repository source"),
            _NamedTool("search_web", "Search public pages"),
            _NamedTool("clock", "Return the current time"),
        ]
    )
    catalog = ToolCatalog(registry, active=set())

    rows = catalog.search("repository code", limit=2)

    assert rows[0]["name"] == "search_code"
    assert rows[0]["matched_by"] == ["bm25"]
    assert len(rows) <= 2


def test_catalog_combines_embedding_recall_with_bm25():
    registry = ToolRegistry(
        [
            _NamedTool("package_reference", "Read package manuals"),
            _NamedTool("weather", "Read the forecast"),
        ]
    )
    catalog = ToolCatalog(registry, active=set(), embedder=_SemanticEmbedder())

    rows = catalog.search("knowledge lookup", limit=1)

    assert rows[0]["name"] == "package_reference"
    assert rows[0]["matched_by"] == ["embedding"]
    assert rows[0]["semantic_status"] == "used"


def test_catalog_falls_back_to_bm25_when_embedding_fails():
    registry = ToolRegistry([_NamedTool("search_code", "Search repository source")])
    catalog = ToolCatalog(registry, active=set(), embedder=_FailingEmbedder())

    rows = catalog.search("repository", limit=1)

    assert rows[0]["name"] == "search_code"
    assert rows[0]["matched_by"] == ["bm25"]
    assert rows[0]["semantic_status"] == "fallback"


def test_catalog_filters_candidates_by_capability_permission_and_mode():
    registry = ToolRegistry(
        [
            _NamedTool(
                "read_source",
                "Inspect project source",
                capabilities=frozenset({"filesystem_read"}),
            ),
            _NamedTool(
                "write_source",
                "Modify project source",
                effect=ToolEffect.WRITE,
                capabilities=frozenset({"filesystem_write"}),
            ),
            _NamedTool(
                "remote_source",
                "Download project source",
                effect=ToolEffect.NETWORK,
                capabilities=frozenset({"network"}),
            ),
        ]
    )
    catalog = ToolCatalog(
        registry,
        active=set(),
        allowed_capabilities=frozenset({"filesystem_read", "filesystem_write"}),
        interactive=True,
    )

    assert {row["name"] for row in catalog.search("source")} == {
        "read_source",
        "write_source",
    }
    catalog.set_planning(True)
    assert [row["name"] for row in catalog.search("source")] == ["read_source"]

    catalog.set_planning(False)
    catalog.interactive = False
    assert [row["name"] for row in catalog.search("source")] == ["read_source"]


def test_activate_tools_reports_tools_filtered_by_runtime_access():
    registry = ToolRegistry(
        [
            _NamedTool(
                "remote_docs",
                "Query remote documentation",
                effect=ToolEffect.NETWORK,
                capabilities=frozenset({"network"}),
            )
        ]
    )
    catalog = ToolCatalog(registry, active=set(), interactive=False, allow_all=False)
    install_catalog_tools(catalog)

    result = json.loads(registry.require("activate_tools").execute(names=["remote_docs"]))

    assert result["activated"] == []
    assert "requires an interactive approver" in result["denied"]["remote_docs"]
    assert not catalog.is_active("remote_docs")


def test_embedding_search_declares_network_effect_and_capability():
    catalog = ToolCatalog(ToolRegistry(), active=set(), embedder=_SemanticEmbedder())
    install_catalog_tools(catalog)

    search = catalog.registry.require("search_tools")

    assert search.effect is ToolEffect.NETWORK
    assert search.capabilities == frozenset({"network"})
