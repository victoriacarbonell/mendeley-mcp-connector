"""OAuth 2.0 authorization code flow against the Mendeley API.

Flow implemented by :func:`login`:

1. Bind a one-shot HTTP listener on 127.0.0.1 at the port of the redirect URI.
2. Open the Mendeley authorization page in the browser with a random ``state``.
3. Receive the redirect, check that ``state`` matches (CSRF protection) and take
   the authorization code.
4. Exchange the code for tokens, sending the client credentials with HTTP Basic
   authentication, and store everything with :class:`TokenStore`.

Mendeley does not document PKCE support, so the flow relies on the client secret
that each user generates for their own app, plus the ``state`` check.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from mendeley_mcp_connector.auth.token_store import Backend, Credentials, TokenStore
from mendeley_mcp_connector.config import AUTHORIZE_URL, LOGIN_HINT, OAUTH_SCOPE, TOKEN_URL
from mendeley_mcp_connector.errors import (
    ApiError,
    AuthenticationExpiredError,
    AuthorizationFlowError,
)

DEFAULT_LOGIN_TIMEOUT_SECONDS = 300.0
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600  # documented lifetime of an access token

_SUCCESS_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Mendeley MCP Connector</title>"
    "</head><body style='font-family:sans-serif;margin:3em'><h1>{title}</h1><p>{body}</p>"
    "</body></html>"
)


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int


def new_state() -> str:
    """Return an unguessable value for the OAuth ``state`` parameter."""
    return secrets.token_urlsafe(32)


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": OAUTH_SCOPE,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def validate_redirect_uri(redirect_uri: str) -> tuple[int, str]:
    """Check that the redirect URI points to this machine and return (port, path)."""
    parts = urlsplit(redirect_uri)
    if parts.scheme != "http" or parts.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError(
            "The redirect URI must look like http://localhost:8585/callback so the login "
            f"can be received on this computer; got {redirect_uri!r}."
        )
    return parts.port or 80, parts.path or "/"


# --------------------------------------------------------------------- token calls


async def exchange_code(
    http: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> TokenResponse:
    """Exchange a one-time authorization code for an access and refresh token."""
    return await _token_request(
        http,
        client_id,
        client_secret,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
    )


async def refresh_access_token(
    http: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    redirect_uri: str,
) -> TokenResponse:
    """Get a new access token. Mendeley may also rotate the refresh token."""
    return await _token_request(
        http,
        client_id,
        client_secret,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "redirect_uri": redirect_uri,
        },
    )


async def _token_request(
    http: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
    data: dict[str, str],
) -> TokenResponse:
    try:
        response = await http.post(
            TOKEN_URL,
            data=data,
            auth=(client_id, client_secret),
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        raise ApiError(None, f"Could not reach Mendeley to obtain a token: {exc}") from exc

    body: Any
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    if response.status_code == 200 and isinstance(body.get("access_token"), str):
        refresh = body.get("refresh_token")
        return TokenResponse(
            access_token=body["access_token"],
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            expires_in=_parse_expires_in(body.get("expires_in")),
        )

    error = str(body.get("error") or "")
    description = str(body.get("error_description") or body.get("message") or "")
    if error == "invalid_grant":
        raise AuthenticationExpiredError(
            "Mendeley no longer accepts the stored authorization (it may have been revoked "
            f"or already used). {LOGIN_HINT}"
        )
    if error == "invalid_client" or response.status_code == 401:
        raise AuthenticationExpiredError(
            "Mendeley rejected the client ID or client secret. Check the values of your app "
            f"at https://dev.mendeley.com/myapps.html. {LOGIN_HINT}"
        )
    detail = description or error or response.text[:200]
    raise ApiError(response.status_code, f"Mendeley token endpoint error: {detail}")


def _parse_expires_in(value: object) -> int:
    try:
        seconds = int(str(value))
    except ValueError:
        return DEFAULT_TOKEN_LIFETIME_SECONDS
    return seconds if seconds > 0 else DEFAULT_TOKEN_LIFETIME_SECONDS


# ------------------------------------------------------------- local callback server


class _CallbackServer(HTTPServer):
    def __init__(self, port: int, expected_path: str, expected_state: str) -> None:
        super().__init__(("127.0.0.1", port), _CallbackHandler)
        self.expected_path = expected_path
        self.expected_state = expected_state
        self.code: str | None = None
        self.error: str | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path != self.server.expected_path:
            self.send_error(404)
            return
        params = parse_qs(parts.query)
        state = params.get("state", [""])[0]
        if not hmac.compare_digest(state, self.server.expected_state):
            self.server.error = (
                "The login response did not match this login attempt (state mismatch). "
                "Start the login again."
            )
        elif "error" in params:
            self.server.error = f"Mendeley did not authorize the app: {params['error'][0]}."
        elif params.get("code", [""])[0]:
            self.server.code = params["code"][0]
        else:
            self.server.error = "The login response did not contain an authorization code."

        ok = self.server.code is not None
        page = _SUCCESS_PAGE.format(
            title="Connected to Mendeley" if ok else "Login failed",
            body="You can close this tab and return to the terminal."
            if ok
            else "Return to the terminal for details.",
        ).encode("utf-8")
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default access log (it would print the authorization code)."""


class CallbackListener:
    """Receives exactly one OAuth redirect on 127.0.0.1."""

    def __init__(self, redirect_uri: str, expected_state: str, *, port: int | None = None) -> None:
        uri_port, path = validate_redirect_uri(redirect_uri)
        try:
            self._server = _CallbackServer(
                port if port is not None else uri_port, path, expected_state
            )
        except OSError as exc:
            raise AuthorizationFlowError(
                f"Could not listen on port {uri_port} for the login response ({exc}). "
                "Close the program using that port, or register a different redirect URL "
                "and pass it with --redirect-uri."
            ) from exc
        self._server.timeout = 0.5

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def wait(self, timeout: float = DEFAULT_LOGIN_TIMEOUT_SECONDS) -> str:
        """Block until the redirect arrives and return the authorization code."""
        deadline = time.monotonic() + timeout
        try:
            while self._server.code is None and self._server.error is None:
                if time.monotonic() > deadline:
                    raise AuthorizationFlowError(
                        "Timed out waiting for the Mendeley login to finish in the browser."
                    )
                self._server.handle_request()
        finally:
            self._server.server_close()
        if self._server.error:
            raise AuthorizationFlowError(self._server.error)
        assert self._server.code is not None
        return self._server.code


# --------------------------------------------------------------------- full login


async def login(
    store: TokenStore,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    http: httpx.AsyncClient,
    open_browser: Callable[[str], object] = webbrowser.open,
    announce_url: Callable[[str], object] = print,
    wait_seconds: float = DEFAULT_LOGIN_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.time,
) -> tuple[Credentials, Backend]:
    """Run the complete browser login and persist the resulting credentials."""
    state = new_state()
    listener = CallbackListener(redirect_uri, state)  # bind before opening the browser
    url = build_authorize_url(client_id, redirect_uri, state)
    announce_url(url)
    open_browser(url)
    code = await asyncio.to_thread(listener.wait, wait_seconds)
    token = await exchange_code(
        http,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
    )
    credentials = Credentials(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        expires_at=clock() + token.expires_in,
    )
    backend = store.save(credentials)
    return credentials, backend
