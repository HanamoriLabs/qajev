"""A page's own dialog is answered, not waited on (#802, 30 Oct: a form's leave-page guard froze the page, and every
later call to it waited a full CDP timeout: 5-6.5 min a step)."""

from types import SimpleNamespace

import pytest

from qajev import session

FORM = "http://127.0.0.1:8000/form.html"


class Page:
    """A CDP link to a page whose dialog freezes its script: every call times out until the dialog is answered. The
    browser still says the tab's history (Page.getNavigationHistory) and its tabs (Target.getTargets), as Chrome does.
    `others` are the urls other tabs show."""

    def __init__(self, dialog, url=FORM, others=()):
        self.dialog = dialog
        self.url = url
        self.others = others
        self.calls = []

    def cdp(self, method, session_id=None, _response_timeout=None, **params):
        if method == "Page.getNavigationHistory":
            return {"currentIndex": 0, "entries": [{"url": self.url}]}
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "url": u} for u in (self.url, *self.others)]
                    + [{"type": "iframe", "url": "http://127.0.0.1:8000/frame.html"}]}
        self.calls.append((method, session_id, params))
        if method == "Page.handleJavaScriptDialog":
            self.dialog = None
            return {}
        if self.dialog is not None:
            raise TimeoutError("no answer in 30 s")
        return {"result": {"value": "complete"}}

    def pending(self):
        return self.dialog


def link(page, pending=None):
    transport = SimpleNamespace(error=None, dialogs=[])
    return session._patient_cdp(page.cdp, 30, transport, pending or page.pending), transport


@pytest.mark.parametrize("kind,accept,said", [("beforeunload", True, "leave"), ("alert", False, "closed"),
                                              ("confirm", False, "no"), ("prompt", False, "no")])
def test_a_timed_out_call_answers_the_open_dialog_and_the_retry_goes_through(kind, accept, said):
    page = Page({"type": kind, "message": "You have unsaved changes", "url": FORM})
    call, transport = link(page)
    assert call("Runtime.evaluate", session_id="s1", expression="document.readyState") == {
        "result": {"value": "complete"}}
    answered = [c for c in page.calls if c[0] == "Page.handleJavaScriptDialog"]
    assert answered == [("Page.handleJavaScriptDialog", "s1", {"accept": accept})]
    assert transport.dialogs == [{"kind": kind, "message": "You have unsaved changes", "answered": said,
                                  "native": True}]


@pytest.mark.parametrize("record", [{"type": "beforeunload", "message": "", "url": "http://127.0.0.1:8000/other.html"},
                                    {"type": "beforeunload", "message": ""}])
def test_another_tabs_leave_page_record_never_makes_this_tab_say_yes(record):
    # browser_harness keeps one dialog record for the whole daemon (redteam L2 on #202): the record is another tab's
    # beforeunload, or names no page, while this tab holds a confirm. The confirm must hear "no", as on production.
    page = Page(dict(record))
    call, transport = link(page)
    call("Runtime.evaluate", session_id="s1", expression="1")
    assert [c[2] for c in page.calls if c[0] == "Page.handleJavaScriptDialog"] == [{"accept": False}]
    assert transport.dialogs[0]["answered"] == "stay"


def test_on_production_even_this_tabs_own_leave_page_record_hears_no():
    # Two production tabs on one url share it, so the url cannot tell them apart: there, nothing is accepted
    shop = "https://shop.example.com/cart"
    page = Page({"type": "beforeunload", "message": "", "url": shop}, url=shop)
    call, transport = link(page)
    call("Runtime.evaluate", session_id="s1", expression="1")
    assert [c[2] for c in page.calls if c[0] == "Page.handleJavaScriptDialog"] == [{"accept": False}]
    assert transport.dialogs[0]["answered"] == "stay"


def test_a_key_held_by_a_dialog_is_not_pressed_again_after_the_answer():
    # The held keyDown runs once the dialog closes; sent again, its handler runs twice (redteam L1 on #202)
    page = Page({"type": "alert", "message": "Saved", "url": FORM})
    call, transport = link(page)
    assert call("Input.dispatchKeyEvent", session_id="s1", type="keyDown", key="Enter") == {}
    assert [c[0] for c in page.calls].count("Input.dispatchKeyEvent") == 1
    assert transport.dialogs[0]["answered"] == "closed"


