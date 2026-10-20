from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from forgeagent.mcp import (
    LEGACY_PROTOCOL,
    MODERN_PROTOCOL,
    McpClient,
    McpError,
    McpTool,
    StdioTransport,
    load_mcp_config,
    load_mcp_tools,
)


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.notifications = []

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "weather.current",
                        "description": "weather",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    }
                ]
            }
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "sunny"}]}
        return {}

    def notify(self, method, params):
        self.notifications.append((method, params))

    def close(self):
        return None


def test_legacy_client_initializes_before_listing():
    transport = FakeTransport()
    client = McpClient("demo", transport, protocol=LEGACY_PROTOCOL)

    assert client.list_tools()[0]["name"] == "weather.current"
    assert [item[0] for item in transport.calls] == ["initialize", "tools/list"]
    assert transport.notifications[0][0] == "notifications/initialized"


def test_modern_client_is_stateless_and_tool_wraps_schema():
    transport = FakeTransport()
    client = McpClient("demo", transport, protocol=MODERN_PROTOCOL)
    schema = client.list_tools()[0]
    tool = McpTool(
        client,
        schema["name"],
        schema["description"],
        schema["inputSchema"],
        capabilities=frozenset({"mcp", "network"}),
    )

    assert transport.calls[0][0] == "tools/list"
    assert transport.calls[0][1]["_meta"]["protocolVersion"] == MODERN_PROTOCOL
    assert tool.name == "mcp__demo__weather_current"
    assert tool.execute(city="Shanghai") == "sunny"


def test_common_mcp_config_shape_is_supported(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "weather": {"command": "python", "args": ["server.py"]},
                    "remote": {"url": "https://example.test/mcp", "transport": "http"},
                }
            }
        ),
        encoding="utf-8",
    )

    configs = load_mcp_config(path)

    assert configs[0].protocol == LEGACY_PROTOCOL
    assert configs[1].protocol == MODERN_PROTOCOL


class FailingCallTransport(FakeTransport):
    def request(self, method, params):
        self.calls.append((method, params))
        raise McpError("connection lost")


def test_failed_tool_call_is_not_replayed_and_next_call_reconnects():
    failed = FailingCallTransport()
    healthy = FakeTransport()
    client = McpClient(
        "demo",
        failed,
        protocol=MODERN_PROTOCOL,
        transport_factory=lambda: healthy,
    )

    with pytest.raises(McpError):
        client.call_tool("weather.current", {"city": "Shanghai"})
    result = client.call_tool("weather.current", {"city": "Shanghai"})

    assert len(failed.calls) == 1
    assert result["content"][0]["text"] == "sunny"


def test_circuit_opens_after_repeated_failures():
    created = []

    def factory():
        transport = FailingCallTransport()
        created.append(transport)
        return transport

    first = factory()
    client = McpClient(
        "demo",
        first,
        protocol=MODERN_PROTOCOL,
        transport_factory=factory,
        circuit_failures=2,
        circuit_cooldown=60,
    )

    with pytest.raises(McpError):
        client.call_tool("x", {})
    with pytest.raises(McpError):
        client.call_tool("x", {})
    with pytest.raises(McpError, match="circuit is open"):
        client.call_tool("x", {})


def test_mcp_calls_serialize_per_server_only():
    one = McpClient("one", FakeTransport(), protocol=MODERN_PROTOCOL)
    two = McpClient("two", FakeTransport(), protocol=MODERN_PROTOCOL)
    first = McpTool(one, "a", "", {}, capabilities=frozenset({"mcp"}))
    same = McpTool(one, "b", "", {}, capabilities=frozenset({"mcp"}))
    other = McpTool(two, "a", "", {}, capabilities=frozenset({"mcp"}))

    assert first.resources({})[0].conflicts_with(same.resources({})[0])
    assert not first.resources({})[0].conflicts_with(other.resources({})[0])


def test_broken_server_is_isolated_during_startup(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"servers": {"broken": {"transport": "unsupported"}}}),
        encoding="utf-8",
    )

    tools, connections = load_mcp_tools(path)

    assert tools == []
    assert connections.errors[0]["name"] == "broken"


def test_local_project_inspector_over_stdio(tmp_path):
    (tmp_path / "app.py").write_text("print('hello')\n# TODO: add CLI\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("def test_app():\n    pass\n", encoding="utf-8")
    server = Path(__file__).parents[1] / "examples" / "local_mcp_server.py"
    transport = StdioTransport([sys.executable, str(server), str(tmp_path)], timeout=5)
    client = McpClient("project-inspector", transport, protocol=LEGACY_PROTOCOL)

    try:
        schemas = client.list_tools()
        summary = client.call_tool("project_summary", {})
        markers = client.call_tool("find_markers", {"marker": "TODO"})
    finally:
        client.close()

    assert {schema["name"] for schema in schemas} == {"project_summary", "find_markers"}
    summary_data = json.loads(summary["content"][0]["text"])
    marker_data = json.loads(markers["content"][0]["text"])
    assert summary_data["files"] == 2
    assert summary_data["testFiles"] == 1
    assert marker_data["matches"][0]["path"] == "app.py"


def test_local_project_inspector_rejects_workspace_escape(tmp_path):
    server = Path(__file__).parents[1] / "examples" / "local_mcp_server.py"
    transport = StdioTransport([sys.executable, str(server), str(tmp_path)], timeout=5)
    client = McpClient("project-inspector", transport, protocol=LEGACY_PROTOCOL)

    try:
        result = client.call_tool("find_markers", {"path": ".."})
    finally:
        client.close()

    assert result["isError"] is True
    assert "escapes workspace" in result["content"][0]["text"]
