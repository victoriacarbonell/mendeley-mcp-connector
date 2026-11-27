"""Async HTTP client for the Mendeley API.

Responsibilities:

* attach the bearer token and refresh it shortly before it expires;
* refresh once and retry when the API answers 401 (token revoked or expired early);
* wait and retry on 429 Too Many Requests (honouring ``Retry-After`` when sent) and,
  for read-only requests, on transient 502/503/504 errors;
* follow ``Link: rel="next"`` pagination, only to the Mendeley API host, so the
  bearer token is never sent anywhere else;
* turn error responses into :mod:`mendeley_mcp_connector.errors` exceptions with
  messages the model can act on.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from mendeley_mcp_connector import __version__
from mendeley_mcp_connector.auth.oauth import (
    DEFAULT_TOKEN_LIFETIME_SECONDS,
    refresh_access_token,
)
from mendeley_mcp_connector.auth.token_store import Credentials, TokenStore
from mendeley_mcp_connector.config import API_BASE_URL, APP_NAME, LOGIN_HINT
from mendeley_mcp_connector.errors import (
    ApiError,
    AuthenticationExpiredError,
    NotAuthenticatedError,
    NotFoundError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TRANSIENT_STATUSES = frozenset({502, 503, 504})
MAX_RETRY_DELAY_SECONDS = 60.0

QueryParams = Mapping[str, str | int | float | bool | None]


class MendeleyClient:
    """Authenticated access to ``https://api.mendeley.com``."""

    def __init__(
        self,
        store: TokenStore,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = API_BASE_URL,
        max_retries: int = 4,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": f"{APP_NAME}/{__version__}"},
        )
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._credentials: Credentials | None = None
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> MendeleyClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ requests

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        accept: str | None = None,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send an authenticated request and return the successful response."""
        method = method.upper()
        url = self._resolve_url(path)
        request_headers = dict(headers or {})
        if accept:
            request_headers["Accept"] = accept
        if content_type:
            request_headers["Content-Type"] = content_type
        query = {k: v for k, v in (params or {}).items() if v is not None}

        token = await self._get_access_token()
        refreshed_after_401 = False
        attempt = 0
        while True:
            request_headers["Authorization"] = f"Bearer {token}"
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=query or None,
                    json=json_body,
                    content=content,
                    headers=request_headers,
                )
            except httpx.TimeoutException as exc:
                if method in IDEMPOTENT_METHODS and attempt < self._max_retries:
                    await self._sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise ApiError(None, "Mendeley did not respond in time. Try again later.") from exc
            except httpx.HTTPError as exc:
                raise ApiError(None, f"Could not connect to Mendeley: {exc}") from exc

            status = response.status_code
            if status == 401 and not refreshed_after_401:
                refreshed_after_401 = True
                token = await self._get_access_token(stale_token=token)
                continue
            retryable = status == 429 or (
                status in TRANSIENT_STATUSES and method in IDEMPOTENT_METHODS
            )
            if retryable and attempt < self._max_retries:
                delay = self._retry_delay(response, attempt)
                logger.info("Mendeley answered %s; retrying in %.1f s", status, delay)
                await self._sleep(delay)
                attempt += 1
                continue
            if response.is_success:
                return response
            raise self._error_for(response, attempts=attempt + 1)

    async def get_json(
        self,
        path: str,
        *,
        accept: str,
        params: QueryParams | None = None,
    ) -> Any:
        response = await self.request("GET", path, params=params, accept=accept)
        return response.json()

    async def paginate(
        self,
        path: str,
        *,
        accept: str,
        params: QueryParams | None = None,
        max_items: int | None = None,
    ) -> list[Any]:
        """Collect every item of a paginated list endpoint, following ``rel="next"``."""
        items: list[Any] = []
        next_url: str | None = path
        next_params: QueryParams | None = params
        while next_url:
            response = await self.request("GET", next_url, params=next_params, accept=accept)
            page = response.json()
            if not isinstance(page, list):
                raise ApiError(response.status_code, "Mendeley returned an unexpected list format.")
            items.extend(page)
            if max_items is not None and len(items) >= max_items:
                return items[:max_items]
            next_url = response.links.get("next", {}).get("url")
            next_params = None  # the next link already carries the query string
        return items

    # -------------------------------------------------------------- token handling

    async def _get_access_token(self, *, stale_token: str | None = None) -> str:
        """Return a usable access token, refreshing it if needed.

        ``stale_token`` is the token the API just rejected. If another request has
        already replaced it, the new token is reused instead of refreshing again.
        """
        async with self._token_lock:
            credentials = self._load_credentials()
            if self._needs_refresh(credentials, stale_token):
                # Another process (`auth status`, `doctor`, a second server) may have
                # refreshed already and, if Mendeley rotated the refresh token, made our
                # in-memory copy unusable. Start from what is stored now.
                credentials = self._reload_credentials() or credentials
                if self._needs_refresh(credentials, stale_token):
                    await self._refresh_or_adopt_stored(credentials)
                    credentials = self._load_credentials()
            assert credentials.access_token is not None
            return credentials.access_token

    def _needs_refresh(self, credentials: Credentials, stale_token: str | None) -> bool:
        if stale_token is not None:
            return credentials.access_token == stale_token
        return not credentials.access_token_is_fresh(self._clock())

    def _load_credentials(self) -> Credentials:
        if self._credentials is None:
            credentials = self._store.load()
            if credentials is None:
                raise NotAuthenticatedError(f"Not connected to Mendeley yet. {LOGIN_HINT}")
            self._credentials = credentials
        return self._credentials

    def _reload_credentials(self) -> Credentials | None:
        """Re-read the store, keeping the in-memory copy if that fails."""
        try:
            stored = self._store.load()
        except Exception as exc:  # a locked or broken keyring must not stop the request
            logger.warning("Could not re-read the stored Mendeley credentials: %s", exc)
            return None
        if stored is not None:
            self._credentials = stored
        return stored

    async def _refresh_or_adopt_stored(self, credentials: Credentials) -> None:
        try:
            await self._refresh(credentials)
        except AuthenticationExpiredError:
            # The refresh token may have been rotated by another process between our
            # reload and the refresh. Retry once if the store now holds a different one.
            latest = self._reload_credentials()
            if latest is None or latest.refresh_token == credentials.refresh_token:
                raise
            if not latest.access_token_is_fresh(self._clock()):
                await self._refresh(latest)

    async def _refresh(self, credentials: Credentials) -> None:
        if not credentials.refresh_token:
            raise AuthenticationExpiredError(f"The Mendeley session has expired. {LOGIN_HINT}")
        token = await refresh_access_token(
            self._http,
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            refresh_token=credentials.refresh_token,
            redirect_uri=credentials.redirect_uri,
        )
        credentials.access_token = token.access_token
        if token.refresh_token:  # Mendeley may rotate it; always keep the newest one
            credentials.refresh_token = token.refresh_token
        credentials.expires_at = self._clock() + (
            token.expires_in or DEFAULT_TOKEN_LIFETIME_SECONDS
        )
        self._credentials = credentials
        try:
            self._store.save(credentials)
        except Exception as exc:  # keep working with the new token held in memory
            logger.warning(
                "Refreshed the Mendeley token but could not store it (%s). This session "
                "keeps working; after a restart you may need to run `auth login` again.",
                exc,
            )
            return
        logger.info("Refreshed the Mendeley access token.")

    # ----------------------------------------------------------------- helpers

    def _resolve_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            if not path.startswith(self._base_url + "/"):
                raise ApiError(None, "Refusing to send credentials to a non-Mendeley URL.")
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    @staticmethod
    def _backoff(attempt: int) -> float:
        return float(min(2**attempt, 30))

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header:
            delay: float | None = None
            if header.strip().isdigit():
                delay = float(header.strip())
            else:
                try:
                    when = email.utils.parsedate_to_datetime(header)
                    delay = when.timestamp() - self._clock()
                except (TypeError, ValueError):
                    delay = None
            if delay is not None:
                return max(0.0, min(delay, MAX_RETRY_DELAY_SECONDS))
        return self._backoff(attempt)

    @staticmethod
    def _error_for(response: httpx.Response, *, attempts: int) -> Exception:
        status = response.status_code
        detail = _extract_error_detail(response)
        if status == 401:
            return AuthenticationExpiredError(
                f"Mendeley rejected the access token even after refreshing it. {LOGIN_HINT}"
            )
        if status == 403:
            return ApiError(
                status,
                "Mendeley denied access to this resource (403). It may belong to another "
                f"user or group.{_suffix(detail)}",
            )
        if status == 404:
            return NotFoundError(
                "Mendeley could not find that item (404). Check that the ID is correct and "
                f"belongs to your library.{_suffix(detail)}"
            )
        if status == 429:
            return RateLimitError(
                f"Mendeley's rate limit is still active after {attempts} attempts. "
                "Wait a minute or two before trying again."
            )
        if status >= 500:
            return ApiError(
                status,
                f"Mendeley had a server error ({status}). Try again later.{_suffix(detail)}",
            )
        return ApiError(status, f"Mendeley rejected the request ({status}).{_suffix(detail)}")


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:300]
    if isinstance(body, dict):
        for key in ("message", "error_message", "error_description", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:300]
    return ""


def _suffix(detail: str) -> str:
    return f" Details: {detail}" if detail else ""
