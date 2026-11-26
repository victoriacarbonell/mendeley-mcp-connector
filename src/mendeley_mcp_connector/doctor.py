"""Read-only checks of which Mendeley endpoints work for the logged-in account.

The official documentation is not dated and some sections carry ambiguous
"not implemented" markers, so these checks confirm what the API actually does
before the tools rely on it. Nothing is created, changed or deleted, and the
output contains no personal data, so it can be shared when reporting issues.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from mendeley_mcp_connector.api import media_types
from mendeley_mcp_connector.api.client import MendeleyClient
from mendeley_mcp_connector.errors import ApiError, MendeleyError, NotFoundError

# Public, widely cited paper used only to test the catalog lookup.
SAMPLE_DOI = "10.1038/nature14539"
# A DOI that cannot exist, used to see whether /documents honours a doi filter.
NONEXISTENT_DOI = "10.0000/mendeley-mcp-connector-doctor-check"

Status = Literal["ok", "fail", "info"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str


async def run_checks(client: MendeleyClient) -> list[CheckResult]:
    results: list[CheckResult] = []

    async def check(name: str, probe: Callable[[], Awaitable[CheckResult]]) -> bool:
        try:
            result = await probe()
        except MendeleyError as exc:
            result = CheckResult(name, "fail", _short(exc))
        results.append(result)
        return result.status != "fail"

    async def profile() -> CheckResult:
        data = await client.get_json("/profiles/me", accept=media_types.PROFILE)
        has_id = isinstance(data, dict) and bool(data.get("id"))
        return CheckResult(
            "GET /profiles/me", "ok" if has_id else "fail", "profile returned" if has_id else ""
        )

    if not await check("GET /profiles/me", profile):
        return results  # authentication problem: the rest would fail the same way

    library_size: int | None = None

    async def documents() -> CheckResult:
        nonlocal library_size
        response = await client.request(
            "GET", "/documents", params={"limit": 1}, accept=media_types.DOCUMENT
        )
        count = response.headers.get("Mendeley-Count")
        library_size = int(count) if count and count.isdigit() else len(response.json())
        return CheckResult("GET /documents", "ok", f"library size reported: {library_size}")

    async def library_search() -> CheckResult:
        try:
            await client.request(
                "GET",
                "/search/documents",
                params={"query": "a", "limit": 1},
                accept=media_types.DOCUMENT,
            )
        except (NotFoundError, ApiError) as exc:
            return CheckResult(
                "GET /search/documents",
                "info",
                f"not available ({_short(exc)}); local filtering will be used",
            )
        return CheckResult("GET /search/documents", "ok", "library search endpoint works")

    async def doi_filter() -> CheckResult:
        try:
            response = await client.request(
                "GET",
                "/documents",
                params={"doi": NONEXISTENT_DOI, "limit": 1},
                accept=media_types.DOCUMENT,
            )
        except ApiError as exc:
            return CheckResult("GET /documents?doi=", "info", f"rejected ({_short(exc)})")
        returned = len(response.json())
        if returned == 0 and library_size:
            verdict = "filter seems supported (no match for a fake DOI)"
        elif returned:
            verdict = "parameter ignored (documents returned for a fake DOI)"
        else:
            verdict = "inconclusive (library is empty)"
        return CheckResult("GET /documents?doi=", "info", verdict)

    async def bibtex() -> CheckResult:
        if not library_size:
            return CheckResult("BibTeX export", "info", "skipped (library is empty)")
        response = await client.request(
            "GET",
            "/documents",
            params={"view": "bib", "limit": 1},
            accept=media_types.BIBTEX,
        )
        looks_like_bibtex = response.text.lstrip().startswith("@")
        return CheckResult(
            "BibTeX export",
            "ok" if looks_like_bibtex else "fail",
            f"content-type {response.headers.get('Content-Type', '?')}",
        )

    async def catalog() -> CheckResult:
        data = await client.get_json(
            "/catalog", params={"doi": SAMPLE_DOI}, accept=media_types.DOCUMENT
        )
        record = data[0] if isinstance(data, list) and data else data
        found = isinstance(record, dict) and bool(record.get("title"))
        return CheckResult(
            "GET /catalog?doi=", "ok" if found else "fail", "record found" if found else ""
        )

    async def folders() -> CheckResult:
        data = await client.get_json("/folders", params={"limit": 1}, accept=media_types.FOLDER)
        return CheckResult("GET /folders", "ok", f"{len(data)} folder(s) in first page")

    await check("GET /documents", documents)
    await check("GET /search/documents", library_search)
    await check("GET /documents?doi=", doi_filter)
    await check("BibTeX export", bibtex)
    await check("GET /catalog?doi=", catalog)
    await check("GET /folders", folders)
    return results


def _short(exc: Exception) -> str:
    text = str(exc)
    return text if len(text) <= 160 else text[:157] + "..."
