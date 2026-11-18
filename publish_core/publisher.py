"""The publish call sequence (backend/routes/tool_routes.py):

    reserve -> upload/init -> upload/{session}/sign -> PUT each part -> upload/{session}/parts -> complete

A retry of the same graph and frames is the same publishKey, so reserve answers the same version. The encoded file is kept per key
under the config directory so a retry sends identical bytes (the server compares size and checksum with the first request).
"""
from __future__ import annotations

import hashlib
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import config, encode, recipe, signin, tokenstore
from .http import ApiError, request

SIGN_BATCH = 64        # the server signs at most 64 part URLs per call


class Refused(Exception):
    """Something the person must fix first (said plainly in the node output)."""


@dataclass
class Result:
    published: bool
    message: str
    warnings: List[str] = field(default_factory=list)
    version_number: Optional[int] = None
    version_id: Optional[str] = None
    repeat: bool = False


def parse_pick(pick: str):
    code, _, stage = (pick or "").rpartition(" | ")
    if not code or not stage:
        raise Refused("Pick a shot from the list (sign in first if the list is empty)")
    return code, stage


STATES: dict = {}      # (shotCode, stage) -> "no_version" | "changes_requested" | "ready"; replaced with NOTES
NOTES: dict = {}       # (shotCode, stage) -> (version number, sanitized note); replaced after each successful list read, dropped after a publish to it


def clean_note(text, limit: int = 300) -> str:
    """The reviewer's note is untrusted: a plain single-line string, control characters gone, no markup carried."""
    s = "".join(" " if (c.isspace() or ord(c) < 32 or 0x7F <= ord(c) < 0xA0) else ("" if unicodedata.category(c) == "Cf" else c)
                for c in str(text or ""))
    s = " ".join(s.replace("<", "").replace(">", "").split())
    return s if len(s) <= limit else s[:limit - 1] + "\u2026"


def note_for(pick: str) -> str:
    """'Changes requested on v004: <note>' for a picked shot that came back, else ''."""
    try:
        code, stage = parse_pick(pick)
    except Refused:
        return ""
    hit = NOTES.get((code, stage))
    if not hit:
        return ""
    ver, note = hit
    return f"Changes requested on v{ver:03d}" + (f": {note}" if note else "")


def list_shots(server: str, token: str, timeout: float = 60) -> List[str]:
    out = request("GET", f"{server}/api/tools/shots", token=token, timeout=timeout)
    labels: List[str] = []
    notes: dict = {}
    states: dict = {}
    for i in out.get("items", []):
        code, stage = i.get("shotCode"), i.get("stage")
        if not code or not stage:
            continue
        latest = i.get("latestVersion") or {}
        if latest.get("reviewState") == "changes_requested" and isinstance(latest.get("number"), int):
            notes[(code, stage)] = (latest["number"], clean_note(i.get("directorNote")))
        states[(code, stage)] = ("no_version" if not latest else
                                 "changes_requested" if (code, stage) in notes else "ready")
        labels.append(f"{code} | {stage}")          # the option value never changes: a saved graph must still validate
    NOTES.clear()
    NOTES.update(notes)                            # swapped only after a good read
    STATES.clear()
    STATES.update(states)
    return labels


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _prune(d: Path, days: int = 7) -> None:
    """Encoded files are kept so a retry sends the same bytes; week-old ones go."""
    if d.is_dir():
        for f in d.glob("*.mp4"):
            if time.time() - f.stat().st_mtime > days * 86400:
                f.unlink(missing_ok=True)


def _put_part(url: str, data: bytes, attempts: int = 3) -> None:
    last = None
    for attempt in range(attempts):
        try:
            request("PUT", url, raw=data, timeout=600)
            return
        except ApiError as e:
            last = e
            if 400 <= e.status < 500 and e.status != 429:
                break
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    raise last  # type: ignore[misc]


