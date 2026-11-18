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
