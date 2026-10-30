"""A page's own dialog is answered, not waited on (#802, 30 Oct: a form's leave-page guard froze the page, and every
later call to it waited a full CDP timeout: 5-6.5 min a step)."""

from types import SimpleNamespace

import pytest

from qajev import session


class Page:
    """A CDP link to a page whose dialog freezes its script: every call times out until the dialog is answered."""

    def __init__(self, dialog):
        self.dialog = dialog
        self.calls = []

    def cdp(self, method, session_id=None, _response_timeout=None, **params):
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


@pytest.mark.parametrize("kind,accept,said", [("beforeunload", True, "leave"), ("alert", True, "ok"),
                                              ("confirm", False, "no"), ("prompt", False, "no")])
def test_a_timed_out_call_answers_the_open_dialog_and_the_retry_goes_through(kind, accept, said):
    page = Page({"type": kind, "message": "You have unsaved changes"})
    call, transport = link(page)
    assert call("Runtime.evaluate", session_id="s1", expression="document.readyState") == {
        "result": {"value": "complete"}}
    answered = [c for c in page.calls if c[0] == "Page.handleJavaScriptDialog"]
    assert answered == [("Page.handleJavaScriptDialog", "s1", {"accept": accept})]
    assert transport.dialogs == [{"kind": kind, "message": "You have unsaved changes", "answered": said,
                                  "native": True}]


def test_a_navigation_held_by_a_leave_page_dialog_goes_on_and_is_not_sent_twice():
    # Sent again, the second navigation aborts the first (the live test saw net::ERR_ABORTED)
    page = Page({"type": "beforeunload", "message": ""})
    history = {"currentIndex": 0, "entries": [{"url": "http://127.0.0.1:8000/form.html"}]}

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
    page = Page({"type": "beforeunload", "message": ""})

    def broken():
        raise OSError("daemon socket gone")

    call, transport = link(page, broken)
    with pytest.raises(TimeoutError):
        call("Runtime.evaluate", session_id="s1", expression="1")
    assert transport.dialogs == []
    assert [c[0] for c in page.calls] == ["Runtime.evaluate", "Runtime.evaluate"]


def test_answered_dialogs_are_taken_once_for_the_next_probe(monkeypatch):
    transport = SimpleNamespace(error=None, dialogs=[{"kind": "beforeunload", "message": "", "answered": "leave",
                                                      "native": True}])
    monkeypatch.setattr(session, "_jev", SimpleNamespace(transport=transport))
    assert [d["kind"] for d in session.take_answered_dialogs()] == ["beforeunload"]
    assert session.take_answered_dialogs() == []
