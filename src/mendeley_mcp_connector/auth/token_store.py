"""Persistent storage for the user's app credentials and OAuth tokens.

Secrets (client secret, access token, refresh token) go to the operating system
keyring when one is available (Windows Credential Manager, macOS Keychain,
Secret Service on Linux). Each secret is stored as its own keyring entry, which
keeps every value well below the size limit of Windows Credential Manager.

If no keyring is usable, secrets fall back to ``secrets.json`` in the config
directory. That file is created with owner-only permissions (0600) from the
start; on Windows it lives inside the user's profile, which is private to that
user by default.

Non-secret metadata (client ID, redirect URI, token expiry, chosen backend) is
kept in ``config.json`` next to it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import keyring
import keyring.errors

from mendeley_mcp_connector.config import APP_NAME, ENV_TOKEN_BACKEND, config_dir

logger = logging.getLogger(__name__)

Backend = Literal["keyring", "file"]
BackendPreference = Literal["auto", "keyring", "file"]

KEYRING_SERVICE = APP_NAME
SECRET_FIELDS = ("client_secret", "access_token", "refresh_token")


@dataclass
class Credentials:
    """App credentials registered by the user plus the current OAuth tokens."""

    client_id: str
    client_secret: str
    redirect_uri: str
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: float | None = None  # Unix timestamp

    def access_token_is_fresh(self, now: float, margin_seconds: float = 60.0) -> bool:
        """True if the access token exists and will not expire within the margin."""
        if not self.access_token or self.expires_at is None:
            return False
        return now < self.expires_at - margin_seconds

    def __repr__(self) -> str:  # never leak secrets in logs or tracebacks
        return (
            f"Credentials(client_id={self.client_id!r}, redirect_uri={self.redirect_uri!r}, "
            f"has_access_token={self.access_token is not None}, "
            f"has_refresh_token={self.refresh_token is not None}, expires_at={self.expires_at!r})"
        )


class TokenStore:
    """Load, save and clear :class:`Credentials`."""

    def __init__(
        self,
        directory: Path | None = None,
        backend: BackendPreference | None = None,
    ) -> None:
        self.directory = directory or config_dir()
        preference = backend or os.environ.get(ENV_TOKEN_BACKEND, "auto")
        if preference not in ("auto", "keyring", "file"):
            raise ValueError(
                f"{ENV_TOKEN_BACKEND} must be 'auto', 'keyring' or 'file', got {preference!r}"
            )
        self.preference: BackendPreference = preference  # type: ignore[assignment]

    @property
    def metadata_path(self) -> Path:
        return self.directory / "config.json"

    @property
    def secrets_path(self) -> Path:
        return self.directory / "secrets.json"

    # ------------------------------------------------------------------ public API

    def load(self) -> Credentials | None:
        """Return stored credentials, or ``None`` if the user has not logged in."""
        metadata = self._read_json(self.metadata_path)
        if not metadata or not metadata.get("client_id"):
            return None
        backend: Backend = "file" if metadata.get("backend") == "file" else "keyring"
        if backend == "keyring":
            secrets = {name: self._keyring_get(name) for name in SECRET_FIELDS}
        else:
            stored = self._read_json(self.secrets_path) or {}
            secrets = {name: stored.get(name) for name in SECRET_FIELDS}
        if not secrets["client_secret"]:
            logger.warning("Stored credentials are incomplete (no client secret); ignoring them.")
            return None
        expires_at = metadata.get("expires_at")
        return Credentials(
            client_id=str(metadata["client_id"]),
            client_secret=str(secrets["client_secret"]),
            redirect_uri=str(metadata.get("redirect_uri", "")),
            access_token=secrets["access_token"],
            refresh_token=secrets["refresh_token"],
            expires_at=float(expires_at) if expires_at is not None else None,
        )

    def save(self, credentials: Credentials) -> Backend:
        """Persist credentials and return the backend that holds the secrets."""
        secrets: dict[str, str | None] = {
            "client_secret": credentials.client_secret,
            "access_token": credentials.access_token,
            "refresh_token": credentials.refresh_token,
        }
        backend: Backend
        if self.preference == "file":
            backend = "file"
        else:
            try:
                for name, value in secrets.items():
                    self._keyring_set(name, value)
                backend = "keyring"
            except Exception as exc:
                if self.preference == "keyring":
                    raise
                logger.warning(
                    "System keyring unavailable (%s); falling back to a private file.",
                    type(exc).__name__,
                )
                # Remove any partial write so secrets live in exactly one place.
                for name in SECRET_FIELDS:
                    with contextlib.suppress(Exception):
                        self._keyring_set(name, None)
                backend = "file"

        if backend == "file":
            _write_private_json(self.secrets_path, secrets)
        else:
            # Do not leave an old plaintext copy behind after moving to the keyring.
            self.secrets_path.unlink(missing_ok=True)

        _write_private_json(
            self.metadata_path,
            {
                "client_id": credentials.client_id,
                "redirect_uri": credentials.redirect_uri,
                "expires_at": credentials.expires_at,
                "backend": backend,
            },
        )
        return backend

    def clear(self) -> None:
        """Remove every stored secret and the metadata file."""
        for name in SECRET_FIELDS:
            with contextlib.suppress(Exception):
                self._keyring_set(name, None)
        self.secrets_path.unlink(missing_ok=True)
        self.metadata_path.unlink(missing_ok=True)

    def describe_backend(self) -> str:
        """Human-readable description of where secrets currently live."""
        metadata = self._read_json(self.metadata_path) or {}
        if metadata.get("backend") == "file":
            return f"private file {self.secrets_path}"
        return f"system keyring ({type(keyring.get_keyring()).__name__})"

    # ----------------------------------------------------------------- internals

    @staticmethod
    def _keyring_get(name: str) -> str | None:
        try:
            return keyring.get_password(KEYRING_SERVICE, name)
        except keyring.errors.KeyringError:
            logger.warning("Could not read %s from the system keyring.", name)
            return None

    @staticmethod
    def _keyring_set(name: str, value: str | None) -> None:
        if value is None:
            with contextlib.suppress(keyring.errors.PasswordDeleteError):
                keyring.delete_password(KEYRING_SERVICE, name)
        else:
            keyring.set_password(KEYRING_SERVICE, name, value)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning("Could not read %s; ignoring it.", path)
            return None
        return data if isinstance(data, dict) else None


def _write_private_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON readable only by the current user.

    ``tempfile.mkstemp`` creates the file with mode 0600 on POSIX, so there is no
    window in which the secrets are readable by other users.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
