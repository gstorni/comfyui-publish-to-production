import sys
import types

import pytest

# ComfyUI is not installed in the test environment: a stub keeps `import comfy_api` from ever being needed by the core.
sys.modules.setdefault("torch", types.ModuleType("torch"))


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COMFY_PUBLISH_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("PRODUCTION_SERVER", raising=False)
    return tmp_path / "cfg"
