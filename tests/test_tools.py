"""End-to-end tests: MCP tool call -> service -> HTTP (mocked with respx).

All data is invented. No real Mendeley account or network is used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mendeley_mcp_connector.api import media_types
from mendeley_mcp_connector.api.client import MendeleyClient
from mendeley_mcp_connector.auth.token_store import TokenStore
from mendeley_mcp_connector.config import API_BASE_URL
from mendeley_mcp_connector.context import ServiceProvider
from mendeley_mcp_connector.server import create_server
from tests.conftest import make_credentials

API = API_BASE_URL
LIBRARY = [
    {
        "id": "doc-1",
        "title": "Políticas públicas de vivienda",
        "type": "journal",
        "authors": [{"first_name": "Ana", "last_name": "Pérez"}],
        "year": 2021,
        "source": "Revista Ficticia",
        "identifiers": {"doi": "10.1234/fake.001"},
    },
    {
        "id": "doc-2",
        "title": "Fake Act on Example Matters",
        "type": "statute",
        "authors": [{"last_name": "Fictional Parliament"}],
        "year": 2023,
    },
    {
        "id": "doc-3",
        "title": "Urban mobility report",
        "type": "report",
        "authors": [{"first_name": "Test", "last_name": "Author"}],
        "year": 2019,
        "identifiers": {"isbn": "000-0-00-000000-0"},
    },
]


async def _no_sleep(_delay: float) -> None:
    return None


class Harness:
    def __init__(self, store: TokenStore) -> None:
        store.save(make_credentials(expires_at=9_999_999_999.0))
        provider = ServiceProvider(lambda: MendeleyClient(store, sleep=_no_sleep))
        self.server = create_server(provider)

    async def call(self, tool: str, /, **arguments: Any) -> dict[str, Any]:
        result = await self.server.call_tool(tool, arguments)
        assert not getattr(result, "is_error", False), result
        structured = getattr(result, "structured_content", None)
        assert isinstance(structured, dict)
        return structured


@pytest.fixture
def harness(store: TokenStore) -> Harness:
    return Harness(store)


def mock_library(documents: list[dict[str, Any]] = LIBRARY) -> respx.Route:
    return respx.get(f"{API}/documents", params={"sort": "last_modified"}).mock(
        return_value=httpx.Response(200, json=documents)
    )


def body_of(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


# ---------------------------------------------------------------- search & get


@respx.mock
async def test_search_library_filters_locally(harness: Harness) -> None:
    route = mock_library()

    by_author = await harness.call("search_library", author="perez")
    by_query = await harness.call("search_library", query="fictional act")
    by_doi = await harness.call("search_library", doi="https://doi.org/10.1234/FAKE.001")
    by_years = await harness.call("search_library", year_from=2020)
    everything = await harness.call("search_library", limit=2)

    assert [d["id"] for d in by_author["documents"]] == ["doc-1"]
    assert [d["id"] for d in by_query["documents"]] == ["doc-2"]
    assert [d["id"] for d in by_doi["documents"]] == ["doc-1"]
    assert [d["id"] for d in by_years["documents"]] == ["doc-1", "doc-2"]
    assert everything["total_matches"] == 3
    assert everything["returned"] == 2
    request = route.calls.last.request
    assert request.url.params["limit"] == "500"
    assert request.headers["Accept"] == media_types.DOCUMENT


@respx.mock
async def test_get_document_includes_files(harness: Harness) -> None:
    respx.get(f"{API}/documents/doc-1", params={"view": "all"}).mock(
        return_value=httpx.Response(200, json=LIBRARY[0])
    )
    respx.get(f"{API}/files", params={"document_id": "doc-1"}).mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "file-1", "file_name": "paper.pdf", "size": 10, "filehash": "x"}],
        )
    )

    document = await harness.call("get_document", document_id="doc-1")

    assert document["title"] == LIBRARY[0]["title"]
    assert document["files"] == [{"id": "file-1", "file_name": "paper.pdf", "size": 10}]


@respx.mock
async def test_document_ids_cannot_escape_the_path(harness: Harness) -> None:
    route = respx.get(f"{API}/documents/..%2Fprofiles%2Fme").mock(
        return_value=httpx.Response(404, json={"message": "Not found"})
    )

    with pytest.raises(ToolError, match="could not find"):
        await harness.call("get_document", document_id="../profiles/me")
    assert route.called


async def test_tools_explain_how_to_log_in(store: TokenStore) -> None:
    server = create_server(ServiceProvider(lambda: MendeleyClient(store)))

    with pytest.raises(ToolError, match="auth login"):
        await server.call_tool("list_folders", {})


# ------------------------------------------------------------------ add by DOI


@respx.mock
async def test_add_by_doi_skips_existing_document(harness: Harness) -> None:
    mock_library()
    create = respx.post(f"{API}/documents").mock(return_value=httpx.Response(201, json={}))

    result = await harness.call("add_document_by_doi", doi="doi:10.1234/FAKE.001")

    assert result["status"] == "already_in_library"
    assert result["document"]["id"] == "doc-1"
    assert not create.called


@respx.mock
async def test_add_by_doi_creates_from_catalog_and_files_it(harness: Harness) -> None:
    mock_library()
    catalog = respx.get(f"{API}/catalog", params={"doi": "10.5555/new.paper"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "catalog-1",
                    "link": "https://www.mendeley.com/catalogue/fake",
                    "title": "A brand new fake paper",
                    "type": "journal",
                    "authors": [{"first_name": "Test", "last_name": "Author"}],
                    "year": 2025,
                    "identifiers": {"doi": "10.5555/NEW.PAPER"},
                }
            ],
        )
    )
    create = respx.post(f"{API}/documents").mock(
        return_value=httpx.Response(
            201, json={"id": "doc-new", "title": "A brand new fake paper", "year": 2025}
        )
    )
    respx.get(f"{API}/folders/folder-1/documents").mock(return_value=httpx.Response(200, json=[]))
    file_into = respx.post(f"{API}/folders/folder-1/documents").mock(
        return_value=httpx.Response(201)
    )

    result = await harness.call(
        "add_document_by_doi", doi="https://doi.org/10.5555/new.paper", folder_id="folder-1"
    )

    assert result["status"] == "added"
    assert result["document"]["id"] == "doc-new"
    assert result["folder"]["added"] == ["doc-new"]
    assert catalog.calls.last.request.url.params["view"] == "bib"
    body = body_of(create)
    assert body["identifiers"]["doi"] == "10.5555/new.paper"
    assert "id" not in body and "link" not in body
    assert create.calls.last.request.headers["Content-Type"] == media_types.DOCUMENT
    assert body_of(file_into) == {"id": "doc-new"}


def mock_generic_catalog_record() -> None:
    mock_library()
    respx.get(f"{API}/catalog", params={"doi": "10.5555/generic.paper"}).mock(
        return_value=httpx.Response(
            200,
            json=[{"title": "A fake generic paper", "type": "generic", "source": "Fake Journal"}],
        )
    )


@respx.mock
async def test_add_by_doi_saves_generic_catalog_article_as_journal(harness: Harness) -> None:
    mock_generic_catalog_record()
    create = respx.post(f"{API}/documents").mock(
        return_value=httpx.Response(201, json={"id": "doc-new", "type": "journal"})
    )

    result = await harness.call("add_document_by_doi", doi="10.5555/generic.paper")

    assert body_of(create)["type"] == "journal"
    assert "'generic'" in result["type_note"]


@respx.mock
async def test_add_by_doi_document_type_override(harness: Harness) -> None:
    mock_generic_catalog_record()
    create = respx.post(f"{API}/documents").mock(
        return_value=httpx.Response(201, json={"id": "doc-new"})
    )

    result = await harness.call(
        "add_document_by_doi", doi="10.5555/generic.paper", document_type="conference_proceedings"
    )

    assert body_of(create)["type"] == "conference_proceedings"
    assert "type_note" not in result


@respx.mock
async def test_add_by_doi_rejects_unknown_type_before_any_request(harness: Harness) -> None:
    library = mock_library()
    with pytest.raises(ToolError, match="Unknown document type"):
        await harness.call("add_document_by_doi", doi="10.5555/x", document_type="podcast")
    assert not library.called


@respx.mock
async def test_add_by_doi_not_in_catalog_suggests_manual(harness: Harness) -> None:
    mock_library()
    respx.get(f"{API}/catalog").mock(return_value=httpx.Response(404, json={"message": "nope"}))

    with pytest.raises(ToolError, match="add_document_manual"):
        await harness.call("add_document_by_doi", doi="10.5555/unknown")


async def test_add_by_doi_rejects_non_doi(harness: Harness) -> None:
    with pytest.raises(ToolError, match="does not look like a DOI"):
        await harness.call("add_document_by_doi", doi="not-a-doi")


# ------------------------------------------------------------------ add manual


@respx.mock
async def test_add_manual_statute(harness: Harness) -> None:
    mock_library()
    create = respx.post(f"{API}/documents").mock(
        return_value=httpx.Response(201, json={"id": "doc-law", "title": "Fake Regulation 1/2026"})
    )

    result = await harness.call(
        "add_document_manual",
        title="Fake Regulation 1/2026",
        document_type="statute",
        authors=[{"last_name": "Fictional Ministry of Examples"}],
        year=2026,
        source="Official Fake Gazette",
        url="https://example.org/reg-1-2026",
        accessed="2026-11-01",
        extra_fields={"month": 2, "day": 3},
    )

    assert result["status"] == "added"
    assert body_of(create) == {
        "title": "Fake Regulation 1/2026",
        "type": "statute",
        "authors": [{"last_name": "Fictional Ministry of Examples"}],
        "year": 2026,
        "source": "Official Fake Gazette",
        "websites": ["https://example.org/reg-1-2026"],
        "accessed": "2026-11-01",
        "month": 2,
        "day": 3,
    }


@respx.mock
async def test_add_manual_refuses_duplicate_title(harness: Harness) -> None:
    mock_library()
    create = respx.post(f"{API}/documents").mock(return_value=httpx.Response(201, json={}))

    result = await harness.call(
        "add_document_manual", title="urban MOBILITY report", document_type="report", year=2019
    )

    assert result["status"] == "already_in_library"
    assert result["document"]["id"] == "doc-3"
    assert not create.called


@respx.mock
async def test_add_manual_allow_duplicate_skips_check(harness: Harness) -> None:
    library = mock_library()
    respx.post(f"{API}/documents").mock(
        return_value=httpx.Response(201, json={"id": "doc-4", "title": "Urban mobility report"})
    )

    result = await harness.call(
        "add_document_manual",
        title="Urban mobility report",
        document_type="report",
        allow_duplicate=True,
    )

    assert result["status"] == "added"
    assert not library.called


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"title": "X", "document_type": "podcast"}, "Unknown document type"),
        ({"title": "X", "accessed": "yesterday"}, "YYYY|2026-01-31"),
        ({"title": "X", "extra_fields": {"profile_id": "x"}}, "Unsupported extra field"),
        ({"title": "   "}, "title is required"),
    ],
)
async def test_add_manual_validation(
    harness: Harness, arguments: dict[str, Any], message: str
) -> None:
    with pytest.raises(ToolError, match=message):
        await harness.call("add_document_manual", **arguments)


@respx.mock
async def test_add_manual_uses_location_when_body_is_empty(harness: Harness) -> None:
    mock_library([])
    respx.post(f"{API}/documents").mock(
        return_value=httpx.Response(201, headers={"Location": f"{API}/documents/doc-9"})
    )
    respx.get(f"{API}/documents/doc-9").mock(
        return_value=httpx.Response(200, json={"id": "doc-9", "title": "New"})
    )

    result = await harness.call("add_document_manual", title="New")

    assert result["document"]["id"] == "doc-9"


# --------------------------------------------------------------------- folders

FOLDERS = [
    {"id": "folder-1", "name": "Thesis", "created": "2026-01-01T00:00:00Z"},
    {"id": "folder-2", "name": "Chapter 1", "parent_id": "folder-1"},
]


@respx.mock
async def test_list_folders(harness: Harness) -> None:
    respx.get(f"{API}/folders").mock(return_value=httpx.Response(200, json=FOLDERS))

    result = await harness.call("list_folders")

    assert result["count"] == 2
    assert result["folders"][1] == {"id": "folder-2", "name": "Chapter 1", "parent_id": "folder-1"}


@respx.mock
async def test_create_folder_returns_existing(harness: Harness) -> None:
    respx.get(f"{API}/folders").mock(return_value=httpx.Response(200, json=FOLDERS))
    create = respx.post(f"{API}/folders").mock(return_value=httpx.Response(201, json={}))

    result = await harness.call("create_folder", name="thesis")

    assert result["status"] == "already_exists"
    assert not create.called


@respx.mock
async def test_create_subfolder(harness: Harness) -> None:
    respx.get(f"{API}/folders").mock(return_value=httpx.Response(200, json=FOLDERS))
    create = respx.post(f"{API}/folders").mock(
        return_value=httpx.Response(
            201, json={"id": "folder-3", "name": "Chapter 2", "parent_id": "folder-1"}
        )
    )

    result = await harness.call("create_folder", name="Chapter 2", parent_id="folder-1")

    assert result["status"] == "created"
    assert result["folder"]["id"] == "folder-3"
    assert body_of(create) == {"name": "Chapter 2", "parent_id": "folder-1"}
    assert create.calls.last.request.headers["Content-Type"] == media_types.FOLDER


@respx.mock
async def test_create_folder_with_unknown_parent(harness: Harness) -> None:
    respx.get(f"{API}/folders").mock(return_value=httpx.Response(200, json=FOLDERS))

    with pytest.raises(ToolError, match="list_folders"):
        await harness.call("create_folder", name="X", parent_id="nope")


@respx.mock
async def test_add_to_folder_skips_present_and_reports_missing(harness: Harness) -> None:
    respx.get(f"{API}/folders/folder-1/documents").mock(
        return_value=httpx.Response(200, json=[{"id": "doc-1"}])
    )
    post = respx.post(f"{API}/folders/folder-1/documents").mock(
        side_effect=[httpx.Response(201), httpx.Response(404, json={"message": "no doc"})]
    )

    result = await harness.call(
        "add_to_folder", folder_id="folder-1", document_ids=["doc-1", "doc-2", "doc-2", "ghost"]
    )

    assert result == {
        "folder_id": "folder-1",
        "added": ["doc-2"],
        "already_in_folder": ["doc-1"],
        "not_found": ["ghost"],
    }
    assert post.call_count == 2


@respx.mock
async def test_list_folder_documents(harness: Harness) -> None:
    respx.get(f"{API}/folders/folder-1/documents").mock(
        return_value=httpx.Response(200, json=[{"id": "doc-3"}, {"id": "doc-1"}])
    )
    mock_library()

    result = await harness.call("list_folder_documents", folder_id="folder-1")

    assert result["total"] == 2
    assert [d["id"] for d in result["documents"]] == ["doc-3", "doc-1"]


# ----------------------------------------------------------------------- files


@pytest.fixture
def pdf_file(tmp_path: Path) -> Path:
    path = tmp_path / "Pérez 2021.pdf"
    path.write_bytes(b"%PDF-1.7\n% fake pdf content\n%%EOF\n")
    return path


@respx.mock
async def test_attach_pdf_uploads_with_link_header(harness: Harness, pdf_file: Path) -> None:
    respx.get(f"{API}/documents/doc-1").mock(return_value=httpx.Response(200, json=LIBRARY[0]))
    respx.get(f"{API}/files").mock(return_value=httpx.Response(200, json=[]))
    upload = respx.post(f"{API}/files").mock(
        return_value=httpx.Response(201, json={"id": "file-9", "file_name": pdf_file.name})
    )

    result = await harness.call("attach_pdf", document_id="doc-1", file_path=str(pdf_file))

    assert result["status"] == "attached"
    assert result["file"]["id"] == "file-9"
    request = upload.calls.last.request
    assert request.headers["Content-Type"] == "application/pdf"
    assert request.headers["Link"] == f'<{API}/documents/doc-1>; rel="document"'
    disposition = request.headers["Content-Disposition"]
    assert disposition.startswith('attachment; filename="P_rez 2021.pdf"')
    assert unquote(disposition.split("UTF-8''")[1]) == "Pérez 2021.pdf"
    assert request.content == pdf_file.read_bytes()


@respx.mock
async def test_attach_pdf_skips_already_attached(harness: Harness, pdf_file: Path) -> None:
    respx.get(f"{API}/documents/doc-1").mock(return_value=httpx.Response(200, json=LIBRARY[0]))
    respx.get(f"{API}/files").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "f1", "file_name": pdf_file.name, "size": pdf_file.stat().st_size}],
        )
    )
    upload = respx.post(f"{API}/files").mock(return_value=httpx.Response(201, json={}))

    result = await harness.call("attach_pdf", document_id="doc-1", file_path=str(pdf_file))

    assert result["status"] == "already_attached"
    assert not upload.called


async def test_attach_pdf_validates_file(harness: Harness, tmp_path: Path) -> None:
    text_file = tmp_path / "notes.txt"
    text_file.write_text("hello", encoding="utf-8")
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_text("not really a pdf", encoding="utf-8")

    with pytest.raises(ToolError, match="No file"):
        await harness.call("attach_pdf", document_id="d", file_path=str(tmp_path / "x.pdf"))
    with pytest.raises(ToolError, match="Only PDF"):
        await harness.call("attach_pdf", document_id="d", file_path=str(text_file))
    with pytest.raises(ToolError, match="does not look like a PDF"):
        await harness.call("attach_pdf", document_id="d", file_path=str(fake_pdf))


# ---------------------------------------------------------------------- BibTeX

API_BIBTEX = "@article{perez2021,\n  title = {Políticas públicas de vivienda}\n}"


@respx.mock
async def test_export_bibtex_from_api(harness: Harness) -> None:
    route = respx.get(f"{API}/documents/doc-1").mock(
        return_value=httpx.Response(200, text=API_BIBTEX)
    )

    result = await harness.call("export_bibtex", document_ids=["doc-1"])

    assert result["source"] == "mendeley"
    assert result["bibtex"] == API_BIBTEX + "\n"
    assert route.calls.last.request.headers["Accept"] == media_types.BIBTEX


@respx.mock
async def test_export_bibtex_falls_back_to_local_generation(harness: Harness) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.headers["Accept"] == media_types.BIBTEX:
            return httpx.Response(406, json={"message": "Not acceptable"})
        document_id = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, json=next(d for d in LIBRARY if d["id"] == document_id))

    respx.get(url__regex=rf"{API}/documents/doc-\d").mock(side_effect=respond)
    respx.get(f"{API}/folders/folder-1/documents").mock(
        return_value=httpx.Response(200, json=[{"id": "doc-1"}, {"id": "doc-2"}])
    )

    result = await harness.call("export_bibtex", folder_id="folder-1")

    assert result["source"] == "generated"
    assert result["count"] == 2
    assert "@article{perez2021politicas," in result["bibtex"]
    assert "@misc{fictionalparliament2023fake," in result["bibtex"]


@respx.mock
async def test_export_bibtex_reports_missing_and_writes_new_file(
    harness: Harness, tmp_path: Path
) -> None:
    respx.get(f"{API}/documents/doc-1").mock(return_value=httpx.Response(200, text=API_BIBTEX))
    respx.get(f"{API}/documents/ghost").mock(return_value=httpx.Response(404))
    target = tmp_path / "refs.bib"

    result = await harness.call(
        "export_bibtex", document_ids=["doc-1", "ghost"], output_path=str(target)
    )

    assert result["missing_ids"] == ["ghost"]
    assert result["written_to"] == str(target)
    assert "bibtex" not in result
    assert target.read_text(encoding="utf-8") == API_BIBTEX + "\n"

    with pytest.raises(ToolError, match="never overwritten"):
        await harness.call("export_bibtex", document_ids=["doc-1"], output_path=str(target))


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"folder_id": "f", "document_ids": ["d"]},
        {"document_ids": ["d"], "output_path": "x.txt"},
    ],
)
async def test_export_bibtex_argument_validation(
    harness: Harness, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ToolError):
        await harness.call("export_bibtex", **arguments)
