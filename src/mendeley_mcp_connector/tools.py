"""MCP tool definitions.

Each tool is a thin wrapper around :class:`LibraryService`: it documents the
arguments for the model and turns expected failures into ``ToolError`` so the
model receives a readable message instead of a generic crash.

There are deliberately no tools that delete anything.
"""

# No `from __future__ import annotations` here: the MCP SDK inspects these
# signatures at runtime to build the JSON schemas.

from collections.abc import Awaitable
from typing import Annotated, Any, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from mendeley_mcp_connector.context import ServiceProvider
from mendeley_mcp_connector.errors import MendeleyError

T = TypeVar("T")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
ADDITIVE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
IDEMPOTENT_ADDITIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

DocumentId = Annotated[str, Field(description="Mendeley document ID (from search_library).")]
FolderId = Annotated[str, Field(description="Mendeley folder ID (from list_folders).")]


class Person(BaseModel):
    """An author or editor."""

    last_name: str = Field(
        description="Surname. For an organisation (e.g. a ministry), its full name."
    )
    first_name: str | None = Field(default=None, description="Given name(s) or initials.")


async def _guard(operation: Awaitable[T]) -> T:
    try:
        return await operation
    except (MendeleyError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    except OSError as exc:
        raise ToolError(f"Local file error: {exc}") from exc


def register_tools(server: MCPServer[Any], services: ServiceProvider) -> None:
    @server.tool(annotations=READ_ONLY)
    async def search_library(
        query: Annotated[
            str | None,
            Field(
                description="Free-text words matched against title, authors, source, abstract,"
                " keywords and identifiers. All words must appear."
            ),
        ] = None,
        title: Annotated[str | None, Field(description="Words that must appear in the title.")] = (
            None
        ),
        author: Annotated[
            str | None, Field(description="Author or editor name (surname is enough).")
        ] = None,
        year: Annotated[int | None, Field(description="Exact publication year.")] = None,
        year_from: Annotated[int | None, Field(description="Earliest year (inclusive).")] = None,
        year_to: Annotated[int | None, Field(description="Latest year (inclusive).")] = None,
        doi: Annotated[str | None, Field(description="DOI, with or without doi.org/.")] = None,
        limit: Annotated[int, Field(ge=1, le=200, description="Maximum results.")] = 20,
    ) -> dict[str, Any]:
        """Search the user's own Mendeley library (not the global catalog).

        Every given criterion must match; matching ignores case and accents. With no
        criteria, returns the most recently modified documents. Results are short
        summaries; use get_document for full details.
        """
        return await _guard(
            services.library().search(
                query=query,
                title=title,
                author=author,
                year=year,
                year_from=year_from,
                year_to=year_to,
                doi=doi,
                limit=limit,
            )
        )

    @server.tool(annotations=READ_ONLY)
    async def get_document(document_id: DocumentId) -> dict[str, Any]:
        """Get every stored detail of one library document, including attached files."""
        return await _guard(services.library().get_document(document_id))

    @server.tool(annotations=ADDITIVE)
    async def add_document_by_doi(
        doi: Annotated[str, Field(description="DOI, e.g. 10.1038/nature14539 or a doi.org URL.")],
        folder_id: Annotated[
            str | None, Field(description="Optional folder to put the document in.")
        ] = None,
        document_type: Annotated[
            str | None,
            Field(
                description=(
                    "Optional Mendeley document type (e.g. journal, book_section, "
                    "conference_proceedings) when you know it. If omitted, the catalog's "
                    "type is used, and catalog records marked 'generic' that name a "
                    "publication are saved as 'journal'."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Add a publication to the library by DOI, using metadata from the Mendeley catalog.

        First checks whether a document with the same DOI is already in the library; if
        so, nothing is added (status "already_in_library") and the existing document is
        returned. If the DOI is not in the catalog, use add_document_manual instead.
        """
        return await _guard(services.library().add_by_doi(doi, folder_id, document_type))

    @server.tool(annotations=ADDITIVE)
    async def add_document_manual(
        title: Annotated[str, Field(description="Title of the work.")],
        document_type: Annotated[
            str,
            Field(
                description="Mendeley type. Common: journal, book, book_section, report, "
                "web_page, thesis, statute (laws, regulations), bill, case (court rulings), "
                "conference_proceedings, working_paper, newspaper_article, generic."
            ),
        ] = "generic",
        authors: Annotated[
            list[Person] | None, Field(description="Authors in order. Organisations allowed.")
        ] = None,
        editors: Annotated[list[Person] | None, Field(description="Editors, if any.")] = None,
        year: Annotated[int | None, Field(description="Publication or enactment year.")] = None,
        source: Annotated[
            str | None,
            Field(description="Where it appeared: journal, book, website or official bulletin."),
        ] = None,
        publisher: Annotated[str | None, Field(description="Publisher.")] = None,
        city: Annotated[str | None, Field(description="Place of publication.")] = None,
        institution: Annotated[
            str | None, Field(description="Issuing institution, university or agency.")
        ] = None,
        volume: Annotated[str | None, Field(description="Volume.")] = None,
        issue: Annotated[str | None, Field(description="Issue or number.")] = None,
        pages: Annotated[str | None, Field(description="Pages, e.g. 12-34.")] = None,
        url: Annotated[str | None, Field(description="Web address of the work.")] = None,
        accessed: Annotated[
            str | None, Field(description="Date the URL was accessed, YYYY-MM-DD.")
        ] = None,
        doi: Annotated[str | None, Field(description="DOI, if it has one.")] = None,
        isbn: Annotated[str | None, Field(description="ISBN.")] = None,
        issn: Annotated[str | None, Field(description="ISSN.")] = None,
        abstract: Annotated[str | None, Field(description="Abstract or summary.")] = None,
        keywords: Annotated[list[str] | None, Field(description="Keywords.")] = None,
        tags: Annotated[list[str] | None, Field(description="Personal tags.")] = None,
        extra_fields: Annotated[
            dict[str, Any] | None,
            Field(
                description='Other Mendeley attributes, e.g. {"month": 3, "day": 14, '
                '"code": "...", "country": "...", "language": "...", '
                '"edition": "...", "series": "..."}.'
            ),
        ] = None,
        folder_id: Annotated[
            str | None, Field(description="Optional folder to put the document in.")
        ] = None,
        allow_duplicate: Annotated[
            bool,
            Field(
                description="Add even if a document with the same title (and year) or DOI "
                "already exists. Only set after confirming with the user."
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Add a document from metadata typed by hand: laws, reports, web pages, items
        without a DOI, or when the DOI is not in the catalog.

        By default nothing is added if the library already has a document with the same
        DOI, or the same title and year (status "already_in_library").
        """
        return await _guard(
            services.library().add_manual(
                title=title,
                document_type=document_type,
                authors=[p.model_dump() for p in authors or []],
                editors=[p.model_dump() for p in editors or []],
                year=year,
                source=source,
                publisher=publisher,
                city=city,
                institution=institution,
                volume=volume,
                issue=issue,
                pages=pages,
                url=url,
                accessed=accessed,
                doi=doi,
                isbn=isbn,
                issn=issn,
                abstract=abstract,
                keywords=keywords,
                tags=tags,
                extra_fields=extra_fields,
                folder_id=folder_id,
                allow_duplicate=allow_duplicate,
            )
        )

    @server.tool(annotations=READ_ONLY)
    async def list_folders() -> dict[str, Any]:
        """List all folders in the library with their IDs and parent folder IDs."""
        folders = await _guard(services.library().list_folders())
        return {"count": len(folders), "folders": folders}

    @server.tool(annotations=IDEMPOTENT_ADDITIVE)
    async def create_folder(
        name: Annotated[str, Field(description="Folder name.")],
        parent_id: Annotated[
            str | None, Field(description="Optional parent folder ID, to create a subfolder.")
        ] = None,
    ) -> dict[str, Any]:
        """Create a folder. If a folder with the same name already exists in the same
        place, it is returned instead (status "already_exists")."""
        return await _guard(services.library().create_folder(name, parent_id))

    @server.tool(annotations=IDEMPOTENT_ADDITIVE)
    async def add_to_folder(
        folder_id: FolderId,
        document_ids: Annotated[
            list[str], Field(min_length=1, description="IDs of the documents to file.")
        ],
    ) -> dict[str, Any]:
        """Put existing library documents into a folder. Documents stay in the library and
        in any other folders; documents already in the folder are skipped."""
        return await _guard(services.library().add_to_folder(folder_id, document_ids))

    @server.tool(annotations=READ_ONLY)
    async def list_folder_documents(
        folder_id: FolderId,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum results.")] = 100,
    ) -> dict[str, Any]:
        """List the documents in a folder, as short summaries."""
        return await _guard(services.library().list_folder_documents(folder_id, limit))

    @server.tool(annotations=IDEMPOTENT_ADDITIVE)
    async def attach_pdf(
        document_id: DocumentId,
        file_path: Annotated[
            str,
            Field(
                description="Full path of a PDF on the computer running this server, "
                "e.g. C:\\Users\\name\\Downloads\\paper.pdf."
            ),
        ],
    ) -> dict[str, Any]:
        """Upload a local PDF and attach it to a library document. Skips the upload if
        the same PDF is already attached. Only attach files the user has the right to
        store."""
        return await _guard(services.library().attach_pdf(document_id, file_path))

    @server.tool(annotations=ADDITIVE)  # may write a new local .bib file
    async def export_bibtex(
        folder_id: Annotated[str | None, Field(description="Export every document in it.")] = (
            None
        ),
        document_ids: Annotated[
            list[str] | None, Field(description="Or export these documents.")
        ] = None,
        output_path: Annotated[
            str | None,
            Field(
                description="Optional path of a NEW .bib file to write on this computer. "
                "Existing files are never overwritten. If omitted, the BibTeX is returned."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Export BibTeX for a folder or a list of documents (give exactly one of them)."""
        return await _guard(
            services.library().export_bibtex(
                folder_id=folder_id, document_ids=document_ids, output_path=output_path
            )
        )
