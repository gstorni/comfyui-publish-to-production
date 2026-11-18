"""A fake of the tool API on loopback: records every call, answers like backend/routes/tool_routes.py (camelCase)."""
import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "fct_" + "t" * 40
PART = 64 * 1024


class Fake:
    def __init__(self):
        self.calls = []          # (method, path, authorization-present)
        self.versions = {}       # publishKey -> dict
        self.reserved_bodies = []
        self.parts = {}
        self.start = None
        self.fail_complete_once = False
        self.edge_dead = False
        self.parts_finished = False
        self.sign_hosts = []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _read(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _auth(self):
                return self.headers.get("Authorization") == f"Bearer {TOKEN}"

            def do_GET(self):
                fake.calls.append(("GET", self.path, bool(self.headers.get("Authorization"))))
                if self.path == "/api/tools/shots":
                    if not self._auth():
                        return self._send(401, {"detail": "Invalid or expired tool credential"})
                    return self._send(200, {"items": [{"shotCode": "SH010", "stage": "gen"}]})
                self._send(404, {})

            def do_PUT(self):
                fake.calls.append(("PUT", self.path, bool(self.headers.get("Authorization"))))
                fake.parts[self.path] = self._read()
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                raw = self._read()
                body = json.loads(raw or b"{}")
                fake.calls.append(("POST", self.path, bool(self.headers.get("Authorization"))))
                p = self.path
                if p == "/api/auth/tool/start":
                    fake.start = body
                    return self._send(200, {"requestId": "r" * 43, "expiresIn": 300})
                if p == "/api/auth/tool/exchange":
                    ch = base64.urlsafe_b64encode(hashlib.sha256(body["verifier"].encode()).digest()).rstrip(b"=").decode()
                    if body.get("code") != "the-one-time-code-xxxxxx" or ch != fake.start["challenge"]:
                        return self._send(400, {"detail": "Invalid or expired sign-in code"})
                    return self._send(200, {"token": TOKEN, "credential": {"expiresAt": "2027-01-01T00:00:00", "projectId": "p"}})
                if not self._auth():
                    return self._send(401, {"detail": "Invalid or expired tool credential"})
                if p == "/api/tools/publish/reserve":
                    fake.reserved_bodies.append(body)
                    k = body["publishKey"]
                    if k in fake.versions:
                        v = fake.versions[k]
                        if (v["uploader"] != "u1" or v["stage"] != body["stage"] or v["size"] != body["size"]
                                or v["checksum"] != body["checksum"]["value"]):
                            return self._send(409, {"detail": "This publish key was already used for a different request"})
                        return self._send(200, {**v["view"], "repeat": True})
                    n = len(fake.versions) + 1
                    view = {"versionId": f"v{n}", "versionNumber": n, "state": "uploading", "publishKey": k, "repeat": False}
                    fake.versions[k] = {"uploader": "u1", "stage": body["stage"], "size": body["size"], "checksum": body["checksum"]["value"], "view": view}
                    return self._send(200, view)
                if p.endswith("/upload/init"):
                    size = next(v["size"] for v in fake.versions.values() if v["view"]["versionId"] == p.split("/")[4])
                    total = max(1, -(-size // PART))
                    return self._send(200, {"direct": True, "session": "s1", "partSize": PART, "partsTotal": total, "hosts": ["edge", "cf"]})
                if p.endswith("/sign"):
                    fake.sign_hosts.append(body["host"])
                    root = "http://127.0.0.1:9" if (body["host"] == "edge" and fake.edge_dead) else fake.url      # port 9: refused
                    return self._send(200, {"urls": {str(n): f"{root}/put/{n}" for n in body["parts"]}, "expiresIn": 3600})
                if p.endswith("/parts"):
                    if fake.parts_finished:
                        return self._send(200, {"job": {"id": "j0"}, "finished": True})
                    return self._send(200, {"partSize": PART, "done": [{"n": int(k.rsplit('/', 1)[1]), "size": len(v)}
                                                                      for k, v in fake.parts.items()]})
                if p == "/api/tools/publish/complete":
                    if fake.fail_complete_once:
                        fake.fail_complete_once = False
                        return self._send(503, {"detail": "try again"})
                    return self._send(200, {"job": {"id": "j1"}})
                self._send(404, {})
        return H
