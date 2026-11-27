from __future__ import annotations

import httpx
import pytest
import respx

from mendeley_mcp_connector.api import media_types
from mendeley_mcp_connector.api.client import MendeleyClient
from mendeley_mcp_connector.auth.token_store import TokenStore
from mendeley_mcp_connector.config import API_BASE_URL, TOKEN_URL
from mendeley_mcp_connector.errors import (
    ApiError,
    AuthenticationExpiredError,
    NotAuthenticatedError,
    NotFoundError,
    RateLimitError,
)
from tests.conftest import make_credentials

NOW = 5_000.0
DOCS_URL = f"{API_BASE_URL}/documents"


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def sleeper() -> SleepRecorder:
    return SleepRecorder()


def make_client(store: TokenStore, sleeper: SleepRecorder, **kwargs: object) -> MendeleyClient:
    return MendeleyClient(store, sleep=sleeper, clock=lambda: NOW, **kwargs)  # type: ignore[arg-type]


@respx.mock
async def test_sends_bearer_token_and_accept_header(
    store: TokenStore, sleeper: SleepRecorder
) -> None:
    store.save(make_credentials(expires_at=NOW + 3600))
    route = respx.get(DOCS_URL).mock(return_value=httpx.Response(200, json=[]))

    async with make_client(store, sleeper) as client:
        assert await client.get_json("/documents", accept=media_types.DOCUMENT) == []

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer fake-access-token"
    assert request.headers["Accept"] == media_types.DOCUMENT


@respx.mock
async def test_none_params_are_dropped(store: TokenStore, sleeper: SleepRecorder) -> None:
    store.save(make_credentials(expires_at=NOW + 3600))
    route = respx.get(DOCS_URL).mock(return_value=httpx.Response(200, json=[]))

    async with make_client(store, sleeper) as client:
        await client.get_json(
            "/documents", accept=media_types.DOCUMENT, params={"limit": 5, "view": None}
        )

    assert dict(route.calls.last.request.url.params) == {"limit": "5"}


async def test_not_logged_in(store: TokenStore, sleeper: SleepRecorder) -> None:
    async with make_client(store, sleeper) as client:
        with pytest.raises(NotAuthenticatedError, match="auth login"):
            await client.get_json("/documents", accept=media_types.DOCUMENT)


@respx.mock
async def test_refreshes_expired_token_and_persists_rotation(
    store: TokenStore, sleeper: SleepRecorder
) -> None:
    store.save(make_credentials(expires_at=NOW - 10))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "fresh", "refresh_token": "rotated", "expires_in": 3600}
        )
    )
    route = respx.get(DOCS_URL).mock(return_value=httpx.Response(200, json=[]))

    async with make_client(store, sleeper) as client:
        await client.get_json("/documents", accept=media_types.DOCUMENT)

    assert route.calls.last.request.headers["Authorization"] == "Bearer fresh"
    stored = store.load()
    assert stored is not None
    assert stored.refresh_token == "rotated"
    assert stored.expires_at == NOW + 3600


@respx.mock
async def test_401_triggers_one_refresh_and_retry(
    store: TokenStore, sleeper: SleepRecorder
) -> None:
    store.save(make_credentials(expires_at=NOW + 3600))
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
    )
    respx.get(DOCS_URL).mock(
        side_effect=[httpx.Response(401, json={"message": "expired"}), httpx.Response(200, json=[])]
    )

    async with make_client(store, sleeper) as client:
        await client.get_json("/documents", accept=media_types.DOCUMENT)

    assert token_route.call_count == 1
    stored = store.load()
    assert stored is not None
    assert stored.refresh_token == "fake-refresh-token"  # kept when not rotated


@respx.mock
async def test_persistent_401_asks_to_log_in_again(
    store: TokenStore, sleeper: SleepRecorder
) -> None:
    store.save(make_credentials(expires_at=NOW + 3600))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
    )
    respx.get(DOCS_URL).mock(return_value=httpx.Response(401, json={"message": "nope"}))

    async with make_client(store, sleeper) as client:
        with pytest.raises(AuthenticationExpiredError, match="auth login"):
            await client.get_json("/documents", accept=media_types.DOCUMENT)


@respx.mock
async def test_uses_token_refreshed_by_another_process(
    store: TokenStore, sleeper: SleepRecorder
) -> None:
    """`auth status` in a terminal refreshed (and rotated) while the server was running."""
    store.save(make_credentials(expires_at=NOW + 3600))
    token_route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(400))
    docs = respx.get(DOCS_URL).mock(return_value=httpx.Response(200, json=[]))

    async with make_client(store, sleeper) as client:
        await client.get_json("/documents", accept=media_types.DOCUMENT)  # caches credentials
        store.save(
            make_credentials(
                access_token="other-process-token",
                refresh_token="rotated-elsewhere",
                expires_at=NOW + 3600,
            )
        )
        client._credentials.expires_at = NOW - 10  # type: ignore[union-attr]
        await client.get_json("/documents", accept=media_types.DOCUMENT)

    assert not token_route.called
    assert docs.calls.last.request.headers["Authorization"] == "Bearer other-process-token"


