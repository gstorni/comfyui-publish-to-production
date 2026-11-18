import json
import os
import stat
import threading
import urllib.request

import numpy as np
import pytest

from publish_core import config, encode, publisher, recipe, signin, tokenstore
from publish_core.http import ApiError
from tests.fake_server import TOKEN, Fake

API = {"3": {"class_type": "KSampler", "inputs": {"seed": 5, "steps": 20}}, "9": {"class_type": config.NODE_ID, "inputs": {"note": "a", "publish": True, "fps": 24.0}}}
UI = {"nodes": [{"id": 3, "type": "KSampler", "widgets_values": [5, "fixed", 20]}]}
UI_RANDOM = {"nodes": [{"id": 3, "type": "KSampler", "widgets_values": [5, "randomize", 20]}]}
needs_ffmpeg = pytest.mark.skipif(encode.find_ffmpeg() is None, reason="ffmpeg not installed")


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.stop()


def imgs(seed=0, n=2):
    return np.random.default_rng(seed).random((n, 16, 16, 3), dtype=np.float32)


def run(fake, enabled=True, token=TOKEN, images=None, ui=UI, note="", pick="SH010 | gen"):
    return publisher.publish(server=fake.url, token=token, pick=pick, note=note, enabled=enabled,
                             images=imgs() if images is None else images, fps=24.0, api_graph=API, ui_graph=ui)


def test_publish_key_is_stable_and_sensitive():
    f = recipe.frames_digest(encode.frames_of(imgs()))
    k = recipe.publish_key(API, f, "SH010", "gen")
    assert k == recipe.publish_key(json.loads(json.dumps(API)), f, "SH010", "gen")
    assert k.startswith("comfy-") and len(k) <= 200
    assert k != recipe.publish_key(API, f, "SH020", "gen")
    assert k != recipe.publish_key(API, recipe.frames_digest(encode.frames_of(imgs(1))), "SH010", "gen")
    changed = {**API, "3": {"class_type": "KSampler", "inputs": {"seed": 6, "steps": 20}}}
    assert k != recipe.publish_key(changed, f, "SH010", "gen")


def test_seed_randomize_warns_and_fixed_does_not():
    assert "randomize" in recipe.seed_warning(UI_RANDOM)
    assert recipe.seed_warning(UI) is None
    assert recipe.seed_warning(None) is None


def test_token_file_is_user_only(home):
    tokenstore.save("https://x.example", TOKEN)
    p = tokenstore.path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    assert tokenstore.load("https://x.example") == TOKEN
    assert tokenstore.load("https://other.example") is None      # never offered to another server


def test_url_policy():
    assert config.check_url("https://flow.example.com/") == "https://flow.example.com"
    assert config.check_url("http://127.0.0.1:8080") and config.check_url("http://192.168.1.10:8080")
    for bad in ("http://flow.example.com", "ftp://x", "https://u:p@x.example", "", "http://8.8.8.8"):
        with pytest.raises(config.ConfigError):
            config.check_url(bad)


def test_no_token_in_recipe():
    rec = recipe.build(API, UI, TOKEN)
    assert TOKEN not in json.dumps(rec)
    with pytest.raises(ValueError):
        recipe.build({"x": {"inputs": {"k": TOKEN}}}, UI, TOKEN)


def test_publish_off_sends_nothing(fake):
    r = run(fake, enabled=False, ui=UI_RANDOM)
    assert not r.published and "off" in r.message
    assert "randomize" in r.warnings[0]
    assert fake.calls == []


def test_no_pick_is_refused(fake):
    with pytest.raises(publisher.Refused):
        run(fake, pick="(sign in first)")
    assert fake.calls == []


def test_no_ffmpeg_refuses_before_sending(fake, monkeypatch):
    monkeypatch.setattr(encode, "find_ffmpeg", lambda: None)
    with pytest.raises(encode.EncodeRefused, match="ffmpeg"):
        run(fake)
    assert fake.calls == []


