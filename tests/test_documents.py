from __future__ import annotations

import pytest

from mendeley_mcp_connector.services.documents import (
    document_from_catalog,
    doi_key,
    fold,
    matches,
    normalize_doi,
    summarize,
)

DOC = {
    "id": "doc-1",
    "title": "Políticas públicas de vivienda",
    "type": "journal",
    "authors": [{"first_name": "Ana", "last_name": "Pérez Muñoz"}],
    "year": 2021,
    "source": "Revista Ficticia",
    "identifiers": {"doi": "10.1234/FAKE.2021.001"},
    "keywords": ["housing"],
}


@pytest.mark.parametrize(
    "raw",
    [
        "10.1234/fake.2021.001",
        "  https://doi.org/10.1234/fake.2021.001 ",
        "http://dx.doi.org/10.1234/fake.2021.001",
        "doi:10.1234/fake.2021.001",
        "DOI: 10.1234/fake.2021.001",
    ],
)
def test_normalize_doi_accepts_common_forms(raw: str) -> None:
    assert normalize_doi(raw) == "10.1234/fake.2021.001"


@pytest.mark.parametrize("raw", ["", "1234/abc", "https://example.com/paper", "10.12/x"])
def test_normalize_doi_rejects_non_dois(raw: str) -> None:
    with pytest.raises(ValueError, match="does not look like a DOI"):
        normalize_doi(raw)


def test_doi_key_is_case_insensitive() -> None:
    assert doi_key("10.1234/ABC") == doi_key("https://doi.org/10.1234/abc")
    assert doi_key(None) is None
    assert doi_key("not a doi") is None


def test_fold_removes_accents_and_case() -> None:
    assert fold("Pérez MUÑOZ") == "perez munoz"


@pytest.mark.parametrize(
    "criteria",
    [
        {"query": "politicas vivienda"},
        {"title": "VIVIENDA"},
        {"author": "munoz"},
        {"author": "Ana Pérez"},
        {"year": 2021},
        {"year_from": 2020, "year_to": 2022},
        {"doi": "https://doi.org/10.1234/fake.2021.001"},
        {"query": "housing"},
        {"query": "revista", "author": "perez", "year": 2021},
    ],
)
def test_matches_positive(criteria: dict[str, object]) -> None:
    assert matches(DOC, **criteria)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "criteria",
    [
        {"query": "transporte"},
        {"title": "Revista"},  # in the source, not the title
        {"author": "garcia"},
        {"year": 2020},
        {"year_from": 2022},
        {"doi": "10.1234/other"},
        {"query": "vivienda", "year": 1999},
    ],
)
def test_matches_negative(criteria: dict[str, object]) -> None:
    assert not matches(DOC, **criteria)  # type: ignore[arg-type]


def test_summarize_is_compact() -> None:
    summary = summarize(DOC)
    assert summary == {
        "id": "doc-1",
        "title": "Políticas públicas de vivienda",
        "type": "journal",
        "authors": ["Pérez Muñoz, Ana"],
        "year": 2021,
        "source": "Revista Ficticia",
        "doi": "10.1234/FAKE.2021.001",
    }


def test_summarize_truncates_long_author_lists() -> None:
    many = {"authors": [{"last_name": f"Author{i}"} for i in range(15)]}
    assert summarize(many)["authors"][-1] == "et al."
    assert len(summarize(many)["authors"]) == 11


def test_document_from_catalog_copies_only_library_fields() -> None:
    record = {
        "id": "catalog-id",
        "link": "https://www.mendeley.com/catalogue/fake",
        "reader_count": 99,
        "title": "A fake paper",
        "type": "journal",
        "authors": [{"first_name": "Test", "last_name": "Author"}, {"first_name": "No last"}],
        "year": 2020,
        "identifiers": {"issn": "0000-0000"},
        "abstract": "",
    }

    body = document_from_catalog(record, "10.1234/fake")

    assert body == {
        "title": "A fake paper",
        "type": "journal",
        "authors": [{"first_name": "Test", "last_name": "Author"}],
        "year": 2020,
        "identifiers": {"issn": "0000-0000", "doi": "10.1234/fake"},
    }


def test_document_from_catalog_fixes_unknown_type_and_requires_title() -> None:
    assert document_from_catalog({"title": "T", "type": "???"}, "10.1/x")["type"] == "journal"
    with pytest.raises(ValueError, match="no title"):
        document_from_catalog({"type": "journal"}, "10.1234/x")
