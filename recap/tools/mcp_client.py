"""MCP client used by the tool gateway (RECAP's V1 implementation of ToolTransport).

Where it sits in the architecture:

    ToolGateway -> [McpToolClient] --(MCP, JSON-RPC over stdio)--> MCP server -> TargetEnvironment

It wraps the official SDK's `mcp.Client` and translates between MCP types and RECAP
contracts: `tools/list` becomes a list of DiscoveredTool and `tools/call` becomes a
RawToolResult. It knows nothing about safety: the gateway decides what may be called and
treats everything the server says about its tools (descriptions, schemas, hints) as untrusted.

`call_tool` never raises for tool-level problems (bad arguments, unknown tool, environment
error, timeout, broken connection). They come back as `RawToolResult(ok=False, error=...)`, so
the gateway records every outcome as data instead of crashing the incident run.
"""

from __future__ import annotations

import asyncio
import sys
from types import TracebackType

from mcp import Client, MCPError, StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp.types import REQUEST_TIMEOUT, CallToolResult, TextContent, Tool

from recap.core.config import REPO_ROOT, EnvironmentConfig
from recap.core.models import DiscoveredTool, RawToolResult

# Both transports use the classic `initialize` handshake, so the in-process tests speak exactly
# the protocol version that the stdio runtime speaks.
_CONNECT_MODE = "legacy"
_MAX_ERROR_CHARS = 300


class McpToolClient:
    """Async context manager that talks MCP to one tool server and returns RECAP contracts."""

    def __init__(self, server: StdioServerParameters | MCPServer, *, transport_name: str) -> None:
        self.server = server  # how to reach the server: a subprocess command or a server object
        self.transport_name = transport_name
        self.server_info: dict[str, str] = {}  # name, version, protocol_version once connected
        self.calls_made = 0
        self._client: Client | None = None

    @classmethod
    def stdio(cls, environment: EnvironmentConfig, run_id: str) -> McpToolClient:
        """Client that starts `python -m recap.tools.mcp_server` as a subprocess (the runtime)."""
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "recap.tools.mcp_server",
                "--env-config",
                environment.model_dump_json(),
                "--run-id",
                run_id,
            ],
            cwd=str(REPO_ROOT),
            # `env` is deliberately left unset: the SDK then passes only a small allowlist of
            # variables (PATH, HOME, USER, ...), so API keys never reach the tool server.
        )
        return cls(params, transport_name="mcp-stdio")

    @classmethod
    def in_process(cls, server: MCPServer) -> McpToolClient:
        """Client for a server object in this process; tests use it, the runtime uses stdio.

        It is still real MCP: JSON-RPC messages and the initialize handshake, only carried over
        in-memory streams instead of a subprocess pipe.
        """
        return cls(server, transport_name="mcp-in-memory")

    async def __aenter__(self) -> McpToolClient:
        """Connect, run the MCP initialize handshake and record which server answered."""
        client = Client(self.server, mode=_CONNECT_MODE)
        await client.__aenter__()
        self._client = client
        info = client.server_info  # the handshake always carries it; None only for anonymous
        self.server_info = {
            "name": info.name if info else "unknown",
            "version": info.version if info else "unknown",
            "protocol_version": client.protocol_version,
        }
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the session; for stdio this also shuts the server subprocess down."""
        client, self._client = self._client, None
        if client is not None:
            # Close normally even if the caller's block failed: handing the caller's exception
            # to the SDK's task group would re-raise it wrapped in an ExceptionGroup, and the
            # controller would no longer recognise e.g. BudgetExhausted. Returning None lets
            # the original exception propagate unchanged.
            await client.__aexit__(None, None, None)

    async def list_tools(self) -> list[DiscoveredTool]:
        """Tools the server advertises. Every field is an untrusted claim made by the server."""
        result = await self._connected().list_tools()
        return [_discovered(tool) for tool in result.tools]

    async def call_tool(
        self, name: str, args: dict[str, object], timeout_seconds: float
    ) -> RawToolResult:
        """Call one tool with a hard timeout. Tool-level failures come back as ok=False."""
        client = self._connected()
        self.calls_made += 1
        try:
            # read_timeout_seconds lets the SDK cancel the request on the server side, and
            # wait_for bounds the whole call no matter where it hangs. Whichever fires first wins.
            result = await asyncio.wait_for(
                client.call_tool(name, dict(args), read_timeout_seconds=timeout_seconds),
                timeout=timeout_seconds,
            )
        except Exception as exc:  # e.g. timeout, server process gone, invalid reply
            return RawToolResult(ok=False, error=_failure_reason(exc, timeout_seconds))
        return _raw_result(result)

    def _connected(self) -> Client:
        """The live SDK client; using this object outside `async with` is a programming error."""
        if self._client is None:
            raise RuntimeError("McpToolClient is not connected; use it inside 'async with'")
        return self._client


def _discovered(tool: Tool) -> DiscoveredTool:
    """Translate an MCP tool listing into RECAP's DiscoveredTool (still untrusted data)."""
    hints = tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}
    return DiscoveredTool(
        name=tool.name,
        description=tool.description or "",
        input_schema=tool.input_schema,
        output_schema=tool.output_schema,
        annotations=hints,
    )


def _raw_result(result: CallToolResult) -> RawToolResult:
    """Translate an MCP CallToolResult into RECAP's RawToolResult."""
    text = "\n".join(block.text for block in result.content if isinstance(block, TextContent))
    if result.is_error:
        # Bad arguments, unknown tools and environment failures all arrive as error text.
        return RawToolResult(ok=False, error=text or "the tool reported an error")
    structured = result.structured_content
    return RawToolResult(
        ok=True, structured=structured if isinstance(structured, dict) else None, text=text
    )


def _failure_reason(exc: Exception, timeout_seconds: float) -> str:
    """Short one-line reason for a failed call. Both kinds of timeout read the same."""
    if isinstance(exc, TimeoutError) or (isinstance(exc, MCPError) and exc.code == REQUEST_TIMEOUT):
        return f"timeout after {timeout_seconds}s"
    message = " ".join(str(exc).split())  # collapse newlines so the reason stays on one line
    return f"{type(exc).__name__}: {message}"[:_MAX_ERROR_CHARS]