@needs_ffmpeg
def test_sequence_and_retry_is_same_version(fake):
    r1 = run(fake, images=imgs(n=3))
    assert r1.published and r1.version_number == 1 and not r1.repeat
    paths = [(m, p) for m, p, _ in fake.calls]
    order = [p for m, p in paths]
    assert order[0] == "/api/tools/publish/reserve"
    assert order[1].endswith("/upload/init") and order[2].endswith("/sign")
    assert any(m == "PUT" for m, _ in paths)
    assert order[-2].endswith("/parts") and order[-1] == "/api/tools/publish/complete"
    body = fake.reserved_bodies[0]
    assert body["checksum"]["algo"] == "sha256" and len(body["checksum"]["value"]) == 64 and body["size"] > 0
    assert TOKEN not in json.dumps(body)
    assert body["recipe"] == {"apiGraph": API, "uiGraph": UI}
    assert all(not auth for m, p, auth in fake.calls if m == "PUT")      # the token never goes to the upload URL
    # same graph + same frames again: same publish key, the server answers the same version, nothing is uploaded twice
    n_put = sum(1 for m, _, _ in fake.calls if m == "PUT")
    fake.versions[body["publishKey"]]["view"]["state"] = "processing"
    r2 = run(fake, images=imgs(n=3))
    assert r2.version_id == r1.version_id and r2.version_number == 1 and r2.repeat
    assert fake.reserved_bodies[1]["publishKey"] == body["publishKey"]
    assert sum(1 for m, _, _ in fake.calls if m == "PUT") == n_put


@needs_ffmpeg
def test_failed_complete_then_retry_same_version(fake):
    fake.fail_complete_once = True
    with pytest.raises(publisher.Refused, match="refused"):
        run(fake)
    r = run(fake)                                       # still 'uploading' on the server: same version, upload again, complete
    assert r.version_number == 1 and r.repeat
    assert len(fake.versions) == 1


@needs_ffmpeg
def test_revoked_token_is_a_clear_refusal(fake):
    with pytest.raises(publisher.Refused, match="sign in again"):
        run(fake, token="fct_wrong")


def test_list_shots(fake):
    assert publisher.list_shots(fake.url, TOKEN) == ["SH010 | gen"]
    with pytest.raises(ApiError):
        publisher.list_shots(fake.url, "bad")


def _back(fake, note="Too dark\nin the corner \x1b[31m<b>x</b>"):
    fake.shots = [{"shotCode": "ZZAI_030", "stage": "generation", "directorNote": note,
                   "latestVersion": {"number": 4, "reviewState": "changes_requested"}},
                  {"shotCode": "SH010", "stage": "gen", "directorNote": None, "latestVersion": {"number": 1, "reviewState": "approved"}}]


def test_returned_shot_label_parses_back(fake):
    _back(fake)
    labels = publisher.list_shots(fake.url, TOKEN)
    assert labels == ["ZZAI_030 | generation", "SH010 | gen"]       # same options with or without a note
    fake.shots = [{"shotCode": "ZZAI_030", "stage": "generation"}, {"shotCode": "SH010", "stage": "gen"}]
    assert publisher.list_shots(fake.url, TOKEN) == labels
    assert publisher.parse_pick(labels[0]) == ("ZZAI_030", "generation")
    assert publisher.parse_pick("A | B | gen") == ("A | B", "gen")


def test_publish_off_status_includes_clean_note(fake):
    _back(fake)
    pick = publisher.list_shots(fake.url, TOKEN)[0]
    assert pick == "ZZAI_030 | generation"
    r = run(fake, enabled=False, pick=pick)
    assert not r.published
    assert "Changes requested on v004: Too dark in the corner" in r.message
    assert "\x1b" not in r.message and "<" not in r.message and r.message.count("\n") == 1


def test_failed_read_keeps_notes_and_publish_drops_them(fake):
    _back(fake)
    publisher.list_shots(fake.url, TOKEN)
    with pytest.raises(ApiError):
        publisher.list_shots(fake.url, "bad")
    assert "v004" in publisher.note_for("ZZAI_030 | generation")
    r = run(fake, pick="ZZAI_030 | generation")
    assert r.published
    assert publisher.note_for("ZZAI_030 | generation") == ""


