import pytest

from qajev import providers as P
from qajev import vision

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
CF = {"CLOUDFLARE_ACCOUNT_ID": "acct123", "CLOUDFLARE_API_TOKEN": "cf-token-value", "QAJEV_JEV_PROVIDER": "cloudflare"}


def test_a_screenshot_goes_to_clef_as_a_data_url_within_its_limits():
    assert vision.data_url(PNG).startswith("data:image/png;base64,")
    assert vision.data_url(JPEG).startswith("data:image/jpeg;base64,")
    with pytest.raises(vision.VisionError, match="4 MiB"):
        vision.data_url(JPEG + b"\x00" * vision.MAX_BYTES)
    with pytest.raises(vision.VisionError, match="not a PNG, JPEG or WebP"):
        vision.data_url(b"GIF89a....")
    with pytest.raises(vision.VisionError, match="no screenshot"):
        vision.data_url(b"")


def test_with_vision_each_clef_decision_carries_the_screen_and_only_then():
    env = dict(CF)
    resolved = P.apply(env)
    sent = []
    post = P.route(lambda url, key, body: sent.append(body) or {"result": {"answers": {}}}, resolved, env)
    decision = {"model": "jev-latest", "state": {}, "questions": {
        "operation": {"type": "choice", "criteria": {"CLICK": "click", "DONE": "done"}}}}
    post(P.TYPESAFE_URL, env["TYPESAFE_API_KEY"], decision)
    assert "images" not in sent[-1]  # vision off: text only, as with Jev
    with vision.seeing(lambda: "data:image/jpeg;base64,AAAA"):
        post(P.TYPESAFE_URL, env["TYPESAFE_API_KEY"], decision)
        assert sent[-1]["images"] == ["data:image/jpeg;base64,AAAA"]
        post(P.TYPESAFE_URL, env["TYPESAFE_API_KEY"], {**decision, "images": ["data:image/png;base64,BB"]})
        assert sent[-1]["images"] == ["data:image/png;base64,BB"]  # a looks check brings its own picture
    assert P.SEE is None  # restored on the way out
    post(P.TYPESAFE_URL, env["TYPESAFE_API_KEY"], decision)
    assert "images" not in sent[-1]


def test_looks_are_judged_one_check_each_with_clefs_probability():
    seen = {}

    def clef(url, key, body):
        seen.update(body)
        return {"answers": {"look_0": {"type": "noul", "noul": 0.93}, "look_1": {"type": "noul", "noul": 0.08}}}

    checks = vision.look(clef, "data:image/jpeg;base64,AAAA", ["the Pro plan costs $29", "three plans are shown"],
                         {"page": {"url": "http://x/pricing", "title": "Pricing"}})
    assert seen["images"] == ["data:image/jpeg;base64,AAAA"] and seen["state"]["page"]["title"] == "Pricing"
    assert [q["type"] for q in seen["questions"].values()] == ["noul", "noul"]
    assert checks == [{"check": "looks: the Pro plan costs $29", "ok": True, "detail": "p 0.93"},
                      {"check": "looks: three plans are shown", "ok": False, "detail": "p 0.08"}]


def test_vision_and_looks_need_clef_and_say_how(monkeypatch):
    for k in ("QAJEV_JEV_PROVIDER", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key-for-test")
    assert "QAJEV_JEV_PROVIDER=cloudflare" in vision.require_clef("looks")
    for k, v in CF.items():
        monkeypatch.setenv(k, v)
    assert vision.require_clef("looks") is None


def test_a_suite_asks_for_vision_and_looks(monkeypatch):
    from qajev import runner, suite

    s = suite.parse({"name": "v", "base_url": "http://127.0.0.1:8765", "devices": ["desktop"], "vision": True,
                     "scenarios": [{"name": "drawn", "url": "/canvas.html", "goal": "Open the pricing page.",
                                    "expect": {"url": "/pricing.html"}},
                                   {"name": "looks only", "url": "/clipped.html", "vision": False,
                                    "expect": {"looks": ["the Sign up now button is fully visible"]}}]})
    assert [(x.vision, x.expect["looks"]) for x in s.scenarios] == [
        (True, []), (False, ["the Sign up now button is fully visible"])]
    monkeypatch.delenv("QAJEV_JEV_PROVIDER", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key-for-test")
    with pytest.raises(runner.ConfigError, match=r"vision needs Clef.*'drawn' asks for it"):
        runner.run(s, runner.Options())


def test_a_game_screenshot_becomes_a_picture_and_a_headless_one_is_refused(tmp_path):
    from qajev import native

    class Windowed:
        def shot(self, path):
            path.write_bytes(PNG)
            return path

    class Headless:
        def shot(self, path):
            return None

    assert native.game_image(Windowed()).startswith("data:image/png;base64,")
    with pytest.raises(vision.VisionError, match="headless"):
        native.game_image(Headless())


def test_a_game_run_sees_by_default_with_clef_and_a_window(monkeypatch):
    # José, 3 Oct: vision on by default for games. Clef reads the screen; Jev cannot, so with Jev it stays off
    # without refusing (only an explicit --vision refuses).
    for k in ("QAJEV_JEV_PROVIDER", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key-for-test")
    assert vision.for_play(None, window=True) == (False, None)  # Jev: off, quietly
    sees, refused = vision.for_play(True, window=True)
    assert sees and "needs Clef" in refused  # asked for it: say why it cannot
    for k, v in CF.items():
        monkeypatch.setenv(k, v)
    assert vision.for_play(None, window=True) == (True, None)
    assert vision.for_play(False, window=True) == (False, None)  # --no-vision, or the suite's vision: false
    assert vision.for_play(None, window=False) == (False, None)  # a headless Godot game: nothing to see
    sees, refused = vision.for_play(True, window=False)
    assert sees and "headless" in refused


def test_play_takes_vision_and_no_vision():
    from qajev.cli import build_parser

    p = build_parser()
    assert p.parse_args(["play", "g"]).vision is None
    assert p.parse_args(["play", "g", "--vision"]).vision is True
    assert p.parse_args(["play", "g", "--no-vision"]).vision is False
    assert p.parse_args(["check", "http://h/"]).vision is False  # websites stay text-only unless asked
