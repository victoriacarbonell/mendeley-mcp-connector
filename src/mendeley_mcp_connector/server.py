"""MCP server definition, independent of the transport it runs on.

``create_server`` builds the :class:`MCPServer` and registers the tools. A
transport module (``transports/stdio.py`` today, an HTTP transport later) only
decides how the server is exposed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer

from mendeley_mcp_connector import __version__
from mendeley_mcp_connector.config import APP_NAME
from mendeley_mcp_connector.context import ServiceProvider
from mendeley_mcp_connector.tools import register_tools

INSTRUCTIONS = """\
Tools for the user's own Mendeley reference library (unofficial connector).
- The library belongs to the user: never invent document or folder IDs; get them from
  search_library, list_folders or list_folder_documents.
- Before adding a document, prefer add_document_by_doi when a DOI is known; both add tools
  refuse to create duplicates.
- Nothing can be deleted through this server.
- If a tool says the user is not connected, ask them to run
  `mendeley-mcp-connector auth login` in a terminal.
"""


def create_server(services: ServiceProvider | None = None) -> MCPServer[Any]:
    """Build the MCP server with all Mendeley tools registered."""
    provider = services or ServiceProvider()

    @asynccontextmanager
    async def lifespan(_server: MCPServer[Any]) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await provider.aclose()

    server: MCPServer[Any] = MCPServer(
        name=APP_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )
    register_tools(server, provider)
    return server
