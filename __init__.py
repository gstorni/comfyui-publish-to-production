"""ComfyUI custom-node package: publish only. Install by copying or symlinking this
folder into ComfyUI/custom_nodes/. Outside ComfyUI (tests, the sign-in script) the import of the node is skipped."""
try:
    from .publish_core.node import comfy_entrypoint
except ImportError:                  # no comfy_api: not running inside ComfyUI
    comfy_entrypoint = None

WEB_DIRECTORY = "./web"              # the node-face extension (note view + refresh button); ComfyUI serves it at /extensions/<this folder>/

__all__ = ["comfy_entrypoint", "WEB_DIRECTORY"]
