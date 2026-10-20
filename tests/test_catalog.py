from __future__ import annotations

from forgeagent.catalog import ToolCatalog, install_catalog_tools
from forgeagent.registry import ToolRegistry
from forgeagent.tools import build_builtin_tools


def test_catalog_hides_searches_and_activates_tools(tmp_path):
    registry = ToolRegistry(build_builtin_tools(tmp_path))
    catalog = ToolCatalog(registry, active={"now"})
    install_catalog_tools(catalog)

    assert "read_file" not in catalog.names()
    assert catalog.search("UTF-8 text file")[0]["name"] == "read_file"
    assert catalog.activate(["read_file", "missing"]) == ["read_file"]
    assert "read_file" in catalog.names()
    assert {row["function"]["name"] for row in catalog.schemas()} >= {
        "now",
        "read_file",
        "search_tools",
        "activate_tools",
    }
