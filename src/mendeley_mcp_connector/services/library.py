"""Operations on the user's Mendeley library.

Design notes:

* Nothing here deletes or overwrites data in Mendeley.
* Library search and duplicate detection filter the user's documents locally.
  The API has no documented library search or DOI filter, so local filtering
  gives predictable results; a personal library is fetched in pages of 500.
* BibTeX is requested from the API first; if the API does not return BibTeX,
  it is generated locally from the document metadata.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mendeley_mcp_connector.api import media_types
from mendeley_mcp_connector.api.client import MendeleyClient
from mendeley_mcp_connector.config import API_BASE_URL
from mendeley_mcp_connector.errors import ApiError, NotFoundError
from mendeley_mcp_connector.services import bibtex
from mendeley_mcp_connector.services.documents import (
    DOCUMENT_TYPES,
    EXTRA_DOCUMENT_FIELDS,
    clean_people,
    document_from_catalog,
    doi_key,
    fold,
    matches,
    normalize_doi,
    summarize,
)

PAGE_SIZE = 500  # documented maximum for list endpoints
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_EXPORT_DOCUMENTS = 500
EXPORT_CONCURRENCY = 4
API_BIBTEX_FAILURE_STATUSES = frozenset({400, 406, 415, 501})


class LibraryService:
    def __init__(self, client: MendeleyClient) -> None:
        self._client = client
        self._api_bibtex_works: bool | None = None  # learned on first export

    # ----------------------------------------------------------------- documents

    async def all_documents(self) -> list[dict[str, Any]]:
        """Every document in the user's library, most recently modified first."""
        items = await self._client.paginate(
            "/documents",
            accept=media_types.DOCUMENT,
            params={"limit": PAGE_SIZE, "sort": "last_modified", "order": "desc"},
        )
        return [item for item in items if isinstance(item, dict)]

    async def search(
        self,
        *,
        query: str | None = None,
        title: str | None = None,
        author: str | None = None,
        year: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        doi: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if doi is not None:
            doi = normalize_doi(doi)
        found = [
            doc
            for doc in await self.all_documents()
            if matches(
                doc,
                query=query,
                title=title,
                author=author,
                year=year,
                year_from=year_from,
                year_to=year_to,
                doi=doi,
            )
        ]
        limit = max(1, min(limit, 200))
        return {
            "total_matches": len(found),
            "returned": min(len(found), limit),
            "documents": [summarize(doc) for doc in found[:limit]],
        }

    async def get_document(self, document_id: str) -> dict[str, Any]:
        document = await self._client.get_json(
            f"/documents/{_path_id(document_id)}",
            accept=media_types.DOCUMENT,
            params={"view": "all"},
        )
        files = await self._files_for(document_id)
        document["files"] = [
            {k: f.get(k) for k in ("id", "file_name", "mime_type", "size") if k in f} for f in files
        ]
        return dict(document)

    async def find_by_doi(self, doi: str) -> list[dict[str, Any]]:
        key = doi_key(doi)
        return [
            doc
            for doc in await self.all_documents()
            if doi_key((doc.get("identifiers") or {}).get("doi")) == key
        ]

    async def add_by_doi(self, doi: str, folder_id: str | None = None) -> dict[str, Any]:
        doi = normalize_doi(doi)
        existing = await self.find_by_doi(doi)
        if existing:
            result: dict[str, Any] = {
                "status": "already_in_library",
                "message": "A document with this DOI is already in the library; nothing was added.",
                "document": summarize(existing[0]),
            }
            if folder_id:
                result["folder"] = await self.add_to_folder(folder_id, [str(existing[0]["id"])])
            return result

        record = await self._catalog_record(doi)
        created = await self._create_document(document_from_catalog(record, doi))
        result = {"status": "added", "document": summarize(created)}
        if folder_id:
            result["folder"] = await self.add_to_folder(folder_id, [str(created["id"])])
        return result

    async def add_manual(
        self,
        *,
        title: str,
        document_type: str,
        authors: Sequence[Mapping[str, Any]] | None = None,
        editors: Sequence[Mapping[str, Any]] | None = None,
        year: int | None = None,
        source: str | None = None,
        publisher: str | None = None,
        city: str | None = None,
        institution: str | None = None,
        volume: str | None = None,
        issue: str | None = None,
        pages: str | None = None,
        url: str | None = None,
        accessed: str | None = None,
        doi: str | None = None,
        isbn: str | None = None,
        issn: str | None = None,
        abstract: str | None = None,
        keywords: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        extra_fields: Mapping[str, Any] | None = None,
        folder_id: str | None = None,
        allow_duplicate: bool = False,
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("A title is required.")
        if document_type not in DOCUMENT_TYPES:
            raise ValueError(
                f"Unknown document type {document_type!r}. Use one of: "
                + ", ".join(sorted(DOCUMENT_TYPES))
            )
        if accessed is not None:
            _validate_date(accessed)
        unknown = set(extra_fields or {}) - EXTRA_DOCUMENT_FIELDS
        if unknown:
            raise ValueError(
                f"Unsupported extra field(s): {', '.join(sorted(unknown))}. Allowed: "
                + ", ".join(sorted(EXTRA_DOCUMENT_FIELDS))
            )

        identifiers: dict[str, str] = {}
        if doi:
            identifiers["doi"] = normalize_doi(doi)
        if isbn:
            identifiers["isbn"] = isbn.strip()
        if issn:
            identifiers["issn"] = issn.strip()

        if not allow_duplicate:
            duplicate = await self._find_duplicate(title, year, identifiers.get("doi"))
            if duplicate is not None:
                return {
                    "status": "already_in_library",
                    "message": "A matching document already exists; nothing was added. "
                    "Call again with allow_duplicate=true to add it anyway.",
                    "document": summarize(duplicate),
                }

        body: dict[str, Any] = {"title": title, "type": document_type}
        optional: dict[str, Any] = {
            "authors": clean_people(list(authors or [])) or None,
            "editors": clean_people(list(editors or [])) or None,
            "year": year,
            "source": source,
            "publisher": publisher,
            "city": city,
            "institution": institution,
            "volume": volume,
            "issue": issue,
            "pages": pages,
            "websites": [url] if url else None,
            "accessed": accessed,
            "identifiers": identifiers or None,
            "abstract": abstract,
            "keywords": list(keywords) if keywords else None,
            "tags": list(tags) if tags else None,
        }
        body.update({k: v for k, v in optional.items() if v not in (None, "")})
        body.update(extra_fields or {})

        created = await self._create_document(body)
        result: dict[str, Any] = {"status": "added", "document": summarize(created)}
        if folder_id:
            result["folder"] = await self.add_to_folder(folder_id, [str(created["id"])])
        return result

    # ------------------------------------------------------------------- folders

    async def list_folders(self) -> list[dict[str, Any]]:
        items = await self._client.paginate(
            "/folders", accept=media_types.FOLDER, params={"limit": PAGE_SIZE}
        )
        return [
            {k: f.get(k) for k in ("id", "name", "parent_id", "created") if f.get(k) is not None}
            for f in items
            if isinstance(f, dict)
        ]

    async def create_folder(self, name: str, parent_id: str | None = None) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("A folder name is required.")
        folders = await self.list_folders()
        if parent_id and not any(f["id"] == parent_id for f in folders):
            raise NotFoundError(f"No folder with ID {parent_id!r}. Use list_folders to find it.")
        for folder in folders:
            if fold(str(folder.get("name"))) == fold(name) and folder.get("parent_id") == parent_id:
                return {"status": "already_exists", "folder": folder}
        body: dict[str, Any] = {"name": name}
        if parent_id:
            body["parent_id"] = parent_id
        response = await self._client.request(
            "POST",
            "/folders",
            json_body=body,
            accept=media_types.FOLDER,
            content_type=media_types.FOLDER,
        )
        created = _json_or_empty(response)
        folder = {k: created.get(k) for k in ("id", "name", "parent_id") if created.get(k)}
        return {"status": "created", "folder": folder or body}

    async def folder_document_ids(self, folder_id: str) -> list[str]:
        items = await self._client.paginate(
            f"/folders/{_path_id(folder_id)}/documents",
            accept=media_types.DOCUMENT,
            params={"limit": PAGE_SIZE},
        )
        return [str(item["id"]) for item in items if isinstance(item, dict) and item.get("id")]

    async def add_to_folder(self, folder_id: str, document_ids: Sequence[str]) -> dict[str, Any]:
        if not document_ids:
            raise ValueError("Give at least one document ID.")
        present = set(await self.folder_document_ids(folder_id))
        added: list[str] = []
        already: list[str] = []
        not_found: list[str] = []
        for document_id in dict.fromkeys(document_ids):  # de-duplicate, keep order
            if document_id in present:
                already.append(document_id)
                continue
            try:
                await self._client.request(
                    "POST",
                    f"/folders/{_path_id(folder_id)}/documents",
                    json_body={"id": document_id},
                    content_type=media_types.DOCUMENT,
                )
            except NotFoundError:
                not_found.append(document_id)
                continue
            added.append(document_id)
        return {
            "folder_id": folder_id,
            "added": added,
            "already_in_folder": already,
            "not_found": not_found,
        }

    async def list_folder_documents(self, folder_id: str, limit: int = 100) -> dict[str, Any]:
        ids = await self.folder_document_ids(folder_id)
        limit = max(1, min(limit, MAX_EXPORT_DOCUMENTS))
        documents = await self._documents_by_id(ids[:limit])
        return {
            "folder_id": folder_id,
            "total": len(ids),
            "returned": len(documents),
            "documents": [summarize(doc) for doc in documents],
        }

    # --------------------------------------------------------------------- files

    async def attach_pdf(self, document_id: str, file_path: str) -> dict[str, Any]:
        path, content = await asyncio.to_thread(_read_pdf, file_path)
        size = len(content)

        # Fails with a clear 404 message if the document does not exist.
        document = await self._client.get_json(
            f"/documents/{_path_id(document_id)}", accept=media_types.DOCUMENT
        )
        sha1 = hashlib.sha1(content, usedforsecurity=False).hexdigest()
        for existing in await self._files_for(document_id):
            same_hash = existing.get("filehash") == sha1
            same_file = existing.get("file_name") == path.name and existing.get("size") == size
            if same_hash or same_file:
                return {
                    "status": "already_attached",
                    "message": "This PDF is already attached to the document; "
                    "nothing was uploaded.",
                    "file": {k: existing.get(k) for k in ("id", "file_name", "size")},
                }

        response = await self._client.request(
            "POST",
            "/files",
            content=content,
            accept=media_types.FILE,
            content_type=mimetypes.types_map.get(".pdf", "application/pdf"),
            headers={
                "Content-Disposition": _content_disposition(path.name),
                "Link": f'<{API_BASE_URL}/documents/{_path_id(document_id)}>; rel="document"',
            },
        )
        uploaded = _json_or_empty(response)
        return {
            "status": "attached",
            "document": summarize(document),
            "file": {
                "id": uploaded.get("id"),
                "file_name": uploaded.get("file_name", path.name),
                "size": uploaded.get("size", size),
            },
        }

    # -------------------------------------------------------------------- BibTeX

    async def export_bibtex(
        self,
        *,
        folder_id: str | None = None,
        document_ids: Sequence[str] | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        if bool(folder_id) == bool(document_ids):
            raise ValueError("Give either folder_id or document_ids (exactly one of them).")
        ids = (
            await self.folder_document_ids(folder_id)
            if folder_id
            else list(dict.fromkeys(document_ids or []))
        )
        if len(ids) > MAX_EXPORT_DOCUMENTS:
            raise ValueError(
                f"Too many documents ({len(ids)}); export at most {MAX_EXPORT_DOCUMENTS} at once."
            )
        target = _check_output_path(output_path) if output_path else None

        semaphore = asyncio.Semaphore(EXPORT_CONCURRENCY)

        async def fetch(document_id: str) -> tuple[str, str | dict[str, Any] | None]:
            async with semaphore:
                return document_id, await self._bibtex_or_metadata(document_id)

        results = await asyncio.gather(*(fetch(i) for i in ids))
        from_api = [entry for _, entry in results if isinstance(entry, str)]
        to_generate = [entry for _, entry in results if isinstance(entry, dict)]
        missing = [document_id for document_id, entry in results if entry is None]

        parts = [entry.strip() for entry in from_api]
        if to_generate:
            parts.append(bibtex.render_bibliography(to_generate).strip())
        text = "\n\n".join(p for p in parts if p) + ("\n" if parts else "")

        source = "mendeley" if not to_generate else "generated" if not from_api else "mixed"
        result: dict[str, Any] = {
            "count": len(from_api) + len(to_generate),
            "source": source,
            "missing_ids": missing,
        }
        if target is not None:
            await asyncio.to_thread(_write_new_file, target, text)
            result["written_to"] = str(target)
        else:
            result["bibtex"] = text
        return result

    # ----------------------------------------------------------------- internals

    async def _catalog_record(self, doi: str) -> dict[str, Any]:
        not_found = NotFoundError(
            f"DOI {doi} was not found in the Mendeley catalog. Check the DOI, or add the "
            "document with add_document_manual."
        )
        try:
            data = await self._client.get_json(
                "/catalog", accept=media_types.DOCUMENT, params={"doi": doi, "view": "bib"}
            )
        except NotFoundError:
            raise not_found from None
        record = data[0] if isinstance(data, list) and data else data
        if not isinstance(record, dict) or not record:
            raise not_found
        return record

    async def _create_document(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.request(
            "POST",
            "/documents",
            json_body=body,
            accept=media_types.DOCUMENT,
            content_type=media_types.DOCUMENT,
        )
        created = _json_or_empty(response)
        if created.get("id"):
            return created
        location = response.headers.get("Location")
        if location:
            fetched = await self._client.get_json(location, accept=media_types.DOCUMENT)
            if isinstance(fetched, dict):
                return fetched
        raise ApiError(response.status_code, "Mendeley did not return the new document's ID.")

    async def _find_duplicate(
        self, title: str, year: int | None, doi: str | None
    ) -> dict[str, Any] | None:
        wanted_title = fold(title)
        for doc in await self.all_documents():
            if doi and doi_key((doc.get("identifiers") or {}).get("doi")) == doi_key(doi):
                return doc
            same_title = fold(str(doc.get("title") or "")) == wanted_title
            if same_title and (year is None or doc.get("year") in (None, year)):
                return doc
        return None

    async def _files_for(self, document_id: str) -> list[dict[str, Any]]:
        items = await self._client.paginate(
            "/files", accept=media_types.FILE, params={"document_id": document_id}
        )
        return [item for item in items if isinstance(item, dict)]

    async def _documents_by_id(self, ids: Sequence[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        by_id = {str(doc.get("id")): doc for doc in await self.all_documents()}
        documents: list[dict[str, Any]] = []
        for document_id in ids:
            doc = by_id.get(document_id)
            if doc is None:  # e.g. a group document not returned by /documents
                try:
                    doc = await self._client.get_json(
                        f"/documents/{_path_id(document_id)}", accept=media_types.DOCUMENT
                    )
                except NotFoundError:
                    continue
            documents.append(doc)
        return documents

    async def _bibtex_or_metadata(self, document_id: str) -> str | dict[str, Any] | None:
        """BibTeX text from the API, metadata for local generation, or None if missing."""
        path = f"/documents/{_path_id(document_id)}"
        try:
            if self._api_bibtex_works is not False:
                try:
                    response = await self._client.request(
                        "GET", path, params={"view": "bib"}, accept=media_types.BIBTEX
                    )
                    if response.text.lstrip().startswith("@"):
                        self._api_bibtex_works = True
                        return response.text
                    self._api_bibtex_works = False
                except ApiError as exc:
                    if exc.status_code not in API_BIBTEX_FAILURE_STATUSES:
                        raise
                    self._api_bibtex_works = False
            data = await self._client.get_json(
                path, accept=media_types.DOCUMENT, params={"view": "bib"}
            )
        except NotFoundError:
            return None
        return data if isinstance(data, dict) else None


def _path_id(value: str) -> str:
    """Encode an ID for use in a URL path, so it cannot change the path."""
    value = value.strip()
    if not value:
        raise ValueError("An ID is required.")
    return quote(value, safe="")


def _json_or_empty(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _validate_date(value: str) -> None:
    from datetime import date

    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"accessed must be a date like 2026-01-31, got {value!r}.") from None


def _read_pdf(file_path: str) -> tuple[Path, bytes]:
    """Validate and read a local PDF (blocking; run it in a worker thread)."""
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise ValueError(
            f"No file at {str(path)!r}. Give the full path of a PDF on the computer "
            "where this server runs."
        )
    if path.suffix.lower() != ".pdf":
        raise ValueError("Only PDF files can be attached (the file must end in .pdf).")
    if path.stat().st_size > MAX_PDF_BYTES:
        raise ValueError(f"The PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB.")
    content = path.read_bytes()
    if not content.startswith(b"%PDF-"):
        raise ValueError("The file does not look like a PDF (missing %PDF- header).")
    return path, content


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "")
    ascii_name = ascii_name.replace("?", "_")
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def _check_output_path(output_path: str) -> Path:
    path = Path(output_path).expanduser()
    if path.suffix.lower() != ".bib":
        raise ValueError("output_path must end in .bib.")
    if path.exists():
        raise ValueError(
            f"{str(path)!r} already exists. Choose a new file name; existing files are never "
            "overwritten."
        )
    if not path.parent.is_dir():
        raise ValueError(f"The folder {str(path.parent)!r} does not exist.")
    return path


def _write_new_file(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:  # "x": never overwrite
        handle.write(text)
