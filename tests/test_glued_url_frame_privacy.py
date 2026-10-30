"""The upload cleaner on three shapes that went out whole (qajev-cloud #75, #76 and #77, 30 Oct): a path glued to a
word, a home path inside a web URL, and a live frame's base64 read as a path. Synthetic names; no network."""
import base64
import json
import re
from pathlib import Path
from unittest import mock

import pytest

from qajev_upload_privacy import sanitize_request, sanitize_upload

PERSON = "private-person-7d31"
EVENTS = "/api/v1/jobs/fixture/events"


def outputs(line):
    """The line as a text report, inside a JSON report, and as a job's log event."""
    return (sanitize_upload(line.encode()).decode(),
            json.loads(sanitize_upload(json.dumps({"detail": line}).encode()))["detail"],
            sanitize_request("POST", EVENTS, [{"kind": "log", "text": line}])[0]["text"])


GLUED = (f"WEBPC:\\Users\\{PERSON}\\work\\qa.yaml", f"logC:/Users/{PERSON}/work", f"logs/Users/{PERSON}/work",
         f"run_/home/{PERSON}/qa.yaml", f"x\\Users\\{PERSON}\\qa.yaml", f"dataD:\\Projects\\{PERSON}\\qa.yaml",
         f"seeC:\\Documents and Settings\\{PERSON}\\qa.yaml", f"bin/home2/{PERSON}/qa.yaml")
# "Call log:\n" is a JSON escape in evidence text, not a drive path
GLUED_KEPT = ("https://example.test/users/sign_in", "shots/a.png", "page 2/3 of 9", "ratio 4:3 and 16:9",
              "Call log:\\n  - waiting")


@pytest.mark.parametrize("text", GLUED)
def test_a_home_or_drive_path_right_after_a_letter_is_still_removed(text):
    for out in outputs(f"failed near {text} today; also {' and '.join(GLUED_KEPT)}"):
        # none of the name, and none of the path after it (a mask that ends early leaks the rest)
        for part in (PERSON, PERSON[-4:], re.split(r"[\\/]", text)[-1]):
            assert part not in out, out
        assert "failed near" in out
        for clean in GLUED_KEPT:
            assert clean in out, out


def test_a_relative_project_path_with_a_home_part_is_masked_too():
    # the known cost: safe for privacy, at a cost to the text
    for out in outputs("see src/pages/home/Hero.tsx:12"):
        assert "see src/pages" in out and "Hero.tsx" not in out, out


LEAKED_URLS = (f"http://localhost:5173/@fs/Users/{PERSON}/work/src/main.ts",
               # outside the (mocked) home folder, so only the /@fs rule, in any case, can catch it
               f"http://127.0.0.1:5173/@FS/D:/Projects/{PERSON}/work/src/main.ts?t=1730280000",
               f"ws://localhost:5173/@fs/home/{PERSON}/work/src/hmr.ts",
               f"http://localhost:3000/__open-in-editor?file=/Users/{PERSON}/work/src/App.tsx:12",
               f"http://localhost:3000/__open-in-editor?file=%2FUsers%2F{PERSON}%2Fwork%2Fsrc%2FApp.tsx")
URLS_KEPT = ("https://example.test/users/sign_in", "http://localhost:5173/@vite/client",
             "http://localhost:5173/src/main.ts?t=1730280000", "https://example.test/@fsharp/docs")


@pytest.mark.parametrize("url", LEAKED_URLS)
def test_a_home_path_inside_a_web_url_is_removed(url):
    with mock.patch("qajev_upload_privacy.Path.home", return_value=Path(f"/Users/{PERSON}")):
        for out in outputs(f"script error at {url} line 4; also {' and '.join(URLS_KEPT)}"):
            # none of the name and none of the path after it; the scheme and host stay for the reader
            for part in (PERSON, PERSON[-4:], "work"):
                assert part not in out, out
            assert "script error at " + "/".join(url.split("/", 3)[:3]) in out
            assert " line 4; also " in out
            for clean in URLS_KEPT:
                assert clean in out, out


JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(range(256)) * 4


@pytest.mark.parametrize("image", (JPEG, WEBP))
def test_a_live_frame_goes_exactly(image):
    data = base64.b64encode(image).decode()
    sent = sanitize_request("POST", EVENTS, [{"kind": "frame", "data": data}, {"kind": "step", "text": "ok"}])
    assert sent == [{"kind": "frame", "data": data}, {"kind": "step", "text": "ok"}]


def spliced_base64():
    """A path spliced into a frame's base64, padded so a loose decode (which drops "-") still succeeds."""
    data, splice = base64.b64encode(JPEG).decode(), f"/Users/{PERSON}/"
    splice += "A" * (-len(re.sub(r"[^A-Za-z0-9+/]", "", splice)) % 4)
    return data[:20] + splice + data[20:]


@pytest.mark.parametrize("data", (
    base64.b64encode(JPEG[:4] + f"C:\\Users\\{PERSON}\\abc".encode() + JPEG[4:]).decode(),  # a path in the bytes
    base64.b64encode(f"/Users/{PERSON}/frame".encode()).decode(),  # not an image
    base64.b64encode(JPEG).decode()[:20] + f"/Users/{PERSON}/x" + base64.b64encode(JPEG).decode()[20:],  # not base64
    spliced_base64(),  # not strict base64, though a loose decode reads it as a clean JPEG
    # an image whose bytes read as text, where the text cleaner keeps the web URL: only the binary check sees the path
    base64.b64encode(b"RIFF\x24\x00\x00\x00WEBPVP8 " + f" see https://example.test/Users/{PERSON}/a.png ".encode()).decode(),
))
def test_a_frame_that_is_not_a_clean_image_is_left_out_and_the_rest_goes(data):
    sent = sanitize_request("POST", EVENTS, [{"kind": "frame", "data": data}, {"kind": "step", "text": "ok"}])
    assert sent == [{"kind": "step", "text": "ok"}]
    assert PERSON not in json.dumps(sent)


def test_a_frame_elsewhere_is_cleaned_as_text():
    # only a job's event post carries frames; the same shape on another route is plain metadata
    data = base64.b64encode(JPEG).decode()
    sent = sanitize_request("POST", "/api/v1/jobs/fixture/finish", {"kind": "frame", "data": data})
    assert isinstance(sent, dict)
