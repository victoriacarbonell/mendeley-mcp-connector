"""Static configuration, environment variables and filesystem locations."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "mendeley-mcp-connector"

API_BASE_URL = "https://api.mendeley.com"
AUTHORIZE_URL = f"{API_BASE_URL}/oauth/authorize"
TOKEN_URL = f"{API_BASE_URL}/oauth/token"
# "all" is the only scope documented for the authorization code flow.
OAUTH_SCOPE = "all"

# Must match, character for character, the redirect URL registered for the user's
# app at https://dev.mendeley.com/myapps.html.
DEFAULT_REDIRECT_URI = "http://localhost:8585/callback"

ENV_CLIENT_ID = "MENDELEY_CLIENT_ID"
ENV_CLIENT_SECRET = "MENDELEY_CLIENT_SECRET"  # noqa: S105 - variable name, not a secret
ENV_REDIRECT_URI = "MENDELEY_REDIRECT_URI"
ENV_CONFIG_DIR = "MENDELEY_MCP_CONFIG_DIR"
ENV_TOKEN_BACKEND = "MENDELEY_MCP_TOKEN_BACKEND"  # noqa: S105 - variable name, not a secret

LOGIN_HINT = (
    "Run `mendeley-mcp-connector auth login` in a terminal to connect your Mendeley account."
)


def config_dir() -> Path:
    """Return the per-user directory used for configuration and the fallback token file."""
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / APP_NAME
