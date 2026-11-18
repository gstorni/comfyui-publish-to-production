"""A small JSON/PUT client on urllib. No redirect is followed (a redirect would carry the bearer token elsewhere); every URL is
checked against the same https / private-host rule before anything is sent."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from . import config


class ApiError(Exception):
    def __init__(self, status: int, detail: str, code: Optional[str] = None):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail, self.code = status, detail, code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


# No proxy: the token and the file go straight to the configured host, never through HTTP(S)_PROXY from the environment.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _detail(raw: bytes) -> tuple:
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return "request refused", None
    d = body.get("detail") if isinstance(body, dict) else None
    if isinstance(d, dict):
        return str(d.get("detail") or "request refused"), d.get("code")
    return (str(d) if d else "request refused"), (body.get("code") if isinstance(body, dict) else None)


def request(method: str, url: str, *, body: Any = None, raw: Optional[bytes] = None, token: Optional[str] = None,
            headers: Optional[Dict[str, str]] = None, timeout: float = 60) -> Any:
    config.check_url(url)
    h = dict(headers or {})
    h.setdefault("User-Agent", config.USER_AGENT)
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with _opener.open(req, timeout=timeout) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        detail, code = _detail(e.read())
        raise ApiError(e.code, detail, code) from None
    except urllib.error.URLError as e:
        raise ApiError(0, f"cannot reach the server ({type(e.reason).__name__})") from None
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except ValueError:
        return {}
