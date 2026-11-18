"""The credential file: user-only (0600) in the user's config directory, bound to the server it was issued by.
It is never part of the graph, a node input, a recipe or a log line."""
from __future__ import annotations

import json
import os
from typing import Optional

from . import config

FILE = "token.json"


def path():
    return config.config_dir() / FILE


def save(server: str, token: str, expires_at: Optional[str] = None, project_id: Optional[str] = None) -> None:
    d = config.config_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    tmp = d / (FILE + ".tmp")
    try:
        tmp.unlink()                                                     # a stale file (maybe with other modes) is never reused
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)      # created 0600: never readable by others, even briefly
    with os.fdopen(fd, "w") as f:
        json.dump({"server": server, "token": token, "expiresAt": expires_at, "projectId": project_id}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path())


def load(server: str) -> Optional[str]:
    """The token for THIS server, or None (a token issued by another server is never offered to this one)."""
    try:
        data = json.loads(path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("server") != server or not isinstance(data.get("token"), str):
        return None
    return data["token"]


def clear() -> None:
    try:
        path().unlink()
    except OSError:
        pass