def test_clean_note_drops_format_chars():
    assert publisher.clean_note("a\u202eb\u200bc") == "abc"


def test_no_note_keeps_today_label_and_status(fake):
    assert publisher.list_shots(fake.url, TOKEN) == ["SH010 | gen"]
    assert "Changes requested" not in run(fake, enabled=False).message


def test_signin_flow_saves_0600_token(fake):
    def approve(url):                                   # the "browser": approve, which redirects to the tool's loopback listener
        s = fake.start
        u = f"http://127.0.0.1:{s['port']}/tool-signin/{s['nonce']}?code=the-one-time-code-xxxxxx&state={s['state']}"
        threading.Thread(target=lambda: urllib.request.urlopen(u).read(), daemon=True).start()
        assert url == f"{fake.url}/tool-signin#r={'r' * 43}"
        return True
    signin.sign_in(fake.url, open_browser=approve, wait_seconds=10)
    assert fake.start["tool"] == "comfy" and fake.start["challenge"] and len(fake.start["challenge"]) == 43
    assert tokenstore.load(fake.url) == TOKEN
    assert stat.S_IMODE(tokenstore.path().stat().st_mode) == 0o600


def test_signin_rejects_wrong_state(fake):
    def evil(url):
        s = fake.start

        def hit():
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{s['port']}/tool-signin/{s['nonce']}?code=x&state=wrong")
            except Exception:
                pass
        threading.Thread(target=hit, daemon=True).start()
        return True
    with pytest.raises(TimeoutError):
        signin.sign_in(fake.url, open_browser=evil, wait_seconds=2)
    assert tokenstore.load(fake.url) is None


def _with_note(note, fps=24.0, publish=True):
    return {**API, "9": {"class_type": config.NODE_ID, "inputs": {"note": note, "publish": publish, "fps": fps}}}


def test_note_and_switch_do_not_change_the_key():
    f = "d"
    k = recipe.publish_key(_with_note("one"), f, "SH010", "gen")
    assert k == recipe.publish_key(_with_note("two", publish=False), f, "SH010", "gen")
    assert k != recipe.publish_key(_with_note("one", fps=25.0), f, "SH010", "gen")


@needs_ffmpeg
def test_failed_version_is_republished_under_a_derived_key(fake):
    r1 = run(fake)
    key = fake.reserved_bodies[0]["publishKey"]
    fake.versions[key]["view"]["state"] = "failed"
    r2 = run(fake)
    assert fake.reserved_bodies[-1]["publishKey"] == key + ":r1"
    assert r2.version_number == 2 and r2.version_number != r1.version_number
    assert any("Previous upload failed" in w for w in r2.warnings)


@needs_ffmpeg
def test_key_already_used_for_another_request_goes_to_derived_key(fake):
    run(fake)
    key = fake.reserved_bodies[0]["publishKey"]
    fake.versions[key]["size"] += 1                     # the server remembers a different request under this key
    r = run(fake)
    assert fake.reserved_bodies[-1]["publishKey"] == key + ":r1" and r.version_number == 2
    assert any("Previous upload failed" in w for w in r.warnings)


@needs_ffmpeg
def test_all_derived_keys_failing_is_an_error_never_success(fake):
    run(fake)
    key = fake.reserved_bodies[0]["publishKey"]
    for k in (key, key + ":r1", key + ":r2", key + ":r3"):
        fake.versions[k] = {"uploader": "x", "stage": "gen", "size": 1, "checksum": "0" * 64,
                            "view": {"versionId": "x", "versionNumber": 9, "state": "failed", "publishKey": k}}
    with pytest.raises(publisher.Refused):
        run(fake)


@needs_ffmpeg
def test_edge_unreachable_falls_back_to_cf(fake):
    fake.edge_dead = True
    r = run(fake, images=imgs(n=2))
    assert r.published
    assert fake.sign_hosts[0] == "edge" and "cf" in fake.sign_hosts
    assert any(m == "PUT" for m, _, _ in fake.calls)