def test_a_key_that_timed_out_on_another_tabs_dialog_is_sent_again():
    # The daemon's one record is another tab's alert: answering it did not free this tab, so the key never counted as
    # pressed here (redteam N2 on #203)
    page = Page({"type": "alert", "message": "Saved", "url": "http://127.0.0.1:8000/other.html"},
                others=["http://127.0.0.1:8000/other.html"])
    call, transport = link(page)
    assert call("Input.dispatchKeyEvent", session_id="s1", type="keyDown", key="Enter") == {
        "result": {"value": "complete"}}
    assert [c[0] for c in page.calls].count("Input.dispatchKeyEvent") == 2
    assert transport.dialogs[0]["answered"] == "closed"


@pytest.mark.parametrize("case", ["iframe", "no history"])
def test_a_key_whose_dialog_may_be_this_tabs_own_is_not_pressed_again(case):
    # redteam L1 on #206: the record's url is the dialog's frame's, so an iframe's alert on this tab is not the tab's
    # url; and a tab that does not say its url proves nothing. Only another tab showing the page proves "other".
    page = Page({"type": "alert", "message": "Saved", "url": "http://127.0.0.1:8000/frame.html"})

    def cdp(method, session_id=None, _response_timeout=None, **params):
        if case == "no history" and method == "Page.getNavigationHistory":
            raise TimeoutError("no answer in 3 s")
        return page.cdp(method, session_id, **params)

    transport = SimpleNamespace(error=None, dialogs=[])
    call = session._patient_cdp(cdp, 30, transport, page.pending)
    assert call("Input.dispatchKeyEvent", session_id="s1", type="keyDown", key="Enter") == {}
    assert [c[0] for c in page.calls].count("Input.dispatchKeyEvent") == 1
    assert [c[2] for c in page.calls if c[0] == "Page.handleJavaScriptDialog"] == [{"accept": False}]
    assert transport.dialogs[0]["answered"] == "closed"


def test_a_key_is_not_pressed_again_when_the_tab_list_cannot_be_read():
    # redteam L1 on #207: the record's url is another tab's, but with no tab list nothing proves it is not this tab's
    page = Page({"type": "alert", "message": "Saved", "url": "http://127.0.0.1:8000/other.html"},
                others=["http://127.0.0.1:8000/other.html"])

    def cdp(method, session_id=None, _response_timeout=None, **params):
        if method == "Target.getTargets":
            raise TimeoutError("no answer in 3 s")
        return page.cdp(method, session_id, **params)

    transport = SimpleNamespace(error=None, dialogs=[])
    call = session._patient_cdp(cdp, 30, transport, page.pending)
    assert call("Input.dispatchKeyEvent", session_id="s1", type="keyDown", key="Enter") == {}
    assert [c[0] for c in page.calls].count("Input.dispatchKeyEvent") == 1


def test_a_navigation_held_by_an_iframes_dialog_goes_on_and_is_not_sent_twice():
    # redteam N2 on #207: whose is "unknown" (the record's url is the iframe's), so the held navigation is waited for
    page = Page({"type": "alert", "message": "Saved", "url": "http://127.0.0.1:8000/frame.html"})
    history = {"currentIndex": 0, "entries": [{"url": FORM}]}

    def cdp(method, session_id=None, _response_timeout=None, **params):
        if method == "Page.getNavigationHistory":
            return history
        if method == "Page.handleJavaScriptDialog":
            history.update(currentIndex=1, entries=[*history["entries"], {"url": "http://127.0.0.1:8000/next.html"}])
        return page.cdp(method, session_id, **params)

    transport = SimpleNamespace(error=None, dialogs=[])
    call = session._patient_cdp(cdp, 30, transport, page.pending)
    assert call("Page.navigate", session_id="s1", url="http://127.0.0.1:8000/next.html") == {}
    assert [c[0] for c in page.calls].count("Page.navigate") == 1
    assert transport.dialogs[0]["answered"] == "closed"


def test_a_navigation_held_by_a_leave_page_dialog_goes_on_and_is_not_sent_twice():
    # Sent again, the second navigation aborts the first (the live test saw net::ERR_ABORTED)
    page = Page({"type": "beforeunload", "message": "", "url": FORM})
    history = {"currentIndex": 0, "entries": [{"url": FORM}]}

    def cdp(method, session_id=None, _response_timeout=None, **params):
        if method == "Page.getNavigationHistory":
            return history
        if method == "Page.handleJavaScriptDialog":  # the held navigation commits once the dialog is answered
            history.update(currentIndex=1, entries=[*history["entries"], {"url": "http://127.0.0.1:8000/next.html"}])
        return page.cdp(method, session_id, **params)

    transport = SimpleNamespace(error=None, dialogs=[])
    call = session._patient_cdp(cdp, 30, transport, page.pending)
    assert call("Page.navigate", session_id="s1", url="http://127.0.0.1:8000/next.html") == {}
    assert [c[0] for c in page.calls].count("Page.navigate") == 1
    assert transport.dialogs[0]["answered"] == "leave"


