"""MCP server definition, independent of the transport it runs on.

``create_server`` builds the :class:`MCPServer` and registers the tools. A
transport module (``transports/stdio.py`` today, an HTTP transport later) only
decides how the server is exposed.

The Mendeley client is created lazily through :class:`ClientProvider`, so the
server starts even when the user has not logged in yet; tools then answer with
a clear "run auth login" message instead of crashing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer

from mendeley_mcp_connector import __version__
from mendeley_mcp_connector.api.client import MendeleyClient
from mendeley_mcp_connector.auth.token_store import TokenStore
from mendeley_mcp_connector.config import APP_NAME

INSTRUCTIONS = """\
Tools for the user's own Mendeley reference library (unofficial connector).
- The library belongs to the user: never invent document IDs; get them from search or list tools.
- Nothing can be deleted through this server.
- If a tool says the user is not connected, ask them to run
  `mendeley-mcp-connector auth login` in a terminal.
"""


class ClientProvider:
    """Creates the :class:`MendeleyClient` on first use and closes it on shutdown."""

    def __init__(self, factory: Callable[[], MendeleyClient] | None = None) -> None:
        self._factory = factory or (lambda: MendeleyClient(TokenStore()))
        self._client: MendeleyClient | None = None

    def get(self) -> MendeleyClient:
        if self._client is None:
            self._client = self._factory()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def create_server(provider: ClientProvider | None = None) -> MCPServer[Any]:
    """Build the MCP server with all Mendeley tools registered."""
    clients = provider or ClientProvider()

    @asynccontextmanager
    async def lifespan(_server: MCPServer[Any]) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await clients.aclose()

    server: MCPServer[Any] = MCPServer(
        name=APP_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )
    # Tools are registered here in the next development phase.
    return server
