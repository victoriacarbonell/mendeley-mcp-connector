from __future__ import annotations

from mendeley_mcp_connector import __version__
from mendeley_mcp_connector.server import ClientProvider, create_server


async def test_server_builds_without_credentials() -> None:
    server = create_server()

    assert server.name == "mendeley-mcp-connector"
    assert server.version == __version__
    assert server.instructions is not None
    assert "deleted" in server.instructions
    assert await server.list_tools() == []


async def test_client_provider_is_lazy_and_closable() -> None:
    created: list[object] = []

    class FakeClient:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    def factory() -> FakeClient:
        client = FakeClient()
        created.append(client)
        return client

    provider = ClientProvider(factory)  # type: ignore[arg-type]
    assert created == []
    first = provider.get()
    assert provider.get() is first
    await provider.aclose()
    assert first.closed  # type: ignore[attr-defined]