@needs_ffmpeg
def test_parts_answer_finished_without_done(fake):
    fake.parts_finished = True
    assert run(fake).published


def test_idle_socket_does_not_hold_the_listener_past_the_deadline(fake):
    import socket
    import time
    idle = []

    def browser(url):
        idle.append(socket.create_connection(("127.0.0.1", fake.start["port"])))     # connects, sends nothing
        return True
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        signin.sign_in(fake.url, open_browser=browser, wait_seconds=1)
    assert time.monotonic() - t0 < 3
    idle[0].close()


def test_proxy_environment_is_ignored(fake, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    with pytest.raises(Exception):                      # control: the default opener would send it to the (dead) proxy
        urllib.request.urlopen(fake.url + "/api/tools/shots", timeout=3)
    assert publisher.list_shots(fake.url, TOKEN) == ["SH010 | gen"]


def test_leftover_part_is_never_used_and_encode_is_atomic(home, monkeypatch):
    out = home / "cache" / "k.mp4"
    out.parent.mkdir(parents=True)
    (home / "cache" / "k.mp4.part").write_bytes(b"junk")
    if encode.find_ffmpeg():
        encode.encode_mp4(encode.frames_of(imgs()), 24.0, out)
        assert out.read_bytes()[:12] != b"junk" and not (home / "cache" / "k.mp4.part").exists()
    monkeypatch.setattr(encode, "find_ffmpeg", lambda: "/bin/false")
    out2 = home / "cache" / "k2.mp4"
    with pytest.raises(encode.EncodeRefused):
        encode.encode_mp4(encode.frames_of(imgs()), 24.0, out2)
    assert not out2.exists() and not (home / "cache" / "k2.mp4.part").exists()


def test_stale_token_tmp_is_replaced(home):
    home.mkdir(parents=True)
    stale = home / "token.json.tmp"
    stale.write_text("old")
    os.chmod(stale, 0o666)
    tokenstore.save("https://x.example", TOKEN)
    assert stat.S_IMODE(tokenstore.path().stat().st_mode) == 0o600


def test_requests_send_own_user_agent(monkeypatch):
    # Cloudflare answers 403 / error 1010 to the default "Python-urllib" agent; every call must name the node instead.
    from publish_core import http as h, config as c
    seen = {}

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def fake_open(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return _R()

    monkeypatch.setattr(h._opener, "open", fake_open)
    h.request("GET", "https://example.com/api/tools/shots", token="t")
    assert seen["ua"] == c.USER_AGENT and seen["ua"].startswith("comfyui-publish-to-production/")


def test_result_pages_are_static_and_complete():
    from publish_core import signin as s
    ok, no = s.result_page(True), s.result_page(False)
    assert "Sign-in approved" in ok and "Sign-in not approved" in no
    for page in (ok, no):
        assert page.startswith("<!doctype html>") and "<script" not in page and "{" not in page.split("<style>")[0]


# ---- video input and audio (v0.2.0)
from fractions import Fraction
import subprocess
import sys
import types


def sine(sr=16000, secs=0.2, ch=1, freq=440.0):
    t = np.arange(int(sr * secs)) / sr
    w = np.tile(np.sin(2 * np.pi * freq * t).astype(np.float32), (ch, 1))
    return {"waveform": w[None], "sample_rate": sr}


class FakeVideo:
    def __init__(self, images, audio=None, rate=Fraction(16, 1)):
        self._c = types.SimpleNamespace(images=images, audio=audio, frame_rate=rate)

    def get_components(self):
        return self._c


def test_video_parts_reads_components():
    images, fps, audio = encode.video_parts(FakeVideo(imgs(), sine(), Fraction(16, 1)))
    assert fps == 16 and images.shape[0] == 2 and audio[0] == 16000 and audio[1].shape == (1, 3200)
    assert encode.video_parts(FakeVideo(imgs()))[2] is None
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(encode.EncodeRefused):
            encode.video_parts(FakeVideo(imgs(), None, bad))
    with pytest.raises(encode.EncodeRefused):
        encode.video_parts(FakeVideo(imgs(), {"waveform": np.zeros((1, 1, 10), np.float32), "sample_rate": 100}))


def test_audio_changes_the_key_and_none_keeps_it():
    f = recipe.frames_digest(encode.frames_of(imgs()))
    base = recipe.publish_key(API, f, "SH010", "gen")
    assert recipe.with_audio(f, None) == f and recipe.publish_key(API, recipe.with_audio(f, None), "SH010", "gen") == base
    a1 = encode.audio_digest(encode.audio_of(sine(freq=440.0)))
    a2 = encode.audio_digest(encode.audio_of(sine(freq=880.0)))
    k1, k2 = (recipe.publish_key(API, recipe.with_audio(f, a), "SH010", "gen") for a in (a1, a2))
    assert len({base, k1, k2}) == 3


def _capture(monkeypatch):
    seen = {}

    def fake_run(cmd, input=None, capture_output=None):
        seen["cmd"] = cmd
        seen["pcm"] = [c for c in cmd if c.endswith(".pcm")]
        seen["pcm_existed"] = [os.path.exists(c) for c in seen["pcm"]]
        open(cmd[-1], "wb").write(b"x")
        return types.SimpleNamespace(returncode=0, stderr=b"")
    monkeypatch.setattr(encode, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_encode_command_with_and_without_audio(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    frames = encode.frames_of(imgs())
    encode.encode_mp4(frames, 24.0, tmp_path / "a.mp4", encode.audio_of(sine(ch=2)))
    c = seen["cmd"]
    assert c[c.index("-f", 10) + 1] == "f32le" and "aac" in c and "192k" in c and "-shortest" not in c
    assert c[c.index("-ac") + 1] == "2" and c[c.index("-ar") + 1] == "16000"
    assert seen["pcm_existed"] == [True] and not os.path.exists(seen["pcm"][0])      # temp file cleaned up
    encode.encode_mp4(frames, 24.0, tmp_path / "b.mp4")
    assert "aac" not in seen["cmd"] and "-map" in seen["cmd"] and seen["cmd"].count("-i") == 1


def test_audio_temp_removed_on_failure(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        encode.encode_mp4(encode.frames_of(imgs()), 24.0, tmp_path / "a.mp4", encode.audio_of(sine()))
    assert not list(tmp_path.glob("*.pcm"))


@needs_ffmpeg
def test_real_encode_has_audio_stream(tmp_path):
    if not shutil_which("ffprobe"):
        pytest.skip("ffprobe not installed")
    out = encode.encode_mp4(encode.frames_of(imgs()), 16.0, tmp_path / "v.mp4", encode.audio_of(sine(ch=2)))
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0", str(out)],
                             capture_output=True, text=True).stdout
    assert "audio" in streams and "aac" in streams and "video" in streams
    assert not list(tmp_path.glob("*.pcm")) and not list(tmp_path.glob("*.part"))
    out2 = encode.encode_mp4(encode.frames_of(imgs()), 16.0, tmp_path / "n.mp4")
    assert "audio" not in subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(out2)],
                                         capture_output=True, text=True).stdout


def shutil_which(x):
    import shutil
    return shutil.which(x)


def test_publish_with_audio_and_images_key_matches(fake, monkeypatch):
    seen = _capture(monkeypatch)
    r = publisher.publish(server=fake.url, token=TOKEN, pick="SH010 | gen", note="", enabled=True, images=imgs(), fps=16.0,
                          audio=encode.audio_of(sine()), api_graph=API, ui_graph=UI)
    assert r.published and "aac" in seen["cmd"] and "16" in seen["cmd"]
    f = recipe.frames_digest(encode.frames_of(imgs()))
    plain = recipe.publish_key(API, f, "SH010", "gen")
    assert not (config.config_dir() / "cache" / f"{plain}.mp4").exists()      # the audio version is cached under a different key


def _node_module(monkeypatch):
    """node.py needs comfy_api; stub just enough to import it and run execute()."""
    class Dummy:
        def __getattr__(self, n):
            return lambda *a, **k: None
    class Node:
        hidden = types.SimpleNamespace(prompt=API, extra_pnginfo={"workflow": UI})
    io = types.SimpleNamespace(ComfyNode=Node, Schema=object, NodeOutput=lambda *a, **k: a)
    mod = types.ModuleType("comfy_api.latest")
    mod.ComfyExtension, mod.io = object, io
    monkeypatch.setitem(sys.modules, "comfy_api", types.ModuleType("comfy_api"))
    monkeypatch.setitem(sys.modules, "comfy_api.latest", mod)
    sys.modules.pop("publish_core.node", None)
    import publish_core.node as node
    return node


def test_node_refuses_none_and_both(monkeypatch):
    node = _node_module(monkeypatch)
    with pytest.raises(RuntimeError, match="Connect images or a video$"):
        node.PublishToProduction.execute("SH010 | gen", "", 24.0, True)
    with pytest.raises(RuntimeError, match="not both"):
        node.PublishToProduction.execute("SH010 | gen", "", 24.0, True, images=imgs(), video=FakeVideo(imgs()))


def test_node_video_fps_overrides_widget(monkeypatch):
    node = _node_module(monkeypatch)
    got = {}
    monkeypatch.setattr(node.publisher, "publish", lambda **kw: got.update(kw) or publisher.Result(True, "ok", []))
    monkeypatch.setattr(node.config, "server_url", lambda: "https://x.example")
    monkeypatch.setattr(node.tokenstore, "load", lambda s: TOKEN)
    node.PublishToProduction.execute("SH010 | gen", "", 24.0, True, video=FakeVideo(imgs(), sine(), Fraction(16, 1)))
    assert got["fps"] == 16.0 and got["audio"][0] == 16000
    node.PublishToProduction.execute("SH010 | gen", "", 30.0, True, images=imgs())
    assert got["fps"] == 30.0 and got["audio"] is None


def test_video_rate_bounds_exact_and_empty_audio(tmp_path, monkeypatch):
    # review: a frame rate outside the widget's 1-240 is refused; NTSC stays exact; an empty audio batch is no audio
    for bad in (Fraction(10**9, 1), Fraction(1, 10**6), 241):
        with pytest.raises(encode.EncodeRefused):
            encode.video_parts(FakeVideo(imgs(), None, bad))
    _, fps, _ = encode.video_parts(FakeVideo(imgs(), None, Fraction(24000, 1001)))
    assert fps == Fraction(24000, 1001) and encode._rate(fps) == "24000/1001" and encode._rate(23.976) == "23.976"
    empty = {"waveform": np.zeros((0, 2, 100), np.float32), "sample_rate": 48000}
    assert encode.video_parts(FakeVideo(imgs(), empty))[2] is None


def test_ffmpeg_failure_text_stays_in_the_log(tmp_path, monkeypatch):
    monkeypatch.setattr(encode, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(encode.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stderr=b"[mp4 @ 0x5f15e462dc40] internal detail"))
    with pytest.raises(encode.EncodeRefused) as e:
        encode.encode_mp4(encode.frames_of(imgs()), 24.0, tmp_path / "x.mp4")
    assert "0x5f" not in str(e.value) and "ComfyUI log" in str(e.value)


# --- 0.4.0: node-face note + refresh route payload (publish_core.queue_api) ---
def _sign_in(fake, monkeypatch):
    monkeypatch.setenv("PRODUCTION_SERVER", fake.url)
    tokenstore.save(fake.url, TOKEN)


def test_queue_payload_shape_notes_and_no_credential(fake, monkeypatch):
    from publish_core import queue_api
    _back(fake)
    _sign_in(fake, monkeypatch)
    out = queue_api.read_queue()
    assert [s["value"] for s in out["shots"]] == ["ZZAI_030 | generation", "SH010 | gen"]     # values unchanged: no label suffix
    assert set(out["shots"][0]) == {"value", "label", "note", "state"}
    first = out["shots"][0]
    assert first["state"] == "changes_requested" and first["note"].startswith("Changes requested on v004: Too dark in the corner")
    assert "<" not in first["note"] and "\x1b" not in first["note"] and "\n" not in first["note"]
    assert out["shots"][1]["state"] == "ready" and out["shots"][1]["note"] == ""
    assert TOKEN not in json.dumps(out)
    fake.shots = [{"shotCode": "NEW1", "stage": "gen"}]
    assert queue_api.read_queue()["shots"][0]["state"] == "no_version"


def test_queue_placeholders_and_options_follow_in_place(fake, monkeypatch):
    from publish_core import queue_api
    assert queue_api.read_queue()["message"] == queue_api.SIGN_IN
    _sign_in(fake, monkeypatch)
    held = queue_api.OPTIONS
    _back(fake)
    queue_api.refresh()
    assert queue_api.OPTIONS is held and held == ["ZZAI_030 | generation", "SH010 | gen"]
    fake.shots = []
    p = queue_api.refresh()
    assert p["shots"] == [] and p["message"] == queue_api.EMPTY and held == [queue_api.EMPTY]


def test_failed_refresh_keeps_last_good_list(fake, monkeypatch):
    from publish_core import queue_api
    _back(fake)
    _sign_in(fake, monkeypatch)
    queue_api.refresh()
    good = list(queue_api.OPTIONS)
    monkeypatch.setattr(publisher, "list_shots", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    out = queue_api.refresh(timeout=1)
    assert out["message"] == queue_api.UNREACHABLE and [s["value"] for s in out["shots"]] == good
    assert out["shots"][0]["note"].startswith("Changes requested on v004")
    assert queue_api.OPTIONS == good


def test_define_schema_makes_no_network_call(monkeypatch):
    """ComfyUI calls define_schema on every validation: it must hand back the live list and never fetch."""
    import importlib
    import types
    from publish_core import queue_api

    class _In:
        def __init__(self, id=None, **kw): self.id, self.options = id, kw.get("options")
    io = types.SimpleNamespace(
        ComfyNode=object, Schema=lambda **kw: types.SimpleNamespace(**kw), Hidden=types.SimpleNamespace(prompt=1, extra_pnginfo=2),
        Image=types.SimpleNamespace(Input=_In), Video=types.SimpleNamespace(Input=_In), Combo=types.SimpleNamespace(Input=_In),
        String=types.SimpleNamespace(Input=_In, Output=_In), Float=types.SimpleNamespace(Input=_In), Boolean=types.SimpleNamespace(Input=_In),
        NodeOutput=object)
    ca = types.ModuleType("comfy_api"); lat = types.ModuleType("comfy_api.latest")
    lat.ComfyExtension, lat.io = object, io
    monkeypatch.setitem(sys.modules, "comfy_api", ca); monkeypatch.setitem(sys.modules, "comfy_api.latest", lat)
    sys.modules.pop("publish_core.node", None)
    node = importlib.import_module("publish_core.node")
    calls = []
    monkeypatch.setattr(publisher, "list_shots", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(1))
    schema = node.PublishToProduction.define_schema()
    shot = [i for i in schema.inputs if i.id == "shot"][0]
    assert shot.options is queue_api.OPTIONS and calls == []
    sys.modules.pop("publish_core.node", None)


def test_refresh_keeps_or_flags_selection():
    """The JS rule, mirrored: a selection that left the queue stays in the list (shown missing), never swapped."""
    import re
    js = open(os.path.join(os.path.dirname(__file__), "..", "web", "publish_to_production.js")).read()
    assert "values.push(current)" in js and "textContent" not in js.replace("textContent-equivalent", "")
    assert "innerHTML" not in js and "serialize" in js
    assert re.search(r"WEB_DIRECTORY\s*=\s*\"\./web\"", open(os.path.join(os.path.dirname(__file__), "..", "__init__.py")).read())
