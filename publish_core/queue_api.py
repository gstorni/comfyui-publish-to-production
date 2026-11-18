"""The shot queue for the node face and its refresh button. No ComfyUI import here, so it is testable on its own.

The credential is read and used on this (Python) side only; what leaves is shot codes, a state and the cleaned note text."""
from __future__ import annotations

import logging

from . import config, publisher, tokenstore

log = logging.getLogger("comfy_publish")

SIGN_IN = "(sign in first)"
EMPTY = "(no shots waiting)"
UNREACHABLE = "(server unreachable)"

# The one list the node's combo validates against. define_schema hands THIS object to the Combo (ComfyUI keeps it as-is and
# calls define_schema on every validation), so it is never fetched there: it is filled at start-up and by refresh(), in place.
OPTIONS: list = [SIGN_IN]
_PLACEHOLDERS = (SIGN_IN, EMPTY, UNREACHABLE)


def _shots_for(values) -> list:
    shots = []
    for v in values:
        code, _, stage = v.rpartition(" | ")
        shots.append({"value": v, "label": v,
                      "note": publisher.note_for(v),          # already clean_note'd; the JS renders it as text only
                      "state": publisher.STATES.get((code, stage), "ready")})
    return shots


def not_waiting_text(shot: str) -> str:
    code, _, _stage = (shot or "").rpartition(" | ")
    return f"{code or shot} isn't waiting for a version (in review or approved)"


def face_text(value, shots, message, read_done) -> str:
    """The note-box text for the picked value. Mirrored by show() in web/publish_to_production.js (keep them equal)."""
    for s in shots:
        if s["value"] == value:
            return (s.get("note") or "Changes requested") if s["state"] == "changes_requested" else \
                   "No version yet" if s["state"] == "no_version" else "Ready"
    if value in _PLACEHOLDERS:
        return message or value
    if message in (SIGN_IN, UNREACHABLE):
        return message
    if read_done:
        return "No shots waiting for you" if not shots else not_waiting_text(value)
    return "Press Refresh shots to read the queue"


def require_in_queue(shot: str, timeout: float = 10) -> None:
    """Refuse (publisher.Refused) a shot that is not in the user's current queue, or when the queue cannot be read. This check is
    the node's own: the server only checks assignment and that the stage is publishable. Sign-in passes (publish() signs in)."""
    payload = read_queue(timeout)
    if payload["message"] == SIGN_IN:
        return
    if payload["message"] == UNREACHABLE:
        raise publisher.Refused("Can't reach the server to check your shots; try again")
    if shot not in [s["value"] for s in payload["shots"]]:
        raise publisher.Refused(not_waiting_text(shot) if payload["shots"] else "No shots waiting for you")


def read_queue(timeout: float = 5) -> dict:
    """{"shots": [{value, label, note, state}], "message": "" | placeholder text}. Never raises, never carries a secret."""
    server = config.optional_server_url()
    token = tokenstore.load(server) if server else None
    if not (server and token):
        return {"shots": [], "message": SIGN_IN}
    try:
        values = publisher.list_shots(server, token, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        log.warning("could not read the shot list: %s", type(e).__name__)
        return {"shots": [], "message": UNREACHABLE}
    return {"shots": _shots_for(values), "message": "" if values else EMPTY}


def refresh(timeout: float = 10) -> dict:
    """Blocking (run it in an executor). Re-read the queue and update OPTIONS in place. A failed read keeps the last good list
    (and its notes) and reports the error in `message`; a first failure leaves one placeholder entry."""
    payload = read_queue(timeout)
    if payload["message"] == UNREACHABLE and any(v not in _PLACEHOLDERS for v in OPTIONS):
        return {"shots": _shots_for(OPTIONS), "message": UNREACHABLE}
    OPTIONS[:] = [s["value"] for s in payload["shots"]] or [payload["message"] or EMPTY]
    return payload
