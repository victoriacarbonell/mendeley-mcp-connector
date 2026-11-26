"""Shared fixtures. No test touches the real keyring, the real config dir or the network."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest

from mendeley_mcp_connector.auth.token_store import Credentials, TokenStore

FAKE_CLIENT_ID = "1234"
FAKE_CLIENT_SECRET = "fake-client-secret"
FAKE_REDIRECT_URI = "http://localhost:8585/callback"


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.entries:
            raise keyring.errors.PasswordDeleteError(username)
        del self.entries[(service, username)]


class BrokenKeyring(MemoryKeyring):
    def set_password(self, service: str, username: str, password: str) -> None:
        raise keyring.errors.KeyringError("no backend available")


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[MemoryKeyring]:
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    monkeypatch.setenv("MENDELEY_MCP_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("MENDELEY_CLIENT_ID", "MENDELEY_CLIENT_SECRET", "MENDELEY_MCP_TOKEN_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    return TokenStore(directory=tmp_path / "store")


def make_credentials(
    *,
    access_token: str | None = "fake-access-token",
    refresh_token: str | None = "fake-refresh-token",
    expires_at: float | None = 10_000.0,
) -> Credentials:
    return Credentials(
        client_id=FAKE_CLIENT_ID,
        client_secret=FAKE_CLIENT_SECRET,
        redirect_uri=FAKE_REDIRECT_URI,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )
