"""Small MCP client supporting modern stateless and legacy initialized servers."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .tools import Tool, ToolEffect

MODERN_PROTOCOL = "2026-07-28"
LEGACY_PROTOCOL = "2025-11-25"


class McpError(RuntimeError):
    pass


class McpTransport(Protocol):
    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def notify(self, method: str, params: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


class StdioTransport:
    """JSON-RPC over one-message-per-line stdio."""

    def __init__(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        timeout: float = 30.0,
    ):
        if not command:
            raise ValueError("MCP stdio command must not be empty")
        merged_env = os.environ.copy()
        merged_env.update(env or {})
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            env=merged_env,
            bufsize=1,
        )
        self.timeout = timeout
        self._next_id = 0
        self._lock = threading.RLock()
        self._messages: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=20)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while True:
                try:
                    message = self._messages.get(timeout=self.timeout)
                except queue.Empty as error:
                    raise McpError(f"MCP request timed out: {method}") from error
                if isinstance(message, BaseException):
                    raise McpError(f"MCP stdio failed: {message}") from message
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise McpError(f"MCP {method} error: {message['error']}")
                result = message.get("result") or {}
                if not isinstance(result, dict):
                    raise McpError(f"MCP {method} returned a non-object result")
                return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            stderr = "".join(self._stderr).strip()
            raise McpError(f"MCP process is not running. {stderr}")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                if line.strip():
                    self._messages.put(json.loads(line))
        except Exception as error:  # noqa: BLE001 - reader forwards transport failures
            self._messages.put(error)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self._stderr.append(line)


class HttpTransport:
    def __init__(
        self,
        url: str,
        *,
        protocol: str = MODERN_PROTOCOL,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ):
        self.url = url
        self.protocol = protocol
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._next_id = 0
        self._session_id: str | None = None

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        response = self._post(message, method, params)
        if response.get("id") not in {None, request_id}:
            raise McpError(f"MCP HTTP response id mismatch for {method}")
        if "error" in response:
            raise McpError(f"MCP {method} error: {response['error']}")
        result = response.get("result") or {}
        if not isinstance(result, dict):
            raise McpError(f"MCP {method} returned a non-object result")
        return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params}, method, params)

    def close(self) -> None:
        return None

    def _post(
        self, message: dict[str, Any], method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self.protocol == MODERN_PROTOCOL:
            headers["Mcp-Method"] = method
            if method == "tools/call" and params.get("name"):
                headers["Mcp-Name"] = str(params["name"])
        request = urllib.request.Request(
            self.url,
            data=json.dumps(message).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self._session_id = response.headers.get("Mcp-Session-Id") or self._session_id
                body = response.read().decode("utf-8")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.URLError as error:
            raise McpError(f"MCP HTTP transport failed: {error}") from error
        if "text/event-stream" in content_type:
            data_lines = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
            if not data_lines:
                raise McpError("MCP SSE response contained no data event")
            body = data_lines[-1]
        decoded = json.loads(body or "{}")
        if not isinstance(decoded, dict):
            raise McpError("MCP HTTP response was not a JSON object")
        return decoded


class McpClient:
    def __init__(
        self,
        name: str,
        transport: McpTransport,
        *,
        protocol: str = LEGACY_PROTOCOL,
    ):
        self.name = name
        self.transport = transport
        self.protocol = protocol
        self.connected = False

    def connect(self) -> None:
        if self.connected:
            return
        if self.protocol != MODERN_PROTOCOL:
            self.transport.request(
                "initialize",
                {
                    "protocolVersion": self.protocol,
                    "capabilities": {},
                    "clientInfo": {"name": "forgeagent", "version": "0.2.0"},
                },
            )
            self.transport.notify("notifications/initialized", {})
        self.connected = True

    def list_tools(self) -> list[dict[str, Any]]:
        self.connect()
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            query = {"cursor": cursor} if cursor else {}
            result = self.transport.request("tools/list", self._params(query))
            tools = result.get("tools") or []
            if not isinstance(tools, list):
                raise McpError("MCP tools/list did not return a list")
            collected.extend(item for item in tools if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                return collected
            cursor = str(next_cursor)
            if cursor in seen:
                raise McpError("MCP tools/list returned a repeated pagination cursor")
            seen.add(cursor)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.connect()
        return self.transport.request(
            "tools/call", self._params({"name": name, "arguments": arguments})
        )

    def close(self) -> None:
        self.transport.close()

    def _params(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.protocol == MODERN_PROTOCOL:
            return {**params, "_meta": {"protocolVersion": self.protocol}}
        return params


class McpTool(Tool):
    effect = ToolEffect.UNKNOWN
    parallel_safe = False

    def __init__(
        self,
        client: McpClient,
        remote_name: str,
        description: str,
        input_schema: dict[str, Any],
        *,
        capabilities: frozenset[str],
    ):
        self.client = client
        self.remote_name = remote_name
        self.name = _tool_name(client.name, remote_name)
        self.description = f"MCP {client.name}/{remote_name}: {description}"
        self.parameters = input_schema or {"type": "object", "properties": {}}
        self.capabilities = capabilities

    def execute(self, **arguments: Any) -> str:
        result = self.client.call_tool(self.remote_name, arguments)
        if result.get("isError"):
            return "Error from MCP Tool: " + _render_content(result.get("content"))
        return _render_content(result.get("content"), structured=result.get("structuredContent"))


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: str
    protocol: str
    command: tuple[str, ...] = ()
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    sandbox_image: str | None = None
    network: str = "none"


class McpConnectionSet:
    def __init__(self, clients: list[McpClient]):
        self.clients = clients

    def close(self) -> None:
        for client in self.clients:
            client.close()


def load_mcp_tools(path: str | Path) -> tuple[list[McpTool], McpConnectionSet]:
    configs = load_mcp_config(path)
    tools: list[McpTool] = []
    clients: list[McpClient] = []
    try:
        for config in configs:
            if config.transport == "stdio":
                command = list(config.command)
                if config.sandbox_image:
                    command = _docker_stdio_command(config, command)
                transport: McpTransport = StdioTransport(command, env=config.env)
                capabilities = {"mcp", "process"}
                if not config.sandbox_image or config.network != "none":
                    capabilities.add("network")
                if not config.sandbox_image:
                    capabilities.update({"filesystem_read", "filesystem_write"})
                capabilities = frozenset(capabilities)
            elif config.transport == "http":
                transport = HttpTransport(
                    config.url,
                    protocol=config.protocol,
                    headers=config.headers,
                )
                capabilities = frozenset({"mcp", "network"})
            else:
                raise ValueError(f"unsupported MCP transport: {config.transport}")
            client = McpClient(config.name, transport, protocol=config.protocol)
            clients.append(client)
            for schema in client.list_tools():
                remote_name = str(schema.get("name") or "")
                if not remote_name:
                    continue
                tools.append(
                    McpTool(
                        client,
                        remote_name,
                        str(schema.get("description") or ""),
                        dict(schema.get("inputSchema") or {}),
                        capabilities=capabilities,
                    )
                )
    except Exception:
        McpConnectionSet(clients).close()
        raise
    return tools, McpConnectionSet(clients)


def load_mcp_config(path: str | Path) -> list[McpServerConfig]:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    servers = data.get("servers") or data.get("mcpServers") or {}
    if not isinstance(servers, dict):
        raise TypeError("MCP config requires an object named 'servers' or 'mcpServers'")
    configs = []
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            raise TypeError(f"MCP server {name!r} must be an object")
        command = raw.get("command")
        args = raw.get("args") or []
        transport = str(raw.get("transport") or ("stdio" if command else "http"))
        protocol = str(
            raw.get("protocol")
            or (LEGACY_PROTOCOL if transport == "stdio" else MODERN_PROTOCOL)
        )
        cmd = ([str(command)] if command else []) + [str(item) for item in args]
        configs.append(
            McpServerConfig(
                name=str(name),
                transport=transport,
                protocol=protocol,
                command=tuple(cmd),
                url=str(raw.get("url") or ""),
                env={
                    str(k): os.path.expandvars(str(v))
                    for k, v in (raw.get("env") or {}).items()
                },
                headers={
                    str(k): os.path.expandvars(str(v))
                    for k, v in (raw.get("headers") or {}).items()
                },
                sandbox_image=(str(raw["sandboxImage"]) if raw.get("sandboxImage") else None),
                network=str(raw.get("network") or "none"),
            )
        )
    return configs


def _docker_stdio_command(config: McpServerConfig, command: list[str]) -> list[str]:
    if config.network not in {"none", "bridge"}:
        raise ValueError(f"invalid MCP Docker network: {config.network}")
    prefix = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--pull",
        "never",
        "--read-only",
        "--network",
        config.network,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--memory",
        "1g",
        "--cpus",
        "1",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
    ]
    for key in config.env:
        prefix.extend(["--env", key])
    return [*prefix, str(config.sandbox_image), *command]


def _tool_name(server: str, remote: str) -> str:
    clean = lambda value: re.sub(r"[^A-Za-z0-9_-]", "_", value)
    return f"mcp__{clean(server)}__{clean(remote)}"[:64]


def _render_content(content: Any, *, structured: Any = None) -> str:
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif item is not None:
                parts.append(json.dumps(item, ensure_ascii=False, default=str))
    elif content is not None:
        parts.append(str(content))
    if structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False, indent=2, default=str))
    return "\n".join(part for part in parts if part) or "[empty MCP Tool Result]"