def test_with_no_dialog_open_a_timeout_still_retries_once_and_answers_nothing():
    page = Page(None)
    tries = []

    def slow(method, session_id=None, _response_timeout=None, **params):
        tries.append(method)
        if len(tries) == 1:
            raise TimeoutError("busy")
        return page.cdp(method, session_id, **params)

    transport = SimpleNamespace(error=None, dialogs=[])
    call = session._patient_cdp(slow, 30, transport, page.pending)
    assert call("Runtime.evaluate", session_id="s1", expression="1") == {"result": {"value": "complete"}}
    assert tries == ["Runtime.evaluate", "Runtime.evaluate"]
    assert transport.dialogs == []


def test_a_daemon_that_cannot_say_still_gets_the_old_retry():
    # It cannot say whether a dialog is open, and the retry times out too: nothing was answered, so the browser
    # stopped answering this test, as below. It is still a timeout.
    page = Page({"type": "beforeunload", "message": ""})

    def broken():
        raise OSError("daemon socket gone")

    call, transport = link(page, broken)
    with pytest.raises(TimeoutError):
        call("Runtime.evaluate", session_id="s1", expression="1")
    assert transport.dialogs == []
    assert [c[0] for c in page.calls] == ["Runtime.evaluate", "Runtime.evaluate"]


def test_a_browser_that_stops_answering_stalls_the_test_after_two_timeouts_and_costs_no_more(monkeypatch):
    # #802, 30 Oct: with no dialog open, every call timed out twice (30 s each) and the step went on, 300-390 s a step.
    monkeypatch.setattr(session, "_load1", iter([17.6, 18.2, 3.0, 3.1]).__next__)
    tries = []

    def hung(method, session_id=None, _response_timeout=None, **params):
        tries.append(method)
        raise TimeoutError("Runtime.evaluate timed out after 30s waiting for the daemon")

    transport = SimpleNamespace(error=None, dialogs=[], stalled=None)
    call = session._patient_cdp(hung, 30, transport, lambda: None)
    with pytest.raises(session.BrowserStalled) as stalled:
        call("Runtime.evaluate", session_id="s1", expression="1")
    assert isinstance(stalled.value, TimeoutError)  # callers that take a timeout still take it
    assert str(stalled.value) == ("the browser stopped answering: Runtime.evaluate timed out twice in a row "
                                  "(30 s each), with no page dialog to answer (load1 17.6, then 18.2)")
    assert transport.stalled is stalled.value and transport.error is None  # this test's, not the transport's
    assert tries == ["Runtime.evaluate", "Runtime.evaluate"]
    with pytest.raises(session.BrowserStalled):  # the next call fails at once: it waits for nothing
        call("Page.navigate", session_id="s1", url="http://127.0.0.1:8000/")
    assert tries == ["Runtime.evaluate", "Runtime.evaluate"]
    monkeypatch.setattr(session, "_jev", SimpleNamespace(transport=transport))
    session.clear_stall()  # the next test tries the browser again
    with pytest.raises(session.BrowserStalled):
        call("Runtime.evaluate", session_id="s1", expression="1")
    assert tries == ["Runtime.evaluate"] * 4


def test_a_page_whose_dialog_was_answered_is_not_lost_when_the_retry_times_out():
    # The page answered once its dialog closed, or it will: only a timeout with nothing to answer means it is gone.
    page = Page({"type": "alert", "message": "Saved", "url": FORM})

    def still_slow(method, session_id=None, _response_timeout=None, **params):
        if method in ("Page.getNavigationHistory", "Target.getTargets", "Page.handleJavaScriptDialog"):
            return page.cdp(method, session_id, **params)
        raise TimeoutError("busy")

    transport = SimpleNamespace(error=None, dialogs=[], stalled=None)
    call = session._patient_cdp(still_slow, 30, transport, page.pending)
    with pytest.raises(TimeoutError) as slow:
        call("Runtime.evaluate", session_id="s1", expression="1")
    assert not isinstance(slow.value, session.BrowserStalled) and transport.stalled is None
    assert transport.dialogs[0]["answered"] == "closed"


def test_answered_dialogs_are_taken_once_for_the_next_probe(monkeypatch):
    transport = SimpleNamespace(error=None, dialogs=[{"kind": "beforeunload", "message": "", "answered": "leave",
                                                      "native": True}])
    monkeypatch.setattr(session, "_jev", SimpleNamespace(transport=transport))
    assert [d["kind"] for d in session.take_answered_dialogs()] == ["beforeunload"]
    assert session.take_answered_dialogs() == []
