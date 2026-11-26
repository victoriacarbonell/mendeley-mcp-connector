from __future__ import annotations

import base64
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from mendeley_mcp_connector.auth import oauth
from mendeley_mcp_connector.auth.token_store import TokenStore
from mendeley_mcp_connector.config import TOKEN_URL
from mendeley_mcp_connector.errors import (
    ApiError,
    AuthenticationExpiredError,
    AuthorizationFlowError,
)
from tests.conftest import FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_REDIRECT_URI

TOKEN_BODY = {
    "access_token": "new-access-token",
    "refresh_token": "new-refresh-token",
    "expires_in": 3600,
    "token_type": "bearer",
}


def test_authorize_url_has_required_parameters() -> None:
    url = oauth.build_authorize_url(FAKE_CLIENT_ID, FAKE_REDIRECT_URI, "xyz")

    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert (
        f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://api.mendeley.com/oauth/authorize"
    )
    assert query == {
        "client_id": [FAKE_CLIENT_ID],
        "redirect_uri": [FAKE_REDIRECT_URI],
        "response_type": ["code"],
        "scope": ["all"],
        "state": ["xyz"],
    }


def test_states_are_unique_and_long() -> None:
    states = {oauth.new_state() for _ in range(20)}
    assert len(states) == 20
    assert all(len(s) >= 40 for s in states)


@pytest.mark.parametrize(
    "uri", ["https://localhost:8585/callback", "http://example.com/callback", "not a url"]
)
def test_redirect_uri_must_be_local_http(uri: str) -> None:
    with pytest.raises(ValueError, match="redirect URI"):
        oauth.validate_redirect_uri(uri)


@respx.mock
async def test_exchange_code_uses_basic_auth_and_form() -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_BODY))

    async with httpx.AsyncClient() as http:
        token = await oauth.exchange_code(
            http,
            client_id=FAKE_CLIENT_ID,
            client_secret=FAKE_CLIENT_SECRET,
            code="one-time-code",
            redirect_uri=FAKE_REDIRECT_URI,
        )

    assert token == oauth.TokenResponse("new-access-token", "new-refresh-token", 3600)
    request = route.calls.last.request
    expected = base64.b64encode(f"{FAKE_CLIENT_ID}:{FAKE_CLIENT_SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    form = parse_qs(request.content.decode())
    assert form == {
        "grant_type": ["authorization_code"],
        "code": ["one-time-code"],
        "redirect_uri": [FAKE_REDIRECT_URI],
    }
    assert FAKE_CLIENT_SECRET not in request.content.decode()


@respx.mock
async def test_refresh_without_new_refresh_token() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a2", "expires_in": "1800"})
    )

    async with httpx.AsyncClient() as http:
        token = await oauth.refresh_access_token(
            http,
            client_id=FAKE_CLIENT_ID,
            client_secret=FAKE_CLIENT_SECRET,
            refresh_token="old-refresh",
            redirect_uri=FAKE_REDIRECT_URI,
        )

    assert token == oauth.TokenResponse("a2", None, 1800)


@respx.mock
@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (400, {"error": "invalid_grant"}, AuthenticationExpiredError),
        (401, {"error": "invalid_client"}, AuthenticationExpiredError),
        (500, {"error": "server_error"}, ApiError),
    ],
)
async def test_token_errors_are_mapped(
    status: int, body: dict[str, str], error: type[Exception]
) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(status, json=body))

    async with httpx.AsyncClient() as http:
        with pytest.raises(error) as excinfo:
            await oauth.exchange_code(
                http,
                client_id=FAKE_CLIENT_ID,
                client_secret=FAKE_CLIENT_SECRET,
                code="c",
                redirect_uri=FAKE_REDIRECT_URI,
            )
    assert FAKE_CLIENT_SECRET not in str(excinfo.value)


def _hit_callback(port: int, query: str) -> httpx.Response:
    return httpx.get(f"http://127.0.0.1:{port}/callback?{query}", timeout=5.0)


def _wait_in_thread(listener: oauth.CallbackListener) -> dict[str, object]:
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["code"] = listener.wait(timeout=5.0)
        except AuthorizationFlowError as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    outcome["thread"] = thread
    return outcome


def test_callback_listener_accepts_matching_state() -> None:
    listener = oauth.CallbackListener(FAKE_REDIRECT_URI, "expected-state", port=0)
    outcome = _wait_in_thread(listener)

    # Browsers also ask for /favicon.ico; other paths must be ignored, not end the login.
    noise = httpx.get(f"http://127.0.0.1:{listener.port}/favicon.ico", timeout=5.0)
    response = _hit_callback(listener.port, "code=abc&state=expected-state")
    outcome["thread"].join(timeout=5.0)  # type: ignore[attr-defined]

    assert noise.status_code == 404
    assert response.status_code == 200
    assert outcome.get("code") == "abc"


def test_callback_listener_rejects_wrong_state() -> None:
    listener = oauth.CallbackListener(FAKE_REDIRECT_URI, "expected-state", port=0)
    outcome = _wait_in_thread(listener)

    response = _hit_callback(listener.port, "code=abc&state=forged")
    outcome["thread"].join(timeout=5.0)  # type: ignore[attr-defined]

    assert response.status_code == 400
    assert "state mismatch" in str(outcome["error"])


def test_callback_listener_reports_denied_access() -> None:
    listener = oauth.CallbackListener(FAKE_REDIRECT_URI, "s", port=0)
    outcome = _wait_in_thread(listener)

    _hit_callback(listener.port, "error=access_denied&state=s")
    outcome["thread"].join(timeout=5.0)  # type: ignore[attr-defined]

    assert "access_denied" in str(outcome["error"])


def test_callback_listener_times_out() -> None:
    listener = oauth.CallbackListener(FAKE_REDIRECT_URI, "s", port=0)
    with pytest.raises(AuthorizationFlowError, match="Timed out"):
        listener.wait(timeout=0.1)


@respx.mock
async def test_full_login_stores_credentials(store: TokenStore) -> None:
    respx.route(host="127.0.0.1").pass_through()
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_BODY))
    port = 47615  # uncommon port, so the test does not clash with a running login
    redirect_uri = f"http://localhost:{port}/callback"

    def fake_browser(url: str) -> None:
        state = parse_qs(urlsplit(url).query)["state"][0]
        # Simulate Mendeley redirecting the browser back to the local listener.
        threading.Thread(target=_hit_callback, args=(port, f"code=granted&state={state}")).start()

    async with httpx.AsyncClient() as http:
        credentials, backend = await oauth.login(
            store,
            client_id=FAKE_CLIENT_ID,
            client_secret=FAKE_CLIENT_SECRET,
            redirect_uri=redirect_uri,
            http=http,
            open_browser=fake_browser,
            announce_url=lambda _url: None,
            wait_seconds=5.0,
            clock=lambda: 1_000.0,
        )

    assert backend == "keyring"
    assert credentials.access_token == "new-access-token"
    assert credentials.expires_at == 4_600.0
    assert store.load() == credentials
