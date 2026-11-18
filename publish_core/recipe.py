"""What the node sends as the recipe, the publish key that makes a retry the same version, and the seed warning."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from . import config


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def key_graph(api_graph: Any) -> Any:
    """The graph as the key sees it: this node's own `note` and `publish` inputs dropped (typing a note or flipping the switch must
    not change the key); fps and everything else stay."""
    if not isinstance(api_graph, dict):
        return api_graph
    out = {}
    for nid, node in api_graph.items():
        if isinstance(node, dict) and node.get("class_type") == config.NODE_ID and isinstance(node.get("inputs"), dict):
            node = {**node, "inputs": {k: v for k, v in node["inputs"].items() if k not in ("note", "publish")}}
        out[nid] = node
    return out


def publish_key(api_graph: Any, frames_digest: str, shot_code: str, stage: str) -> str:
    """Stable: the same graph, the same output frames, the same shot and stage always give the same key (a retry is the same
    version). The note is not part of it. 'comfy-' + 40 hex chars."""
    h = hashlib.sha256()
    for part in (canonical(key_graph(api_graph)), frames_digest.encode(), shot_code.encode(), stage.encode()):
        h.update(hashlib.sha256(part).digest())
    return "comfy-" + h.hexdigest()[:40]


def frames_digest(frames) -> str:
    h = hashlib.sha256()
    h.update(str(tuple(frames.shape)).encode())
    h.update(frames.tobytes())
    return h.hexdigest()


def randomized_seeds(ui_graph: Any) -> List[str]:
    """Ids of workflow nodes whose 'control after generate' is randomize (a seed widget followed by that control). The UI workflow
    keeps the control; the API graph only keeps the seed that happened to be drawn."""
    out = []
    nodes = ui_graph.get("nodes") if isinstance(ui_graph, dict) else None
    for n in nodes or []:
        vals = n.get("widgets_values") if isinstance(n, dict) else None
        if isinstance(vals, list) and "randomize" in vals:
            out.append(f"{n.get('type', '?')} #{n.get('id', '?')}")
    return out


def seed_warning(ui_graph: Any) -> Optional[str]:
    r = randomized_seeds(ui_graph)
    if not r:
        return None
    return ("Warning: the seed is set to randomize on " + ", ".join(r) + ". The published recipe records the seed that was drawn, "
            "but re-running this workflow will give a different result. Set it to fixed to make the version reproducible.")


def build(api_graph: Any, ui_graph: Any, token: Optional[str] = None) -> Dict[str, Any]:
    """{apiGraph, uiGraph}. A last guard: the credential must never be inside what is sent as the recipe."""
    rec = {"apiGraph": api_graph, "uiGraph": ui_graph}
    if token and token.encode() in canonical(rec):
        raise ValueError("The credential was found inside the workflow; refusing to publish")
    return rec


def with_audio(frames_digest_value: str, audio_digest_value: Optional[str]) -> str:
    """The digest the key sees: unchanged without audio (existing retries stay stable), combined with the audio's otherwise."""
    if not audio_digest_value:
        return frames_digest_value
    return hashlib.sha256(f"{frames_digest_value}|audio:{audio_digest_value}".encode()).hexdigest()
