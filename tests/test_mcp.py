from __future__ import annotations

import json

from forgeagent.mcp import LEGACY_PROTOCOL, MODERN_PROTOCOL, McpClient, McpTool, load_mcp_config


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
