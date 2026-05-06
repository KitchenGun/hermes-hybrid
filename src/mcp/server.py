"""Minimal MCP server implementation.

We intentionally skip the official Python MCP SDK for now:

* The Phase 3 goal is "prove the orchestrator can answer MCP-shaped
  requests" — not to ship a production MCP server. A hand-rolled
  JSON-RPC 2.0 handler is ~120 lines and has no new dependency, so the
  supply-chain cost is zero.
* The surface is tiny (3 methods: ``initialize``, ``tools/list``,
  ``tools/call``). Writing it by hand keeps the blast radius small
  should the MCP spec shift between now and Phase 4.

When Phase 4 expands the surface (resources, prompts, sampling,
subscriptions) this module is the natural place to swap in the official
SDK. The public surface (``HybridMCPServer.handle_request`` +
``run_stdio``) stays stable.

## Protocol summary

Requests / responses follow JSON-RPC 2.0:

.. code-block:: json

    { "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {...} }

    { "jsonrpc": "2.0", "id": 1, "result": {...} }

Errors use the standard error object with MCP-appropriate codes
(``-32601`` method not found, ``-32602`` invalid params, ``-32000``
implementation-defined server error).

## Tool exposed

Only one for Phase 3: ``hybrid.handle``. Takes ``{user_message, user_id,
}``; returns ``{response, handled_by, tier, degraded}``. Enough
to verify the wiring end-to-end. Phase 4 will add ``hybrid.memo.*`` and
``hybrid.status`` once the shape stabilizes.
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

from src.obs import get_logger
from src.orchestrator import Orchestrator

log = get_logger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "hermes-hybrid"
SERVER_VERSION = "0.1.0"


class MCPError(Exception):
    """Raised to return a JSON-RPC error with a specific code."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.data = data
        super().__init__(message)


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    input_schema: dict[str, Any]


_TOOLS: list[_Tool] = [
    _Tool(
        name="hybrid.handle",
        description=(
            "Route a message through the hybrid orchestrator. "
            "Returns the final response, handled_by tag, tier reached, "
            "and degraded flag."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "user_message": {"type": "string", "description": "Message text."},
                "user_id": {"type": "string", "description": "Stable user id."},
            },
            "required": ["user_message", "user_id"],
        },
    ),
]


class HybridMCPServer:
    """JSON-RPC 2.0 request handler for MCP protocol.

    The object is transport-agnostic — call :meth:`handle_request` with
    a parsed dict and get a response dict back. :func:`run_stdio` wraps
    this with the line-delimited JSON framing used by MCP clients over
    stdio.
    """

    def __init__(self, orchestrator: Orchestrator):
        self.orchestrator = orchestrator
        self._initialized = False

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Process a single JSON-RPC request.

        Returns a response dict, or ``None`` for notifications (no ``id``).
        Never raises — all exceptions are encoded as JSON-RPC errors.
        """
        if request.get("jsonrpc") != "2.0":
            return _error_response(request.get("id"), -32600, "Invalid request")

        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}

        # Notifications have no id — don't reply.
        is_notification = "id" not in request

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "notifications/initialized":
                # Spec-mandated notification from the client after initialize.
                self._initialized = True
                return None
            elif method == "tools/list":
                result = self._handle_tools_list()
            elif method == "tools/call":
                result = await self._handle_tools_call(params)
            else:
                raise MCPError(-32601, f"Method not found: {method!r}")
        except MCPError as e:
            log.warning("mcp.error", method=method, code=e.code, err=str(e))
            if is_notification:
                return None
            return _error_response(req_id, e.code, str(e), e.data)
        except Exception as e:  # noqa: BLE001
            log.exception("mcp.unhandled", method=method)
            if is_notification:
                return None
            return _error_response(req_id, -32000, f"{type(e).__name__}: {e}")

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    # ---- method handlers ----

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        # We accept any client protocolVersion and always return ours.
        # MCP clients negotiate via the returned version field.
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},  # tools/list is supported; no subscriptions for now
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _handle_tools_list(self) -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                }
                for t in _TOOLS
            ]
        }

    async def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}

        if name != "hybrid.handle":
            raise MCPError(-32602, f"Unknown tool: {name!r}")

        user_message = arguments.get("user_message")
        user_id = arguments.get("user_id")
        if not isinstance(user_message, str) or not user_message.strip():
            raise MCPError(-32602, "user_message is required and must be a non-empty string")
        if not isinstance(user_id, str) or not user_id.strip():
            raise MCPError(-32602, "user_id is required and must be a non-empty string")

        result = await self.orchestrator.handle(
            user_message,
            user_id=user_id,
        )

        # MCP tool results use a `content` list with typed items. We return
        # the text response plus a structured metadata blob so MCP clients
        # can show either/both.
        return {
            "content": [
                {"type": "text", "text": result.response},
            ],
            "isError": result.task.degraded,
            "_meta": {
                "handled_by": result.handled_by,
                "tier": result.task.current_tier,
                "task_id": result.task.task_id,
                "degraded": result.task.degraded,
                "retry_count": result.task.retry_count,
            },
        }


def _error_response(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------


async def run_stdio(orchestrator: Orchestrator) -> None:  # pragma: no cover
    """Run the server over stdin/stdout using line-delimited JSON framing.

    This is the canonical MCP stdio transport: one JSON object per line.
    The official SDK uses Content-Length framing for long messages; we
    can add that once we hit a message > a few MB, which isn't a
    near-term concern for the Phase 3 surface.
    """
    server = HybridMCPServer(orchestrator)
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    log.info("mcp.stdio.start")
    while True:
        raw = await reader.readline()
        if not raw:
            break  # stdin closed → client disconnected
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            resp = _error_response(None, -32700, f"Parse error: {e}")
            _write_line(resp)
            continue

        response = await server.handle_request(request)
        if response is not None:
            _write_line(response)


def _write_line(obj: dict[str, Any]) -> None:  # pragma: no cover
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()
