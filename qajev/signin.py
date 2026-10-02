"""Signing in with a stored test account, once, before the scenarios.

QAJev itself does it, in its own tab, before any guard is armed (a sign-in form posts, which the read-only guard
would block): it opens the sign-in page, types the email, reads the password from its vault reference and types it
into the password field, submits, and checks that the site let it in. Then that tab closes and the scenarios run as
usual, guarded, in the same browser, so they start signed in. Jev is never involved: the password is not in any
prompt, step, report or log, and the guard still disables password fields for Jev.

Rails: the sign-in page is https (or localhost) and a host the suite allows; the password goes only into an
<input type=password> on such a page; a failed sign-in stops the run with a reason instead of letting Jev try.
"""

import json
import time
from urllib.parse import urlsplit

from . import vault
from .config import redact

# In order of preference: the first selector with a visible match wins (a page-order search would pick a header's
# search box before the sign-in form's text field).
EMAIL_FIELDS = ["input[type=email]", "input[autocomplete=username]", "input[autocomplete=email]",
                "input[name*=email i]", "input[id*=email i]", "input[name*=user i]", "input[id*=user i]",
                "input[name=login i]", "form:has(input[type=password]) input[type=text]", "input[type=text]"]
PASSWORD_FIELD = "input[type=password]"
ERRORS = "[role=alert], [aria-live=assertive], .error, .alert, [class*=error i], [data-error]"

# Marks the first visible match (of the first selector that has one), so find(), which takes one selector, can use it.
PICK_JS = """((sels, tag) => {
  const seen = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled; };
  document.querySelectorAll('[data-qajev-signin="' + tag + '"]').forEach((e) => e.removeAttribute('data-qajev-signin'));
  for (const sel of Array.isArray(sels) ? sels : [sels]) {
    const el = [...document.querySelectorAll(sel)].find(seen);
    if (el) { el.setAttribute('data-qajev-signin', tag); return true; }
  }
  return false;
})(%s, %s)"""
STATE_JS = """(() => {
  const pw = [...document.querySelectorAll('input[type=password]')].some((e) => {
    const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  const errs = [...document.querySelectorAll(%s)].map((e) => e.innerText.trim()).filter(Boolean);
  return { href: location.href, path: location.pathname, pw, error: (errs[0] || '').slice(0, 160),
           text: (document.body ? document.body.innerText : '').slice(0, 20000) };
})()""" % json.dumps(ERRORS)


class SignInFailed(RuntimeError):
    """The site did not let the account in, or the account could not be used. Never carries the password."""


def _pick(session, selectors, tag):
    if not session.evaluate(PICK_JS % (json.dumps(selectors), json.dumps(tag))):
        return None
    return session.find(f'[data-qajev-signin="{tag}"]')


def _state(session):
    try:
        return session.evaluate(STATE_JS) or {}
    except RuntimeError:  # navigating: no page to read for a moment
        return {}


def _signed_in(session, login, state):
    spec = login.get("signed_in") or {}
    if not state:
        return False
    if spec.get("js"):
        try:
            return bool(session.evaluate(f"(async () => !!({spec['js']}))()"))
        except RuntimeError:
            return False
    if not spec:  # by default: away from the sign-in page, and no password field asking again
        return state.get("path") != urlsplit(login["url"]).path and not state.get("pw")
    ok = True
    if spec.get("url_not"):
        ok = ok and spec["url_not"] not in state.get("href", "")
    if spec.get("text"):
        texts = spec["text"] if isinstance(spec["text"], list) else [spec["text"]]
        ok = ok and all(t in state.get("text", "") for t in texts)
    return ok


def _wait(session, until, timeout):
    deadline = time.monotonic() + timeout
    while True:
        state = _state(session)
        if until(state):
            return state
        if time.monotonic() > deadline:
            return None
        time.sleep(0.25)


def sign_in(session, account, *, timeout=20.0):
    """Sign `account` (a suite's account block) in, in `session`'s tab. -> what happened (no secrets)."""
    login = account["login"]
    started = time.monotonic()
    out = {"account": account["name"], "ok": False}
    error = session.navigate(login["url"])
    if error:
        raise SignInFailed(f"could not open the sign-in page {login['url']}: {error}")
    first = _wait(session, lambda s: bool(s), 5) or {}
    if not first.get("pw") and _signed_in(session, login, first):  # the site sent a signed-in visitor on already
        return {**out, "ok": True, "already": True, "landed": first.get("href", "").split("?")[0],
                "seconds": round(time.monotonic() - started, 1)}

    email = vault.resolve(account["email"]) if vault.is_ref(account["email"]) else account["email"]
    out["email"] = email
    field = _pick(session, login.get("email_field") or EMAIL_FIELDS, "email")
    if not field:
        raise SignInFailed(f"no email or username field on {login['url']}; set account.login.email_field")
    session.type_into(field, email)

    password_field = login.get("password_field") or PASSWORD_FIELD
    if not _pick(session, password_field, "password"):  # two-step sign-in: the email first, then the password
        nxt = _pick(session, login["next"], "next") if login.get("next") else None
        if nxt:
            session.trusted_click(nxt["x"], nxt["y"])
        else:
            session.press("Enter")
        if not _wait(session, lambda s: s.get("pw"), 10):
            raise SignInFailed(f"no password field appeared after the email on {login['url']}"
                               + (f" (the page says: {_state(session).get('error')})" if _state(session).get("error")
                                  else "; set account.login.next or password_field"))
    field = _pick(session, password_field, "password")
    if not field or not field.get("secret"):
        raise SignInFailed("refused: the password goes only into a password field, and none was found "
                           f"({password_field!r})")
    session.check_host(_state(session).get("href", login["url"]))  # still on a host the suite allows
    session.type_into(field, vault.resolve(account["password"]))

    button = _pick(session, login["submit"], "submit") if login.get("submit") else None
    if login.get("submit") and not button:
        raise SignInFailed(f"no {login['submit']!r} to submit with on {login['url']}")
    if button:
        session.trusted_click(button["x"], button["y"])
    else:
        session.press("Enter")
    submitted = time.monotonic()
    # Done when signed in; given up early when the page says why not and still asks for the password.
    state = _wait(session, lambda s: _signed_in(session, login, s)
                  or (s.get("error") and s.get("pw") and time.monotonic() - submitted > 3), timeout)
    if state and not _signed_in(session, login, state):
        state = None
    if not state:
        last = _state(session)
        says = f"; the page says: {last['error']}" if last.get("error") else ""
        raise SignInFailed(redact(f"not signed in after {timeout:.0f} s: still on {last.get('path', '?')}{says}"))
    return {**out, "ok": True, "landed": state.get("href", "").split("?")[0],
            "seconds": round(time.monotonic() - started, 1)}
