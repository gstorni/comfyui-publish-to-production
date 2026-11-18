"""Sign in from a terminal: `python signin.py` (server from PRODUCTION_SERVER or config.json), or `python signin.py --logout`."""
import sys

from publish_core import config, signin, tokenstore

if __name__ == "__main__":
    if "--logout" in sys.argv:
        tokenstore.clear()
        print("Signed out on this machine (revoke the credential in your account page too).")
    else:
        signin.sign_in(config.server_url())
        print("Signed in. Restart ComfyUI (or reload nodes) so the shot list fills.")
