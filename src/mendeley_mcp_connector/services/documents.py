"""Pure helpers for Mendeley document data (no network access)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

# Allowed values of the document "type" attribute (https://dev.mendeley.com/methods/).
DOCUMENT_TYPES = frozenset(
    {
        "journal",
        "book",
        "generic",
        "book_section",
        "conference_proceedings",
        "working_paper",
        "report",
        "web_page",
        "thesis",
        "magazine_article",
        "statute",
        "patent",
        "newspaper_article",
        "computer_program",
        "hearing",
        "television_broadcast",
        "encyclopedia_article",
        "case",
        "film",
        "bill",
    }
)

# Writable document attributes documented by Mendeley, other than the ones that the
# manual-entry tool exposes as named parameters. Used to validate ``extra_fields``.
EXTRA_DOCUMENT_FIELDS = frozenset(
    {
        "month",
        "day",
        "revision",
        "edition",
        "series",
        "chapter",
        "citation_key",
        "source_type",
        "language",
        "short_title",
        "reprint_edition",
        "genre",
        "country",
        "translators",
        "series_editor",
        "code",
        "medium",
        "user_context",
        "department",
        "patent_owner",
        "patent_application_number",
        "patent_legal_status",
        "read",
        "starred",
        "authored",
        "confirmed",
        "hidden",
    }
)

# Catalog attributes that make sense to copy into a new library document.
CATALOG_FIELDS_TO_COPY = (
    "title",
    "type",
    "authors",
    "editors",
    "year",
    "month",
    "day",
    "source",
    "identifiers",
    "abstract",
    "keywords",
    "pages",
    "volume",
    "issue",
    "publisher",
    "city",
    "edition",
    "institution",
    "series",
    "chapter",
    "websites",
)

_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_DOI_SHAPE = re.compile(r"^10\.\d{4,9}/\S+$")
MAX_AUTHORS_IN_SUMMARY = 10


def normalize_doi(value: str) -> str:
    """Return the bare DOI (``10.xxxx/...``) or raise ``ValueError``.

    Accepts the common forms people paste: ``https://doi.org/...``, ``doi:...``.
    """
    doi = _DOI_PREFIX.sub("", value.strip()).strip()
    if not _DOI_SHAPE.match(doi):
        raise ValueError(
            f"{value!r} does not look like a DOI. A DOI starts with '10.' followed by a "
            "slash, for example 10.1000/xyz123."
        )
    return doi


def doi_key(value: object) -> str | None:
    """Comparison key for a DOI (DOIs are case-insensitive), or None if not a DOI."""
    if not isinstance(value, str):
        return None
    try:
        return normalize_doi(value).casefold()
    except ValueError:
        return None


def fold(text: str) -> str:
    """Lower-case and strip accents, so 'Muñoz' matches 'munoz'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def format_person(person: Mapping[str, Any]) -> str:
    last = str(person.get("last_name") or "").strip()
    first = str(person.get("first_name") or "").strip()
    return f"{last}, {first}" if first else last


def clean_people(people: object) -> list[dict[str, str]]:
    """Keep only valid person objects (Mendeley requires ``last_name``)."""
    cleaned: list[dict[str, str]] = []
    if not isinstance(people, list):
        return cleaned
    for person in people:
        if not isinstance(person, Mapping):
            continue
        last = str(person.get("last_name") or "").strip()
        if not last:
            continue
        entry = {"last_name": last}
        first = str(person.get("first_name") or "").strip()
        if first:
            entry["first_name"] = first
        cleaned.append(entry)
    return cleaned


def summarize(document: Mapping[str, Any]) -> dict[str, Any]:
    """Compact view of a document for lists and search results."""
    authors = [format_person(p) for p in document.get("authors") or [] if isinstance(p, Mapping)]
    if len(authors) > MAX_AUTHORS_IN_SUMMARY:
        authors = [*authors[:MAX_AUTHORS_IN_SUMMARY], "et al."]
    identifiers = document.get("identifiers")
    doi = identifiers.get("doi") if isinstance(identifiers, Mapping) else None
    summary: dict[str, Any] = {
        "id": document.get("id"),
        "title": document.get("title"),
        "type": document.get("type"),
        "authors": authors,
        "year": document.get("year"),
        "source": document.get("source"),
        "doi": doi,
    }
    return {key: value for key, value in summary.items() if value not in (None, [], "")}


def searchable_text(document: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "source", "abstract", "publisher", "institution"):
        value = document.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("authors", "editors"):
        parts.extend(format_person(p) for p in document.get(key) or [] if isinstance(p, Mapping))
    parts.extend(_strings(document.get("keywords")))
    parts.extend(_strings(document.get("tags")))
    identifiers = document.get("identifiers")
    if isinstance(identifiers, Mapping):
        parts.extend(_strings(identifiers.values()))
    return fold(" ".join(parts))


def matches(
    document: Mapping[str, Any],
    *,
    query: str | None = None,
    title: str | None = None,
    author: str | None = None,
    year: int | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    doi: str | None = None,
) -> bool:
    """True if the document satisfies every given criterion (all are optional, ANDed)."""
    if doi is not None:
        identifiers = document.get("identifiers")
        stored = identifiers.get("doi") if isinstance(identifiers, Mapping) else None
        if doi_key(stored) != doi_key(doi):
            return False
    doc_year = document.get("year")
    if year is not None and doc_year != year:
        return False
    if year_from is not None and not (isinstance(doc_year, int) and doc_year >= year_from):
        return False
    if year_to is not None and not (isinstance(doc_year, int) and doc_year <= year_to):
        return False
    if title is not None and not _all_terms_in(title, fold(str(document.get("title") or ""))):
        return False
    if author is not None:
        people = " ".join(
            format_person(p)
            for key in ("authors", "editors")
            for p in document.get(key) or []
            if isinstance(p, Mapping)
        )
        if not _all_terms_in(author, fold(people)):
            return False
    return query is None or _all_terms_in(query, searchable_text(document))


def document_from_catalog(record: Mapping[str, Any], doi: str) -> dict[str, Any]:
    """Build the body of ``POST /documents`` from a catalog record."""
    body: dict[str, Any] = {}
    for key in CATALOG_FIELDS_TO_COPY:
        value = record.get(key)
        if value not in (None, "", [], {}):
            body[key] = value
    for key in ("authors", "editors"):
        if key in body:
            people = clean_people(body[key])
            if people:
                body[key] = people
            else:
                del body[key]
    if body.get("type") not in DOCUMENT_TYPES:
        body["type"] = "journal"
    identifiers = dict(body.get("identifiers") or {})
    identifiers["doi"] = doi
    body["identifiers"] = identifiers
    if not body.get("title"):
        raise ValueError(f"The catalog record for DOI {doi} has no title; add it manually.")
    return body


def _all_terms_in(needle: str, haystack: str) -> bool:
    return all(term in haystack for term in fold(needle).split())


def _strings(values: object) -> Iterable[str]:
    if isinstance(values, str):
        return [values]
    if isinstance(values, Iterable):
        return [v for v in values if isinstance(v, str)]
    return []
