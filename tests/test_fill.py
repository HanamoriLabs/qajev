"""A fill hook fills the field or says it could not (dash-wholesale-links, 12 Oct: fill "2027-06-30" on a date field
left 06/30/2025 and said nothing; the next wait_for timed out instead, hiding the cause)."""

import pytest

from qajev import session as session_mod
from qajev.session import HookFailed, Session


class Page(Session):
    """One field, `kind`, holding `value`. Typed text reaches it only when `types` (a date field: Chrome ignores it)."""

    def __init__(self, kind="text", value="", types=True, secret=False, coerce=None):
        self.guard_cfg, self.agent = None, None  # no guard and no Jev: the hook alone
        self.kind, self.value, self.types, self.secret = kind, value, types, secret
        self.coerce, self.typed = coerce or (lambda v: v), []

    def find(self, selector):
        return {"x": 1, "y": 1, "secret": self.secret, "covered": False, "tag": "INPUT"}

    def evaluate(self, js, timeout_ms=15000):
        if js.startswith("(() => { const el = document.querySelector("):  # VALUE_JS
            return self.value
        if "PICKER" not in js and '"date"' in js:  # SET_PICKER_JS
            if self.kind not in session_mod.PICKER_TYPES:
                return None
            self.value = self.coerce(js.split(".call(el, ", 1)[1].split(");", 1)[0].strip('"'))
            return self.value
        raise AssertionError(js[:80])

    def type_into(self, found, text):
        self.typed.append(text)
        if self.types:
            self.value = text


def test_a_date_field_gets_its_value_set_not_typed():
    page = Page("date", "2025-06-30", types=False)
    page.run_hook({"fill": {"[data-qa=termination]": "2027-06-30"}}, "http://localhost:4011/")
    assert page.value == "2027-06-30" and page.typed == []


def test_a_date_in_another_form_fails_saying_which_form_it_takes():
    page = Page("date", "2025-06-30", coerce=lambda v: "")  # Chrome empties a date field given 06/30/2027
    with pytest.raises(HookFailed, match="took '', not '06/30/2027'.*2027-06-30"):
        page.run_hook({"fill": {"#d": "06/30/2027"}}, "http://localhost:4011/")


def test_typing_that_never_reaches_the_field_fails_instead_of_passing_quietly():
    page = Page("text", "old", types=False)
    with pytest.raises(HookFailed, match="still holds 'old'; the typing did not reach it"):
        page.run_hook({"fill": {"#name": "new"}}, "http://localhost:4011/")
    secret = Page("text", "old-pass", types=False, secret=True)
    with pytest.raises(HookFailed, match="still holds what it held before") as e:
        secret.run_hook({"fill": {"#pw": "new-pass"}}, "http://localhost:4011/")
    assert "old-pass" not in str(e.value)


def test_a_field_that_reformats_what_was_typed_is_fine():
    page = Page("text", "")
    page.types = False
    page.type_into = lambda found, text: setattr(page, "value", "(555) 1234")  # a phone mask
    page.run_hook({"fill": {"#phone": "5551234"}}, "http://localhost:4011/")
    assert page.value == "(555) 1234"
