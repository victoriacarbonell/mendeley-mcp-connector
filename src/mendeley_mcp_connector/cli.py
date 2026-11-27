"""Command-line interface.

mendeley-mcp-connector serve          # run the MCP server over stdio
mendeley-mcp-connector auth login     # connect your Mendeley account
mendeley-mcp-connector auth status    # show whether you are connected
mendeley-mcp-connector auth logout    # forget stored credentials
mendeley-mcp-connector doctor         # read-only API compatibility checks
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import time
from collections.abc import Sequence

import httpx

from mendeley_mcp_connector import __version__
from mendeley_mcp_connector.api import media_types
from mendeley_mcp_connector.api.client import MendeleyClient
from mendeley_mcp_connector.auth import oauth
from mendeley_mcp_connector.auth.token_store import TokenStore
from mendeley_mcp_connector.config import (
    APP_NAME,
    DEFAULT_REDIRECT_URI,
    ENV_CLIENT_ID,
    ENV_CLIENT_SECRET,
    ENV_REDIRECT_URI,
)
from mendeley_mcp_connector.errors import MendeleyError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Unofficial MCP server for your own Mendeley library.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug information")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("serve", help="run the MCP server over stdio (used by Claude Desktop)")
    commands.add_parser("doctor", help="check which Mendeley API features work (read-only)")

    auth = commands.add_parser("auth", help="manage the connection to your Mendeley account")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_commands.add_parser("login", help="connect your Mendeley account")
    login.add_argument(
        "--client-id", help=f"application ID from dev.mendeley.com (or set {ENV_CLIENT_ID})"
    )
    login.add_argument(
        "--redirect-uri",
        help=f"redirect URL registered for your app (default {DEFAULT_REDIRECT_URI})",
    )
    login.add_argument(
        "--no-browser", action="store_true", help="print the login URL instead of opening it"
    )
    auth_commands.add_parser("status", help="show whether you are connected")
    auth_commands.add_parser("logout", help="delete stored credentials from this computer")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # stderr only: in `serve` mode stdout is reserved for the MCP protocol.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.command == "serve":
        from mendeley_mcp_connector.transports.stdio import run_stdio

        run_stdio()
        return 0
    try:
        if args.command == "doctor":
            return asyncio.run(_doctor())
        if args.auth_command == "login":
            return _login(args)
        if args.auth_command == "status":
            return asyncio.run(_status())
        if args.auth_command == "logout":
            return _logout()
    except (MendeleyError, ValueError, OSError) as exc:
        # ValueError: invalid settings (redirect URL, token backend); OSError: local files.
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    raise AssertionError("unreachable")  # pragma: no cover


def _login(args: argparse.Namespace) -> int:
    """Ask for the app credentials (blocking prompts), then run the async login."""
    store = TokenStore()
    previous = store.load()
    client_id = (
        args.client_id
        or os.environ.get(ENV_CLIENT_ID)
        or input("Mendeley application ID (client ID): ").strip()
    )
    client_secret = os.environ.get(ENV_CLIENT_SECRET) or getpass.getpass(
        "Mendeley application secret (input hidden): "
    )
    redirect_uri = (
        args.redirect_uri
        or os.environ.get(ENV_REDIRECT_URI)
        or (previous.redirect_uri if previous else None)
        or DEFAULT_REDIRECT_URI
    )
    if not client_id or not client_secret:
        print("Error: both the application ID and the secret are required.", file=sys.stderr)
        return 2
    return asyncio.run(
        _login_async(
            store,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            no_browser=args.no_browser,
        )
    )


async def _login_async(
    store: TokenStore,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    no_browser: bool,
) -> int:
    def announce(url: str) -> None:
        print("Opening the Mendeley login page in your browser.")
        print("If it does not open, visit this URL:\n")
        print(f"  {url}\n")
        print(f"Waiting for the login to finish (redirect URL: {redirect_uri}) ...")

    def open_browser(url: str) -> object:
        if no_browser:
            return None
        import webbrowser

        return webbrowser.open(url)

    async with httpx.AsyncClient(timeout=30.0) as http:
        _, backend = await oauth.login(
            store,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            http=http,
            open_browser=open_browser,
            announce_url=announce,
        )
    print(f"\nConnected. Credentials stored in the {store.describe_backend()}.")
    if backend == "file":
        print("Note: no system keyring was available, so a private file is used instead.")
    return await _status(show_header=False)


async def _status(*, show_header: bool = True) -> int:
    store = TokenStore()
    credentials = store.load()
    if credentials is None:
        print("Not connected. Run `mendeley-mcp-connector auth login`.")
        return 1
    if show_header:
        print(f"Application ID: {_mask(credentials.client_id)}")
        print(f"Redirect URL:   {credentials.redirect_uri}")
        print(f"Stored in:      {store.describe_backend()}")
        if credentials.expires_at:
            minutes = int((credentials.expires_at - time.time()) / 60)
            state = f"valid for ~{minutes} min" if minutes > 0 else "expired (refreshed on use)"
            print(f"Access token:   {state}")
    async with MendeleyClient(store) as client:
        profile = await client.get_json("/profiles/me", accept=media_types.PROFILE)
    name = profile.get("display_name") if isinstance(profile, dict) else None
    print(f"Connected to Mendeley as: {name or 'unknown user'}")
    return 0


def _logout() -> int:
    TokenStore().clear()
    print("Removed the stored Mendeley credentials from this computer.")
    print(
        "To revoke access completely, regenerate your app secret at "
        "https://dev.mendeley.com/myapps.html."
    )
    return 0


async def _doctor() -> int:
    from mendeley_mcp_connector.doctor import run_checks

    async with MendeleyClient(TokenStore()) as client:
        results = await run_checks(client)
    width = max(len(r.name) for r in results)
    for result in results:
        print(f"[{result.status.upper():4}] {result.name.ljust(width)}  {result.detail}")
    return 1 if any(r.status == "fail" for r in results) else 0


def _mask(value: str) -> str:
    return value if len(value) <= 4 else f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"
