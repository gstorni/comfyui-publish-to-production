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


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#f4f2ee;--card:#fff;--ink:#1c1f24;--sub:#5c636e;--line:#e3e0d9;--accent:{accent};--tint:{tint}}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1015;--card:#151a21;--ink:#ece7db;--sub:#9aa3ae;--line:#252c36}}}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px 16px;background:var(--bg);
color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{width:100%;max-width:420px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:36px 32px 28px;text-align:center}}
.icon{{width:56px;height:56px;margin:0 auto 20px;border-radius:50%;background:var(--tint);display:flex;align-items:center;justify-content:center}}
h1{{margin:0 0 8px;font-size:22px;font-weight:600;letter-spacing:-.01em}}
p{{margin:0;color:var(--sub)}}
.foot{{margin-top:24px;padding-top:16px;border-top:1px solid var(--line);font-size:13px;color:var(--sub)}}
</style></head>
<body><main>
<div class="icon" aria-hidden="true">{icon}</div>
<h1>{title}</h1>
<p>{text}</p>
<div class="foot">{label} &middot; ComfyUI</div>
</main></body></html>
"""
_OK_ICON = ('<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" '
            'stroke-linecap="round" stroke-linejoin="round" style="color:var(--accent)"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>')
_NO_ICON = ('<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" '
            'stroke-linecap="round" style="color:var(--accent)"><path d="M7 7l10 10M17 7L7 17"/></svg>')


def result_page(ok: bool) -> str:
    """The page the browser lands on after the approval screen. Static text only: nothing from the request is echoed."""
    if ok:
        return _PAGE.format(title="Sign-in approved", accent="#1f9d6b", tint="rgba(52,211,153,.16)", icon=_OK_ICON,
                            text="Go back to ComfyUI: it finishes connecting there. You can close this tab.", label=config.NODE_LABEL)
    return _PAGE.format(title="Sign-in not approved", accent="#d9483b", tint="rgba(244,87,77,.14)", icon=_NO_ICON,
                        text="Nothing was connected. To try again, run the sign-in from ComfyUI.", label=config.NODE_LABEL)


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
            body = result_page(ok).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
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
