"""Run the server over stdio, the transport used by Claude Desktop.

Over stdio, stdout carries the MCP protocol, so nothing else may be printed to it.
Logging goes to stderr.
"""

from __future__ import annotations

from mendeley_mcp_connector.server import create_server


def run_stdio() -> None:
    create_server().run("stdio")