@respx.mock
async def test_refresh_retries_with_token_rotated_meanwhile(
    store: TokenStore, sleeper: SleepRecorder
) -> None:
    store.save(make_credentials(expires_at=NOW - 10))

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        if b"rotated-elsewhere" in request.content:
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        # Simulate another process rotating the refresh token during our attempt.
        store.save(make_credentials(refresh_token="rotated-elsewhere", expires_at=NOW - 10))
        return httpx.Response(400, json={"error": "invalid_grant"})

    respx.post(TOKEN_URL).mock(side_effect=token_endpoint)
    docs = respx.get(DOCS_URL).mock(return_value=httpx.Response(200, json=[]))

    async with make_client(store, sleeper) as client:
        await client.get_json("/documents", accept=media_types.DOCUMENT)

    assert docs.calls.last.request.headers["Authorization"] == "Bearer fresh"


@respx.mock
async def test_failed_save_after_refresh_does_not_fail_request(
    store: TokenStore, sleeper: SleepRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(make_credentials(expires_at=NOW - 10))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
    )
    docs = respx.get(DOCS_URL).mock(return_value=httpx.Response(200, json=[]))

    def broken_save(_credentials: object) -> None:
        raise OSError("keyring locked")

    monkeypatch.setattr(store, "save", broken_save)
    async with make_client(store, sleeper) as client:
        await client.get_json("/documents", accept=media_types.DOCUMENT)
        await client.get_json("/documents", accept=media_types.DOCUMENT)

    assert docs.calls.last.request.headers["Authorization"] == "Bearer fresh"


@respx.mock
async def test_revoked_refresh_token(store: TokenStore, sleeper: SleepRecorder) -> None:
    store.save(make_credentials(expires_at=NOW - 10))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))

    async with make_client(store, sleeper) as client:
        with pytest.raises(AuthenticationExpiredError, match="auth login"):
            await client.get_json("/documents", accept=media_types.DOCUMENT)


@respx.mock
async def test_429_honours_retry_after(store: TokenStore, sleeper: SleepRecorder) -> None:
    store.save(make_credentials(expires_at=NOW + 3600))
    respx.get(DOCS_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429),
            httpx.Response(200, json=[]),
        ]
    )

    async with make_client(store, sleeper) as client:
        await client.get_json("/documents", accept=media_types.DOCUMENT)

    assert sleeper.delays == [7.0, 2.0]  # Retry-After, then exponential backoff (2**1)


@respx.mock
async def test_429_gives_up_after_max_retries(store: TokenStore, sleeper: SleepRecorder) -> None:
    store.save(make_credentials(expires_at=NOW + 3600))
    respx.get(DOCS_URL).mock(return_value=httpx.Response(429))

    async with make_client(store, sleeper, max_retries=2) as client:
        with pytest.raises(RateLimitError, match="3 attempts"):
            await client.get_json("/documents", accept=media_types.DOCUMENT)
    assert len(sleeper.delays) == 2


@respx.mock
async def test_503_is_not_retried_for_post(store: TokenStore, sleeper: SleepRecorder) -> None:
    store.save(make_credentials(expires_at=NOW + 3600))
    route = respx.post(DOCS_URL).mock(return_value=httpx.Response(503))

    async with make_client(store, sleeper) as client:
        with pytest.raises(ApiError, match="server error"):
            await client.request("POST", "/documents", json_body={"title": "t"})
    assert route.call_count == 1  # retrying a POST could create a duplicate document


@respx.mock
async def test_404_message_is_actionable(store: TokenStore, sleeper: SleepRecorder) -> None:
    store.save(make_credentials(expires_at=NOW + 3600))
    respx.get(f"{DOCS_URL}/missing").mock(
        return_value=httpx.Response(404, json={"message": "Document not found"})
    )

    async with make_client(store, sleeper) as client:
        with pytest.raises(NotFoundError, match="Document not found"):
            await client.get_json("/documents/missing", accept=media_types.DOCUMENT)


@respx.mock
async def test_paginate_follows_next_links(store: TokenStore, sleeper: SleepRecorder) -> None:
    store.save(make_credentials(expires_at=NOW + 3600))
    next_url = f"{DOCS_URL}?marker=abc&limit=2"
    respx.get(DOCS_URL, params={"marker": "abc"}).mock(
        return_value=httpx.Response(200, json=[{"id": "3"}])
    )
    respx.get(DOCS_URL, params={"limit": "2"}).mock(
        return_value=httpx.Response(
            200, json=[{"id": "1"}, {"id": "2"}], headers={"Link": f'<{next_url}>; rel="next"'}
        )
    )

    async with make_client(store, sleeper) as client:
        items = await client.paginate(
            "/documents", accept=media_types.DOCUMENT, params={"limit": 2}
        )

    assert [item["id"] for item in items] == ["1", "2", "3"]


@respx.mock
async def test_paginate_refuses_foreign_next_link(
    store: TokenStore, sleeper: SleepRecorder
) -> None:
    store.save(make_credentials(expires_at=NOW + 3600))
    respx.get(DOCS_URL).mock(
        return_value=httpx.Response(
            200, json=[{"id": "1"}], headers={"Link": '<https://evil.example/steal>; rel="next"'}
        )
    )

    async with make_client(store, sleeper) as client:
        with pytest.raises(ApiError, match="non-Mendeley URL"):
            await client.paginate("/documents", accept=media_types.DOCUMENT)


@respx.mock
async def test_paginate_respects_max_items(store: TokenStore, sleeper: SleepRecorder) -> None:
    store.save(make_credentials(expires_at=NOW + 3600))
    respx.get(DOCS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "1"}, {"id": "2"}],
            headers={"Link": f'<{DOCS_URL}?marker=x>; rel="next"'},
        )
    )

    async with make_client(store, sleeper) as client:
        items = await client.paginate("/documents", accept=media_types.DOCUMENT, max_items=1)

    assert items == [{"id": "1"}]
