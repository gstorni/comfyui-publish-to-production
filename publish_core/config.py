"""Settings: the product name (one constant), the server URL and where files live.

The server comes from the environment (`PRODUCTION_SERVER`, e.g. https://flow.example.com) or `config.json` in the config
directory. The token is only ever sent to that server: https always, plain http only for a loopback host or a private-network
IP literal (a studio server on the LAN), never a public name.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

PRODUCT_NAME = "Production"          # the label users see ("Publish to production"); change it here only
NODE_LABEL = f"Publish to {PRODUCT_NAME.lower()}"
NODE_ID = "ComfyPublish_PublishToProduction"
TOOL_ID = "comfy"                    # the tool name the server knows (tool_credentials.ALLOWED_TOOLS)
ENV_SERVER = "PRODUCTION_SERVER"
ENV_CONFIG_DIR = "COMFY_PUBLISH_HOME"


class ConfigError(Exception):
    pass


def config_dir() -> Path:
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "comfy-publish"


def _private_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def check_url(url: str) -> str:
    """The URL if the token (or the file) may travel to it, else ConfigError. Returns it without a trailing slash."""
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ConfigError("The server address must be an https:// URL")
    if parts.scheme == "http" and not _private_host(parts.hostname):
        raise ConfigError("Plain http is only allowed for a loopback or private-network address; use https://")
    return url.rstrip("/")


def server_url() -> str:
    url = os.environ.get(ENV_SERVER)
    if not url:
        try:
            url = json.loads((config_dir() / "config.json").read_text()).get("server")
        except (OSError, ValueError, AttributeError):
            url = None
    if not url:
        raise ConfigError(f"No server configured: set {ENV_SERVER} or write {{\"server\": \"https://...\"}} to "
                          f"{config_dir() / 'config.json'}")
    return check_url(url)


def optional_server_url() -> Optional[str]:
    try:
        return server_url()
    except ConfigError:
        return None
