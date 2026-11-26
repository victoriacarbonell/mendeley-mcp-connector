from __future__ import annotations

from mendeley_mcp_connector.services.bibtex import citation_key, escape, render_bibliography

ARTICLE = {
    "type": "journal",
    "title": "Costs & benefits of 100% fake data",
    "authors": [
        {"first_name": "Ana", "last_name": "Pérez"},
        {"first_name": "Test", "last_name": "Author"},
    ],
    "year": 2024,
    "month": 3,
    "source": "Journal of Fake Studies",
    "volume": "12",
    "issue": "3",
    "pages": "1-10",
    "identifiers": {"doi": "10.1234/fake_2024"},
}


def test_escape_special_characters() -> None:
    assert escape("A & B 50% #1 a_b $5") == r"A \& B 50\% \#1 a\_b \$5"
    assert escape("unbalanced { brace") == "unbalanced brace"


def test_article_entry() -> None:
    text = render_bibliography([ARTICLE])

    assert text.startswith("@article{perez2024costs,\n")
    assert "  title = {Costs \\& benefits of 100\\% fake data}" in text
    assert "  author = {Pérez, Ana and Author, Test}" in text
    assert "  journal = {Journal of Fake Studies}" in text
    assert "  month = mar" not in text  # month is a braced value
    assert "  month = {mar}" in text
    assert "  number = {3}" in text
    assert "  doi = {10.1234/fake\\_2024}" in text
    assert text.endswith("}\n")


def test_statute_becomes_misc_with_note_and_url() -> None:
    statute = {
        "type": "statute",
        "title": "Fake Act on Example Matters",
        "authors": [{"last_name": "Fictional Parliament"}],
        "year": 2023,
        "source": "Official Fake Gazette",
        "websites": ["https://example.org/fake-act"],
        "accessed": "2026-01-15T00:00:00.000Z",
    }

    text = render_bibliography([statute])

    assert text.startswith("@misc{")
    assert "  author = {Fictional Parliament}" in text
    assert "  howpublished = {Official Fake Gazette}" in text
    assert "  url = {https://example.org/fake-act}" in text
    assert "  urldate = {2026-01-15}" in text
    assert "  note = {Statute}" in text


def test_duplicate_keys_get_suffixes() -> None:
    text = render_bibliography([ARTICLE, ARTICLE, ARTICLE])
    assert "{perez2024costs," in text
    assert "{perez2024costsa," in text
    assert "{perez2024costsb," in text


def test_stored_citation_key_is_used() -> None:
    assert citation_key({"citation_key": "Fake:2024 x"}) == "Fake2024x"
    assert citation_key({}) == "item"


def test_empty_bibliography() -> None:
    assert render_bibliography([]) == ""
