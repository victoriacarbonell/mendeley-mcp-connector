"""Local BibTeX generation, used when the API does not return BibTeX itself."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from mendeley_mcp_connector.services.documents import fold, format_person

# Mendeley document type -> BibTeX entry type.
ENTRY_TYPES: dict[str, str] = {
    "journal": "article",
    "magazine_article": "article",
    "newspaper_article": "article",
    "book": "book",
    "book_section": "incollection",
    "encyclopedia_article": "incollection",
    "conference_proceedings": "inproceedings",
    "thesis": "phdthesis",
    "report": "techreport",
    "working_paper": "techreport",
}

# Which BibTeX field receives Mendeley's "source" (journal, book or proceedings title).
SOURCE_FIELD: dict[str, str] = {
    "article": "journal",
    "incollection": "booktitle",
    "inproceedings": "booktitle",
}

_SPECIAL = re.compile(r"([&%$#_])")
_KEY_CHARS = re.compile(r"[^A-Za-z0-9]")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def escape(value: str) -> str:
    """Escape characters with special meaning in BibTeX and drop unbalanced braces."""
    text = _SPECIAL.sub(r"\\\1", value.replace("\\", "\\textbackslash{}"))
    if text.count("{") != text.count("}"):
        text = text.replace("{", "").replace("}", "")
    return " ".join(text.split())


def citation_key(document: Mapping[str, Any]) -> str:
    stored = document.get("citation_key")
    if isinstance(stored, str) and stored.strip():
        return _KEY_CHARS.sub("", stored) or "item"
    authors = document.get("authors") or []
    first_author = authors[0].get("last_name", "") if authors else ""
    words = [w for w in fold(str(document.get("title") or "")).split() if len(w) > 3]
    raw = f"{fold(str(first_author))}{document.get('year') or ''}{words[0] if words else ''}"
    return _KEY_CHARS.sub("", raw) or "item"


def to_bibtex(document: Mapping[str, Any], key: str) -> str:
    entry_type = ENTRY_TYPES.get(str(document.get("type")), "misc")
    fields: list[tuple[str, str]] = []

    def add(name: str, value: object) -> None:
        if isinstance(value, int) and not isinstance(value, bool):
            fields.append((name, str(value)))
        elif isinstance(value, str) and value.strip():
            fields.append((name, escape(value)))

    add("title", document.get("title"))
    for field, people_key in (("author", "authors"), ("editor", "editors")):
        people = [
            escape(format_person(p)) for p in document.get(people_key) or [] if isinstance(p, dict)
        ]
        if people:
            fields.append((field, " and ".join(people)))
    add(SOURCE_FIELD.get(entry_type, "howpublished"), document.get("source"))
    add("year", document.get("year"))
    month = document.get("month")
    if isinstance(month, int) and 1 <= month <= 12:
        fields.append(("month", _MONTHS[month - 1]))
    add("volume", document.get("volume"))
    add("number", document.get("issue"))
    add("pages", document.get("pages"))
    add("edition", _first(document.get("edition")))
    add("series", document.get("series"))
    add("chapter", document.get("chapter"))
    add("publisher", document.get("publisher"))
    add("address", document.get("city"))
    add("institution" if entry_type == "techreport" else "school", document.get("institution"))
    identifiers = document.get("identifiers") or {}
    for name in ("doi", "isbn", "issn"):
        add(name, identifiers.get(name) if isinstance(identifiers, dict) else None)
    add("url", _first(document.get("websites")))
    accessed = document.get("accessed")
    if isinstance(accessed, str):
        add("urldate", accessed[:10])
    if entry_type == "misc" and document.get("type") not in (None, "generic"):
        add("note", str(document.get("type")).replace("_", " ").capitalize())

    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
    return f"@{entry_type}{{{key},\n{body}\n}}"


_ENTRY_HEAD = re.compile(r"^\s*@\s*\w+\s*\{\s*([^,\s]*)\s*,")


def render_bibliography(documents: Iterable[Mapping[str, Any]]) -> str:
    """BibTeX for several documents, with unique citation keys."""
    return merge_bibliography([], documents)


def merge_bibliography(
    api_entries: Iterable[str], documents: Iterable[Mapping[str, Any]] = ()
) -> str:
    """Join BibTeX returned by the API with locally generated entries.

    Every citation key in the result is unique (compared case-insensitively, as
    BibTeX and biber do): a repeated key gets a suffix a, b, c... wherever the
    entry came from, so LaTeX never silently cites the wrong work.
    """
    taken: set[str] = set()

    def unique(base: str) -> str:
        base = base or "item"
        candidate, index = base, 0
        while candidate.casefold() in taken:
            index += 1
            candidate = f"{base}{_suffix(index)}"
        taken.add(candidate.casefold())
        return candidate

    entries: list[str] = []
    for text in api_entries:
        text = text.strip()
        match = _ENTRY_HEAD.match(text)
        if match:
            key = unique(match.group(1))
            text = text[: match.start(1)] + key + text[match.end(1) :]
        if text:
            entries.append(text)
    for document in documents:
        entries.append(to_bibtex(document, unique(citation_key(document))))
    return "\n\n".join(entries) + ("\n" if entries else "")


def _suffix(index: int) -> str:
    """1 -> 'a', 2 -> 'b', ..., 26 -> 'z', 27 -> 'aa'."""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("a") + rem) + letters
    return letters


def _first(value: object) -> object:
    if isinstance(value, list):
        return value[0] if value else None
    return value
