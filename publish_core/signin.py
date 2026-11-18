"""Browser sign-in against /api/auth/tool/start and /exchange (backend/routes/tool_auth_routes.py).

PKCE S256 + state + nonce; a one-shot listener on 127.0.0.1 receives /tool-signin/<nonce>?code=...&state=... after the person
approves in the browser. The code is traded with the verifier for the credential, which goes to the token file and nowhere else.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import platform
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from . import config, tokenstore
from .http import ApiError, request

WAIT_SECONDS = 300           # the server keeps a request for 5 minutes


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_pkce() -> Dict[str, str]:
    verifier = secrets.token_urlsafe(48)                       # 64 chars, RFC 7636
    return {"verifier": verifier, "challenge": _b64(hashlib.sha256(verifier.encode("ascii")).digest()),
            "state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24)}


class _Listener(HTTPServer):
    result: Optional[Dict[str, str]] = None


def _handler(nonce: str, state: str):
    class H(BaseHTTPRequestHandler):
        timeout = 5          # an idle or half-open connection is dropped after this (the loop shortens it near the deadline)
        def log_message(self, *a):          # the query carries the one-time code: never logged
            pass

        def do_GET(self):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path != f"/tool-signin/{nonce}" or not hmac.compare_digest(q.get("state", ""), state):
                self.send_response(404)
                self.end_headers()
                return
            self.server.result = q                             # type: ignore[attr-defined]
            ok = "code" in q
            body = (f"<html><body><p>{'Signed in. You can close this tab and go back to ComfyUI.' if ok else 'Sign-in was not approved.'}"
                    "</p></body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
    return H


def sign_in(server: Optional[str] = None, *, open_browser: Callable[[str], object] = webbrowser.open,
            say: Callable[[str], None] = print, wait_seconds: float = WAIT_SECONDS) -> None:
    """Run the whole flow and save the credential. Raises ApiError / ConfigError / TimeoutError."""
    server = server or config.server_url()
    config.check_url(server)
    p = make_pkce()
    handler = _handler(p["nonce"], p["state"])
    listener = _Listener(("127.0.0.1", 0), handler)
    listener.timeout = 0.5
    port = listener.server_address[1]
    try:
        started = request("POST", f"{server}/api/auth/tool/start", body={
            "challenge": p["challenge"], "state": p["state"], "port": port, "nonce": p["nonce"], "tool": config.TOOL_ID,
            "deviceName": platform.node()[:80]})
        rid = started["requestId"]
        url = f"{server}/tool-signin#r={rid}"
        if not open_browser(url):
            say(f"Open this address in your browser to approve the sign-in: {url}")
        deadline = time.monotonic() + wait_seconds
        while listener.result is None and time.monotonic() < deadline:
            handler.timeout = max(0.1, min(5.0, deadline - time.monotonic()))
            listener.handle_request()
    finally:
        listener.server_close()
    res = listener.result
    if res is None:
        raise TimeoutError("The sign-in was not approved in time")
    if "code" not in res:
        raise ApiError(403, "The sign-in was denied")
    out = request("POST", f"{server}/api/auth/tool/exchange", body={"requestId": rid, "code": res["code"], "verifier": p["verifier"]})
    cred = out.get("credential") or {}
    tokenstore.save(server, out["token"], cred.get("expiresAt"), cred.get("projectId"))
