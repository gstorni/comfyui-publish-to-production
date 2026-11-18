"""The ComfyUI node (V3 schema, comfy_api.latest: https://docs.comfy.org/custom-nodes/v3_migration). Thin on purpose: all the work
is in publisher.py. Imported only inside ComfyUI."""
from __future__ import annotations

import logging

from comfy_api.latest import ComfyExtension, io      # noqa: E402 — only present inside ComfyUI

from . import config, encode, publisher, tokenstore
from .encode import EncodeRefused
from .http import ApiError

log = logging.getLogger("comfy_publish")


def _shot_options():
    """Filled when ComfyUI loads (and on a restart/refresh after signing in): my queue, 'CODE | stage'."""
    server = config.optional_server_url()
    token = tokenstore.load(server) if server else None
    if server and token:
        try:
            shots = publisher.list_shots(server, token, timeout=5)      # never hold ComfyUI's start-up
            for (code, stage) in publisher.NOTES:
                log.info("%s %s: %s", code, stage, publisher.note_for(f"{code} | {stage}"))
            return shots or ["(no shots waiting)"]
        except Exception as e:  # noqa: BLE001
            log.warning("could not read the shot list: %s", type(e).__name__)
            return ["(server unreachable)"]
    return ["(sign in first)"]


def _shot_tooltip() -> str:
    tip = "A shot assigned to you that is waiting for a version."
    back = [f"{c} v{v:03d}" for (c, _s), (v, _n) in publisher.NOTES.items()]
    if back:
        tip += " Waiting with changes requested: " + ", ".join(back[:5]) + " - run with publish off to read the note."
    return tip


class PublishToProduction(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        options = _shot_options()
        return io.Schema(
            node_id=config.NODE_ID,
            display_name=config.NODE_LABEL,
            category="publish",
            description="Publishes what you made to a shot. Publish only: it edits nothing and generates nothing.",
            inputs=[
                io.Image.Input("images", optional=True, tooltip="The frames to publish, encoded to one MP4 (needs ffmpeg). Connect images or a video."),
                io.Video.Input("video", optional=True, tooltip="A video to publish: it brings its own frame rate and audio. Connect images or a video."),
                io.Combo.Input("shot", options=options, tooltip=_shot_tooltip()),
                io.String.Input("note", multiline=True, default="", tooltip="Required from the second version."),
                io.Float.Input("fps", default=24.0, min=1.0, max=240.0, step=0.001,
                               tooltip="Frame rate for images. A video brings its own."),
                io.Boolean.Input("publish", default=False, tooltip="Nothing is sent unless this is on."),
            ],
            outputs=[io.String.Output(display_name="status")],
            hidden=[io.Hidden.prompt, io.Hidden.extra_pnginfo],
            is_output_node=True,
        )

    @classmethod
    def execute(cls, shot, note, fps, publish, images=None, video=None) -> io.NodeOutput:
        try:
            if images is None and video is None:
                raise publisher.Refused("Connect images or a video")
            if images is not None and video is not None:
                raise publisher.Refused("Connect images or a video, not both")
            audio = None
            if video is not None:
                images, fps, audio = encode.video_parts(video)
            server = config.server_url()
            res = publisher.publish(server=server, token=tokenstore.load(server), pick=shot, note=note, enabled=bool(publish),
                                    images=images, fps=fps if video is not None else float(fps), audio=audio, api_graph=cls.hidden.prompt,
                                    ui_graph=(cls.hidden.extra_pnginfo or {}).get("workflow"), say=log.info)
            lines = [res.message, *res.warnings]
        except (publisher.Refused, EncodeRefused, config.ConfigError, TimeoutError) as e:
            raise RuntimeError(str(e)) from None          # shows on the node in red; no stack of internals, no secrets
        except ApiError as e:
            raise RuntimeError(f"The server said: {e.detail}") from None
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            raise RuntimeError("The server's answer was not what this tool expects; update the tool or try again later") from None
        except OSError as e:
            raise RuntimeError(f"Could not publish ({type(e).__name__})") from None
        for line in lines:
            log.warning(line) if line.startswith("Warning") else log.info(line)
        text = "\n".join(lines)
        return io.NodeOutput(text, ui={"text": [text]})


class PublishExtension(ComfyExtension):
    async def get_node_list(self):
        return [PublishToProduction]


async def comfy_entrypoint() -> PublishExtension:
    return PublishExtension()
