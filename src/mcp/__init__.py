"""Phase 3: expose the hybrid orchestrator as an MCP server.

Entry point for external MCP clients (IDEs, other agents) to reach into
this project's orchestrator without going through Discord. Kept
SDK-free: we speak the MCP JSON-RPC 2.0 protocol directly over stdio
so the stub has zero new dependencies.

Scope is deliberately minimal for Phase 3 — enough to verify the wiring
and shape the surface that Phase 4+ will flesh out:

  * ``initialize`` — handshake
  * ``tools/list`` — advertise the one tool
  * ``tools/call`` — dispatch ``hybrid.handle`` to the Orchestrator

The body of ``hybrid.handle`` is just a thin wrapper around
``Orchestrator.handle``; all the real logic (routing, skills, Hermes
lanes, budget, memo) lives in the existing code. The server is
transport-agnostic: ``HybridMCPServer.handle_request`` takes and returns
dict-shaped JSON-RPC, so unit tests don't need a subprocess.
"""
from .server import HybridMCPServer, MCPError, run_stdio

__all__ = ["HybridMCPServer", "MCPError", "run_stdio"]
