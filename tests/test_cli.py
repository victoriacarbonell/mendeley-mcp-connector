from __future__ import annotations

import httpx
import pytest
import respx

from mendeley_mcp_connector import cli
from mendeley_mcp_connector.auth.token_store import TokenStore
from mendeley_mcp_connector.config import API_BASE_URL
from tests.conftest import FAKE_CLIENT_SECRET, make_credentials


def test_status_when_not_logged_in(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["auth", "status"]) == 1
    assert "Not connected" in capsys.readouterr().out


@respx.mock
def test_status_when_logged_in(capsys: pytest.CaptureFixture[str]) -> None:
    TokenStore().save(make_credentials(expires_at=9_999_999_999.0))
    respx.get(f"{API_BASE_URL}/profiles/me").mock(
        return_value=httpx.Response(200, json={"id": "p1", "display_name": "Test User"})
    )

    assert cli.main(["auth", "status"]) == 0
    out = capsys.readouterr().out
    assert "Connected to Mendeley as: Test User" in out
    assert FAKE_CLIENT_SECRET not in out
    assert "fake-access-token" not in out


def test_logout_clears_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    TokenStore().save(make_credentials())

    assert cli.main(["auth", "logout"]) == 0
    assert TokenStore().load() is None


def test_login_requires_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "")

    assert cli.main(["auth", "login", "--no-browser"]) == 2
    assert "required" in capsys.readouterr().err


def test_doctor_without_login_reports_failure(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("[FAIL] GET /profiles/me")
    assert "auth login" in out


def test_api_errors_are_printed_without_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    TokenStore().save(make_credentials(expires_at=9_999_999_999.0))
    with respx.mock:
        respx.get(f"{API_BASE_URL}/profiles/me").mock(return_value=httpx.Response(403))
        assert cli.main(["auth", "status"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: Mendeley denied access")
    assert "Traceback" not in err
