from __future__ import annotations

from mendeley_mcp_connector import __version__
from mendeley_mcp_connector.context import ServiceProvider
from mendeley_mcp_connector.server import create_server

EXPECTED_TOOLS = {
    "search_library",
    "get_document",
    "add_document_by_doi",
    "add_document_manual",
    "list_folders",
    "create_folder",
    "add_to_folder",
    "list_folder_documents",
    "attach_pdf",
    "export_bibtex",
}


async def test_server_exposes_expected_tools_and_nothing_destructive() -> None:
    server = create_server()

    assert server.name == "mendeley-mcp-connector"
    assert server.version == __version__
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.annotations is not None
        assert tool.annotations.destructive_hint is False
        assert not any(word in tool.name for word in ("delete", "remove", "trash"))


async def test_read_only_tools_are_marked() -> None:
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    for name in ("search_library", "get_document", "list_folders", "list_folder_documents"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True


async def test_service_provider_is_lazy_and_closable() -> None:
    created: list[object] = []

    class FakeClient:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    def factory() -> FakeClient:
        client = FakeClient()
        created.append(client)
        return client

    provider = ServiceProvider(factory)  # type: ignore[arg-type]
    assert created == []
    first = provider.client()
    assert provider.client() is first
    await provider.aclose()
    assert first.closed  # type: ignore[attr-defined]
