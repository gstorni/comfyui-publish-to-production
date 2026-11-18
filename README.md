# comfyui-publish-to-production

A ComfyUI custom node that **publishes what you already made** to a shot in your production. Publish only: it edits nothing and
generates nothing. The user-facing name is one constant (`PRODUCT_NAME` in `publish_core/config.py`).
Licence: GPL-3.0-or-later, see `LICENSE`.

## Install

1. Copy or symlink this folder into `ComfyUI/custom_nodes/comfy-publish`. No pip dependencies (ComfyUI already has numpy).
2. Install **ffmpeg** and put it on `PATH` (the node encodes your frames to one MP4 and refuses clearly if it is missing).
3. Tell it your server (https; plain http only for a loopback or private-network address):
   `export PRODUCTION_SERVER=https://your-server.example` or write `{"server": "https://..."}` to
   `~/.config/comfy-publish/config.json` (`%APPDATA%\comfy-publish` on Windows).
4. Sign in once: `python signin.py` in this folder (or just run the node with Publish on: it opens the browser the same way).
   Approve in the browser, choosing the production. The credential is saved in `token.json` in that config folder, mode 0600, and
   is never part of the workflow or the log. `python signin.py --logout` removes it (also revoke it in your account page).
5. Restart ComfyUI so the shot list fills (it lists the shots assigned to you that are waiting for a version).

## Use

Add **Publish to production** (category `publish`) after your final image/video node. Pick the shot, add a note (required from the
second version), and switch **publish** on. It starts off: nothing is sent until you turn it on. The node warns when a sampler's
seed is set to randomize (the recipe stores the seed that was drawn, but a re-run would differ). Running the same graph with the
same frames again is a retry: you get the same version back, nothing is duplicated.

## Develop

`python3 -m pytest tools/comfy-publish -q` (needs `pytest` and `numpy`; ComfyUI is not needed, it is stubbed out).
The node follows ComfyUI's current V3 schema (`comfy_api.latest`, `comfy_entrypoint`).

## Licence

GPL-3.0-or-later (see LICENSE). ComfyUI is GPL-3.0 and this node runs inside it. The node only talks to the production server over HTTPS; the server is a separate program.
