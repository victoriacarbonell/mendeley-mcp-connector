from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import keyring
import pytest

from mendeley_mcp_connector.auth.token_store import KEYRING_SERVICE, TokenStore
from tests.conftest import FAKE_CLIENT_SECRET, BrokenKeyring, MemoryKeyring, make_credentials


def test_load_returns_none_when_nothing_stored(store: TokenStore) -> None:
    assert store.load() is None


def test_round_trip_through_keyring(store: TokenStore, memory_keyring: MemoryKeyring) -> None:
    backend = store.save(make_credentials())

    assert backend == "keyring"
    assert memory_keyring.entries[(KEYRING_SERVICE, "client_secret")] == FAKE_CLIENT_SECRET
    assert not store.secrets_path.exists()
    loaded = store.load()
    assert loaded == make_credentials()


def test_metadata_file_contains_no_secrets(store: TokenStore) -> None:
    store.save(make_credentials())

    text = store.metadata_path.read_text(encoding="utf-8")
    assert "fake-access-token" not in text
    assert "fake-refresh-token" not in text
    assert FAKE_CLIENT_SECRET not in text
    assert json.loads(text)["backend"] == "keyring"


def test_falls_back_to_private_file_when_keyring_fails(store: TokenStore) -> None:
    keyring.set_keyring(BrokenKeyring())

    backend = store.save(make_credentials())

    assert backend == "file"
    assert json.loads(store.secrets_path.read_text(encoding="utf-8"))["refresh_token"] == (
        "fake-refresh-token"
    )
    assert store.load() == make_credentials()


def test_forced_keyring_backend_raises_instead_of_falling_back(tmp_path: Path) -> None:
    keyring.set_keyring(BrokenKeyring())
    forced = TokenStore(directory=tmp_path / "forced", backend="keyring")

    with pytest.raises(keyring.errors.KeyringError):
        forced.save(make_credentials())


def test_file_backend_can_be_forced_with_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    monkeypatch.setenv("MENDELEY_MCP_TOKEN_BACKEND", "file")
    forced = TokenStore(directory=tmp_path / "env")

    assert forced.save(make_credentials()) == "file"
    assert memory_keyring.entries == {}


def test_invalid_backend_preference_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="MENDELEY_MCP_TOKEN_BACKEND"):
        TokenStore(directory=tmp_path, backend="cloud")  # type: ignore[arg-type]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_files_are_owner_only(tmp_path: Path) -> None:
    file_store = TokenStore(directory=tmp_path / "perm", backend="file")
    file_store.save(make_credentials())

    for path in (file_store.secrets_path, file_store.metadata_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(file_store.directory.stat().st_mode) == 0o700


def test_moving_to_keyring_removes_plaintext_copy(tmp_path: Path) -> None:
    directory = tmp_path / "move"
    TokenStore(directory=directory, backend="file").save(make_credentials())
    assert (directory / "secrets.json").exists()

    TokenStore(directory=directory).save(make_credentials())

    assert not (directory / "secrets.json").exists()


def test_clear_removes_everything(store: TokenStore, memory_keyring: MemoryKeyring) -> None:
    store.save(make_credentials())

    store.clear()

    assert store.load() is None
    assert memory_keyring.entries == {}
    assert not store.metadata_path.exists()


def test_repr_does_not_leak_secrets() -> None:
    text = repr(make_credentials())
    assert FAKE_CLIENT_SECRET not in text
    assert "fake-access-token" not in text


def test_access_token_freshness() -> None:
    credentials = make_credentials(expires_at=1_000.0)
    assert credentials.access_token_is_fresh(now=900.0)
    assert not credentials.access_token_is_fresh(now=950.0)  # inside the 60 s margin
    assert not make_credentials(access_token=None).access_token_is_fresh(now=0.0)
