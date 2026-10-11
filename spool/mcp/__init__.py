"""Model Context Protocol: a small client and server over stdio, written against the spec.

Both speak the two protocol eras in use in 2026:
  handshake  2024-11-05 .. 2025-11-25: `initialize`, then `notifications/initialized`
  modern     2026-07-28: no handshake; `server/discover` advertises versions, and every
             request carries its protocol version, client info and capabilities in `_meta`
The client negotiates the way the official SDKs do: probe `server/discover`, and fall back
to `initialize` on anything that is not positive evidence of a modern server.
"""

from .client import MCPClient, MCPError
from .protocol import HANDSHAKE_VERSIONS, MODERN_VERSIONS, result_text

__all__ = ["MCPClient", "MCPError", "HANDSHAKE_VERSIONS", "MODERN_VERSIONS", "result_text"]
