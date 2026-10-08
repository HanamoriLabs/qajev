"""A hidden page draws no frames (8 Oct: a Verse 3D page stayed on "Preparing shaders…" in a minimised window, 0 frames
a second; 182 with the window open). QAJev never minimises its window, and a check on a page the browser reports
hidden is QAJev's problem (harness), never the product's pass or fail."""

import pytest

from qajev import runner, session, suite, verdict


@pytest.mark.parametrize("outcome", ["pass", "fail", "stuck"])
def test_a_verdict_on_a_hidden_page_is_harness(outcome):
    got, why = verdict.hidden_page(outcome, "all 1 check(s) passed", {"hidden": True})
    assert got == "harness" and why == f"{verdict.HIDDEN_PAGE}; all 1 check(s) passed"


@pytest.mark.parametrize("outcome, observed", [("pass", {"hidden": False}), ("fail", {}), ("harness", {"hidden": True}),
                                               ("skipped", {"hidden": True}), ("pass", None)])
def test_a_visible_page_or_a_verdict_already_not_the_products_is_kept(outcome, observed):
    assert verdict.hidden_page(outcome, "why", observed) == (outcome, "why")


class Page:
    """A session whose page the browser reports hidden or not."""

    def __init__(self, hidden):
        self.hidden = hidden

    def set_device(self, device): pass
    def arm(self, mode, speech=None): pass
    def check_host(self, url): pass
    def navigate(self, url): self.url = url
    def why_failed(self, url): return None
    def screenshot(self, path): return None

    def probe(self, expect):
        return {"url": self.url, "status": 200, "text": [True], "probe": {}, "hidden": self.hidden}


@pytest.mark.parametrize("hidden, outcome", [(True, "harness"), (False, "pass")])
def test_a_run_on_a_hidden_page_says_so(monkeypatch, tmp_path, hidden, outcome):
    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 1.0))
    s = suite.parse({"scenarios": [{"name": "plaza", "url": "http://127.0.0.1:8765/", "expect": {"text": "Plaza"}}]})
    r = runner.run_scenario(Page(hidden), s.scenarios[0], opts=runner.Options(), hosts={"127.0.0.1"}, run_dir=tmp_path)
    assert r["outcome"] == outcome
    assert (verdict.HIDDEN_PAGE in r["reason"]) is hidden


def test_qajev_never_minimises_its_window():
    # Minimised, the window draws nothing. On a Mac it stays off the person's screen by starting hidden instead.
    assert not hasattr(session.Session, "minimize")
    assert "hidden: document.hidden" in session.PROBE_JS.template  # every probe says whether the page was hidden
