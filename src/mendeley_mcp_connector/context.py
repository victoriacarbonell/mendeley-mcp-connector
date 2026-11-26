"""Lazily created, shared objects used by the tools.

The Mendeley client is created on first use, so the server can start before the
user has logged in; tools then report a clear "run auth login" message. A future
HTTP transport can provide one :class:`ServiceProvider` per authenticated user.
"""

from __future__ import annotations

from collections.abc import Callable

from mendeley_mcp_connector.api.client import MendeleyClient
from mendeley_mcp_connector.auth.token_store import TokenStore
from mendeley_mcp_connector.services.library import LibraryService


class ServiceProvider:
    """Creates the client and library service once and closes them on shutdown."""

    def __init__(self, client_factory: Callable[[], MendeleyClient] | None = None) -> None:
        self._client_factory = client_factory or (lambda: MendeleyClient(TokenStore()))
        self._client: MendeleyClient | None = None
        self._library: LibraryService | None = None

    def client(self) -> MendeleyClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def library(self) -> LibraryService:
        if self._library is None:
            self._library = LibraryService(self.client())
        return self._library

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None
        self._library = None