def _upload(server: str, token: str, version_id: str, path: Path) -> str:
    base = f"{server}/api/tools/publish/{version_id}/upload"
    init = request("POST", f"{base}/init", token=token)
    if not init.get("direct") or not init.get("session"):
        raise Refused("The server cannot take a direct upload right now; try again later")
    session, part_size, total = init["session"], int(init["partSize"]), int(init["partsTotal"])
    hosts = init.get("hosts") or ["edge"]
    host = "edge" if "edge" in hosts else hosts[0]

    def sign(nums, h):
        return request("POST", f"{base}/{session}/sign", token=token, body={"parts": nums, "host": h})["urls"]

    with open(path, "rb") as f:
        first = 1
        while first <= total:
            nums = list(range(first, min(first + SIGN_BATCH, total + 1)))
            urls = sign(nums, host)
            i = 0
            while i < len(nums):
                n = nums[i]
                f.seek((n - 1) * part_size)
                data = f.read(part_size)
                can_fall_back = host == "edge" and "cf" in hosts
                try:
                    _put_part(urls[str(n)], data, attempts=1 if can_fall_back else 3)    # no token on this leg: the URL is signed
                except ApiError as e:
                    if e.status == 0 and can_fall_back:        # the edge is unreachable (blocked port): the rest goes by Cloudflare
                        host = "cf"
                        urls = sign(nums[i:], host)
                        continue
                    raise
                i += 1
            first += SIGN_BATCH
    state = request("POST", f"{base}/{session}/parts", token=token)
    if not state.get("finished") and len(state.get("done") or []) < total:
        raise Refused("The server did not receive every part; run again to retry")
    return session


MAX_RETRIES = 3


def _reserve(server: str, token: str, body: dict, say: Callable[[str], None]):
    """Reserve under the key; a failed earlier version, or a key used for a different request, is retried under a derived key
    (key:r1 .. key:r3, deterministic) so the same render can go out again as a new version."""
    base_key = body["publishKey"]
    for n in range(MAX_RETRIES + 1):
        body["publishKey"] = base_key if n == 0 else f"{base_key}:r{n}"
        try:
            res = request("POST", f"{server}/api/tools/publish/reserve", token=token, body=body)
        except ApiError as e:
            if e.status == 409 and "already used" in e.detail.lower() and n < MAX_RETRIES:
                say("Previous upload failed, publishing again as a new version.")
                continue
            raise
        if res.get("repeat") and res.get("state") == "failed":
            if n < MAX_RETRIES:
                say("Previous upload failed, publishing again as a new version.")
                continue
            raise Refused("Earlier uploads of this render failed; try again later")
        return res
    raise Refused("Could not reserve a version; try again later")


def publish(*, server: str, token: Optional[str], pick: str, note: str, enabled: bool, images, fps, api_graph, ui_graph,
            audio=None, open_browser=None, say: Callable[[str], None] = print) -> Result:
    warnings: List[str] = []
    w = recipe.seed_warning(ui_graph)
    if w:
        warnings.append(w)
    if not enabled:
        msg = "Not published: the publish switch is off. Switch it on to publish this output."
        back = note_for(pick)
        return Result(False, f"{msg}\n{back}" if back else msg, warnings)
    code, stage = parse_pick(pick)
    if not token:
        say("Not signed in: opening the browser to approve this tool.")
        signin.sign_in(server, **({"open_browser": open_browser} if open_browser else {}), say=say)
        token = tokenstore.load(server)
        if not token:
            raise Refused("Sign-in did not complete")
    frames = encode.frames_of(images)
    digest = recipe.with_audio(recipe.frames_digest(frames), encode.audio_digest(audio) if audio else None)
    key = recipe.publish_key(api_graph, digest, code, stage)
    cached = config.config_dir() / "cache" / f"{key}.mp4"
    _prune(cached.parent)
    if not cached.exists():
        encode.encode_mp4(frames, fps, cached, audio)         # raises EncodeRefused (no ffmpeg) before anything is sent
    size, checksum = cached.stat().st_size, _sha256(cached)
    body = {"shotCode": code, "stage": stage, "publishKey": key, "note": note.strip() or None,
            "recipe": recipe.build(api_graph, ui_graph, token), "size": size, "filename": "publish.mp4",
            "checksum": {"algo": "sha256", "value": checksum}}
    try:
        res = _reserve(server, token, body, lambda m: (warnings.append(m), say(m)))
        vid = res["versionId"]
        if res.get("repeat") and res.get("state") != "uploading":
            return Result(True, f"Already published as version {res['versionNumber']} (nothing sent again).", warnings,
                          res["versionNumber"], vid, True)
        session = _upload(server, token, vid, cached)
        request("POST", f"{server}/api/tools/publish/complete", token=token, body={"versionId": vid, "session": session})
    except ApiError as e:
        if e.status == 401:
            raise Refused("The credential was refused (expired or revoked): sign in again") from None
        raise Refused(f"The server refused the publish: {e.detail}") from None
    NOTES.pop((code, stage), None)                 # that note was about an older version
    return Result(True, f"Published as version {res['versionNumber']}; the encode is queued.", warnings, res["versionNumber"], vid,
                  bool(res.get("repeat")))
