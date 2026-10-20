"""Small MCP client supporting modern stateless and legacy initialized servers."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .tools import Access, ResourceClaim, Tool, ToolEffect

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
        if not self._session_id:
            return
        request = urllib.request.Request(
            self.url,
            headers={**self.headers, "Mcp-Session-Id": self._session_id},
            method="DELETE",
        )
        try:
            urllib.request.urlopen(request, timeout=min(self.timeout, 5)).close()
        except (OSError, urllib.error.URLError, urllib.error.HTTPError):
            pass
        self._session_id = None

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
        transport_factory: Callable[[], McpTransport] | None = None,
        capabilities: frozenset[str] = frozenset({"mcp"}),
        reconnect: bool = True,
        circuit_failures: int = 3,
        circuit_cooldown: float = 30.0,
    ):
        if circuit_failures < 1 or circuit_cooldown < 0:
            raise ValueError("MCP circuit settings must be positive")
        self.name = name
        self.transport = transport
        self.protocol = protocol
        self.transport_factory = transport_factory
        self.capabilities = capabilities
        self.reconnect = reconnect
        self.circuit_failures = circuit_failures
        self.circuit_cooldown = circuit_cooldown
        self.connected = False
        self._replace_transport = False
        self._consecutive_failures = 0
        self._circuit_opened_at: float | None = None
        self._reconnect_lock = threading.Lock()

    def connect(self) -> None:
        if self.connected:
            return
        self._check_circuit()
        recovering = self._replace_transport
        if recovering:
            self._recreate_transport()
        try:
            if self.protocol != MODERN_PROTOCOL:
                self.transport.request(
                    "initialize",
                    {
                        "protocolVersion": self.protocol,
                        "capabilities": {},
                        "clientInfo": {"name": "forgeagent", "version": "0.3.0"},
                    },
                )
                self.transport.notify("notifications/initialized", {})
        except Exception:
            self._record_failure()
            self._replace_transport = True
            raise
        self.connected = True
        if not recovering:
            self._record_success()

    def list_tools(self) -> list[dict[str, Any]]:
        self.connect()
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            query = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", self._params(query))
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
        return self._request(
            "tools/call", self._params({"name": name, "arguments": arguments})
        )

    def health(self) -> dict[str, Any]:
        opened = self._circuit_opened_at
        return {
            "name": self.name,
            "connected": self.connected,
            "failures": self._consecutive_failures,
            "circuit_open": opened is not None
            and time.monotonic() - opened < self.circuit_cooldown,
        }

    def close(self) -> None:
        self.connected = False
        self.transport.close()

    def _params(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.protocol == MODERN_PROTOCOL:
            return {**params, "_meta": {"protocolVersion": self.protocol}}
        return params

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.connect()
        self._check_circuit()
        try:
            result = self.transport.request(method, params)
        except Exception:
            self._record_failure()
            self.connected = False
            self._replace_transport = bool(self.transport_factory and self.reconnect)
            raise
        self._record_success()
        return result

    def _recreate_transport(self) -> None:
        if self.transport_factory is None or not self.reconnect:
            return
        with self._reconnect_lock:
            if not self._replace_transport:
                return
            self.transport.close()
            self.transport = self.transport_factory()
            self._replace_transport = False

    def _check_circuit(self) -> None:
        if self._circuit_opened_at is None:
            return
        elapsed = time.monotonic() - self._circuit_opened_at
        if elapsed >= self.circuit_cooldown:
            self._circuit_opened_at = None
            self._consecutive_failures = 0
            return
        raise McpError(
            f"MCP server {self.name!r} circuit is open for "
            f"{self.circuit_cooldown - elapsed:.1f}s"
        )

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.circuit_failures:
            self._circuit_opened_at = time.monotonic()

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None


class McpTool(Tool):
    effect = ToolEffect.UNKNOWN
    parallel_safe = True

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

    def resources(self, arguments: dict[str, Any]) -> tuple[ResourceClaim, ...]:
        return (ResourceClaim(f"mcp-server:{self.client.name}", Access.WRITE),)


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
    reconnect: bool = True
    circuit_failures: int = 3
    circuit_cooldown: float = 30.0


class McpConnectionSet:
    def __init__(self, clients: list[McpClient], errors: list[dict[str, str]] | None = None):
        self.clients = clients
        self.errors = list(errors or [])

    def close(self) -> None:
        for client in self.clients:
            client.close()

    def refresh(self) -> tuple[list[McpTool], list[dict[str, Any]]]:
        tools: list[McpTool] = []
        health: list[dict[str, Any]] = list(self.errors)
        for client in self.clients:
            try:
                schemas = client.list_tools()
            except Exception as error:  # noqa: BLE001 - one MCP server is isolated
                health.append(
                    {"name": client.name, "status": "error", "error": str(error)}
                )
                continue
            for schema in schemas:
                remote_name = str(schema.get("name") or "")
                if remote_name:
                    tools.append(
                        McpTool(
                            client,
                            remote_name,
                            str(schema.get("description") or ""),
                            dict(schema.get("inputSchema") or {}),
                            capabilities=client.capabilities,
                        )
                    )
            health.append(
                {"name": client.name, "status": "healthy", "tools": len(schemas)}
            )
        return tools, health


def load_mcp_tools(
    path: str | Path,
    *,
    allowed_capabilities: frozenset[str] | None = None,
) -> tuple[list[McpTool], McpConnectionSet]:
    configs = load_mcp_config(path)
    tools: list[McpTool] = []
    clients: list[McpClient] = []
    errors: list[dict[str, str]] = []
    known_names: set[str] = set()
    for config in configs:
        client: McpClient | None = None
        try:
            capabilities = _server_capabilities(config)
            if allowed_capabilities is not None:
                missing = sorted(capabilities - allowed_capabilities)
                if missing:
                    raise PermissionError(
                        "capability admission rejected server; missing: " + ", ".join(missing)
                    )

            def transport_factory(config: McpServerConfig = config) -> McpTransport:
                return _make_transport(config)

            client = McpClient(
                config.name,
                transport_factory(),
                protocol=config.protocol,
                transport_factory=transport_factory,
                capabilities=capabilities,
                reconnect=config.reconnect,
                circuit_failures=config.circuit_failures,
                circuit_cooldown=config.circuit_cooldown,
            )
            server_tools: list[McpTool] = []
            server_names: set[str] = set()
            for schema in client.list_tools():
                remote_name = str(schema.get("name") or "")
                if not remote_name:
                    continue
                tool = McpTool(
                    client,
                    remote_name,
                    str(schema.get("description") or ""),
                    dict(schema.get("inputSchema") or {}),
                    capabilities=capabilities,
                )
                if tool.name in known_names or tool.name in server_names:
                    raise ValueError(f"duplicate normalized MCP Tool name: {tool.name}")
                server_names.add(tool.name)
                server_tools.append(tool)
        except Exception as error:  # noqa: BLE001 - isolate one broken server
            try:
                if client is not None:
                    client.close()
            except (AttributeError, OSError):
                pass
            errors.append({"name": config.name, "status": "error", "error": str(error)})
            continue
        known_names.update(server_names)
        tools.extend(server_tools)
        assert client is not None
        clients.append(client)
    return tools, McpConnectionSet(clients, errors)


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
                reconnect=bool(raw.get("reconnect", True)),
                circuit_failures=int(raw.get("circuitFailures") or 3),
                circuit_cooldown=float(raw.get("circuitCooldown") or 30.0),
            )
        )
    return configs


def _make_transport(config: McpServerConfig) -> McpTransport:
    if config.transport == "stdio":
        command = list(config.command)
        if not command:
            raise ValueError(f"MCP stdio server {config.name!r} requires command")
        if config.sandbox_image:
            command = _docker_stdio_command(config, command)
        return StdioTransport(command, env=config.env)
    if config.transport == "http":
        if not config.url:
            raise ValueError(f"MCP HTTP server {config.name!r} requires url")
        return HttpTransport(config.url, protocol=config.protocol, headers=config.headers)
    raise ValueError(f"unsupported MCP transport: {config.transport}")


def _server_capabilities(config: McpServerConfig) -> frozenset[str]:
    if config.transport == "http":
        return frozenset({"mcp", "network"})
    capabilities = {"mcp", "process"}
    if not config.sandbox_image or config.network != "none":
        capabilities.add("network")
    if not config.sandbox_image:
        capabilities.update({"filesystem_read", "filesystem_write"})
    return frozenset(capabilities)


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
