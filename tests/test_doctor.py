from __future__ import annotations

import httpx
import respx

from mendeley_mcp_connector.api.client import MendeleyClient
from mendeley_mcp_connector.auth.token_store import TokenStore
from mendeley_mcp_connector.config import API_BASE_URL
from mendeley_mcp_connector.doctor import NONEXISTENT_DOI, SAMPLE_DOI, run_checks
from tests.conftest import make_credentials

BASE = API_BASE_URL


async def _no_sleep(_delay: float) -> None:
    return None


@respx.mock
async def test_doctor_reports_each_capability(store: TokenStore) -> None:
    store.save(make_credentials(expires_at=9_999_999_999.0))
    respx.get(f"{BASE}/profiles/me").mock(return_value=httpx.Response(200, json={"id": "p1"}))
    respx.get(f"{BASE}/documents", params={"doi": NONEXISTENT_DOI}).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"{BASE}/documents", params={"view": "bib"}).mock(
        return_value=httpx.Response(
            200,
            text="@article{fake2026, title={Fake}}",
            headers={"Content-Type": "application/x-bibtex"},
        )
    )
    respx.get(f"{BASE}/documents").mock(
        return_value=httpx.Response(200, json=[{"id": "d1"}], headers={"Mendeley-Count": "42"})
    )
    respx.get(f"{BASE}/search/documents").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/catalog", params={"doi": SAMPLE_DOI}).mock(
        return_value=httpx.Response(200, json=[{"title": "A fake title"}])
    )
    respx.get(f"{BASE}/folders").mock(return_value=httpx.Response(200, json=[]))

    async with MendeleyClient(store, sleep=_no_sleep) as client:
        results = {r.name: r for r in await run_checks(client)}

    assert results["GET /profiles/me"].status == "ok"
    assert "42" in results["GET /documents"].detail
    assert results["GET /search/documents"].status == "info"
    assert "supported" in results["GET /documents?doi="].detail
    assert results["BibTeX export"].status == "ok"
    assert results["GET /catalog?doi="].status == "ok"
    assert results["GET /folders"].status == "ok"


@respx.mock
async def test_doctor_stops_after_auth_failure(store: TokenStore) -> None:
    store.save(make_credentials(expires_at=9_999_999_999.0))
    respx.post(f"{BASE}/oauth/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    respx.get(f"{BASE}/profiles/me").mock(return_value=httpx.Response(401))

    async with MendeleyClient(store, sleep=_no_sleep) as client:
        results = await run_checks(client)

    assert [r.status for r in results] == ["fail"]
