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
5. Restart ComfyUI once so the node loads (it lists the shots assigned to you that are waiting for a version). After that, new shots and notes need no restart: use **Refresh shots** (see Use).

## Use

Add **Publish to production** (category `publish`) after your final image/video node. Pick the shot, add a note (required from the
second version), and switch **publish** on. It starts off: nothing is sent until you turn it on. The node warns when a sampler's
seed is set to randomize (the recipe stores the seed that was drawn, but a re-run would differ). Running the same graph with the
same frames again is a retry: you get the same version back, nothing is duplicated.
A shot a reviewer sent back is named in the shot input's tooltip ("changes requested", with the version); its note is logged when ComfyUI starts and shown in the node's status when publish is off. The list entries themselves never change.
The node face shows the note for the picked shot in a read-only box ("Changes requested on v004: ...", "No version yet" or "Ready"). **Refresh shots** re-reads your queue and the reviewers' notes without restarting ComfyUI: your current selection is kept, and if it is no longer in the queue it stays selected and is flagged as missing (the run then refuses it; pick another). Only shot codes and note text reach the browser, never your credential.

Connect either IMAGE frames or a VIDEO (the purple wire from an image-to-video node), not both. A video brings its own frame rate
and its audio, which goes into the MP4; the **fps** widget only applies to images.

## Develop

`python3 -m pytest tools/comfy-publish -q` (needs `pytest` and `numpy`; ComfyUI is not needed, it is stubbed out).
The node follows ComfyUI's current V3 schema (`comfy_api.latest`, `comfy_entrypoint`).

## Licence

GPL-3.0-or-later (see LICENSE). ComfyUI is GPL-3.0 and this node runs inside it. The node only talks to the production server over HTTPS; the server is a separate program.
