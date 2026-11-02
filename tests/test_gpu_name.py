"""Each report names the GPU renderer its Chrome drew with (browser.gpu).

workload-gpu, 2 Nov: a QAJev Chrome there drew with SwiftShader, the CPU, on a machine with an NVIDIA L4, and no report
said so. WebGL and WebGPU pages then draw slowly or not at all, and their results say little about the page.
"""

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from test_live import site  # noqa: F401  (the fixture server)

from qajev import cli, report, report_html, runner
from qajev.session import _SOFTWARE_GPU

LIVE = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")
L4 = {"device": "ANGLE (NVIDIA, NVIDIA L4 (0x000027B8) Direct3D11 vs_5_0 ps_5_0)"}
SWIFT = {"device": "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)"}


@pytest.fixture
def nvidia(tmp_path, monkeypatch):
    """This machine's NVIDIA driver folder: empty (no GPU) until a test adds one."""
    gpus = tmp_path / "gpus"
    gpus.mkdir()
    monkeypatch.setattr(runner, "NVIDIA_GPUS", gpus)
    return gpus


def test_the_renderer_moves_from_the_first_result_to_the_browser_record(nvidia):
    browser, results = {"managed": True}, [{"name": "a", "browser_gpu": L4}, {"name": "b"}]
    runner.lift_gpu(browser, results)
    assert browser["gpu"] == L4
    assert all("browser_gpu" not in r for r in results)


def test_a_software_renderer_is_marked_and_noted_only_on_a_machine_with_an_nvidia_gpu(nvidia):
    browser = {}
    runner.lift_gpu(browser, [{"name": "a", "browser_gpu": SWIFT}])
    assert browser["gpu"]["software"] == "SwiftShader" and "note" not in browser["gpu"]  # no GPU here: as expected
    (nvidia / "0000:31:00.0").mkdir()
    browser = {}
    runner.lift_gpu(browser, [{"name": "a", "browser_gpu": SWIFT}])
    assert browser["gpu"]["software"] == "SwiftShader"
    assert "on a machine with an NVIDIA GPU" in browser["gpu"]["note"], browser
    browser = {}
    runner.lift_gpu(browser, [{"name": "a", "browser_gpu": L4}])
    assert "software" not in browser["gpu"] and "note" not in browser["gpu"]


def test_chromes_that_drew_with_other_renderers_are_listed_and_none_is_kept(nvidia):
    browser = {}
    runner.lift_gpu(browser, [{"name": "a", "browser_gpu": L4}, {"name": "b", "browser_gpu": L4},
                              {"name": "c", "browser_gpu": SWIFT}])
    assert browser["gpu"]["device"] == L4["device"] and browser["gpu"]["others"] == [SWIFT]
    browser = {}
    runner.lift_gpu(browser, [{"name": "a", "browser_gpu": {"none": "the page has no WebGL to name its GPU"}}])
    assert browser["gpu"] == {"none": "the page has no WebGL to name its GPU"}
    browser = {}
    runner.lift_gpu(browser, [{"name": "a"}])
    assert "gpu" not in browser


def test_the_report_page_and_text_show_the_gpu_line(tmp_path):
    gpu = {**SWIFT, "software": "SwiftShader", "note": "Chrome drew with a software renderer (SwiftShader) ...",
           "others": [L4]}
    line = report_html.gpu_line(gpu)
    assert line.startswith(SWIFT["device"]) and "(software: SwiftShader)" in line and "Chrome drew with" in line
    assert L4["device"] in line
    data = report.build(type("S", (), {"name": "s"})(), [], [], browser={"gpu": gpu}, started_at=time.time(),
                        strict=False, interrupted=False, run_dir=tmp_path)
    assert [x for x in report.markdown(data).splitlines() if x.startswith("- GPU:")] == [f"- GPU: {line}"]
    assert f"<dt>GPU</dt><dd>{report_html._e(line)}</dd>" in report_html.render(data)
    data["browser"].pop("gpu")
    assert "- GPU:" not in report.markdown(data) and "<dt>GPU</dt>" not in report_html.render(data)


def test_the_terminal_warns_when_chrome_drew_in_software_on_a_gpu_machine(tmp_path, capsys):
    data = report.build(type("S", (), {"name": "s"})(), [], [], started_at=time.time(), strict=False,
                        browser={"gpu": {**SWIFT, "software": "SwiftShader", "note": "Chrome drew with software"}},
                        interrupted=False, run_dir=tmp_path)
    cli._finish(SimpleNamespace(json=False, quiet=False), data)
    assert "qajev: warning: Chrome drew with software" in capsys.readouterr().err
    data["browser"]["gpu"].pop("note")
    cli._finish(SimpleNamespace(json=False, quiet=False), data)
    assert "warning" not in capsys.readouterr().err


@LIVE
def test_a_run_and_a_smoke_name_the_renderer_their_chrome_drew_with(site, tmp_path):  # noqa: F811
    suite = tmp_path / "gpu.qajev.yaml"
    suite.write_text(f"""name: gpu
base_url: {site}
devices: [desktop]
scenarios:
  - name: a page
    about: fixture
    url: /canvas-lit.html
    expect: {{text: ["the hall stands"]}}
""")
    env = {**os.environ, "QAJEV_RUNNER": "local"}
    for args in (["run", str(suite)], ["smoke", f"{site}/canvas-lit.html", "--max-pages", "1", "--ux", "off"]):
        p = subprocess.run([sys.executable, "-m", "qajev", *args, "--headless", "--ephemeral", "--profile",
                            f"gpu-{os.getpid()}", "--out", str(tmp_path / "out"), "--json", "--quiet",
                            "--load-high", "0"], capture_output=True, text=True, timeout=300, env=env)
        gpu = json.loads(p.stdout)["browser"].get("gpu")
        assert isinstance(gpu, dict) and (gpu.get("device") or gpu.get("none")), (args[0], gpu, p.stderr[-500:])
        if _SOFTWARE_GPU.search(gpu.get("device") or ""):  # a CI or test box draws in software: it says so
            assert gpu.get("software"), (args[0], gpu)
