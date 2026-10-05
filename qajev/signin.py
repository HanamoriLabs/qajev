"""Signing in with a stored test account, once, before the scenarios.

QAJev itself does it, in its own tab, before any guard is armed (a sign-in form posts, which the read-only guard
would block): it opens the sign-in page, types the email, reads the password from its vault reference and types it
into the password field, submits, and checks that the site let it in. Then that tab closes and the scenarios run as
usual, guarded, in the same browser, so they start signed in. Jev is never involved: the password is not in any
prompt, step, report or log, and the guard still disables password fields for Jev.

Rails: the sign-in page is https (or localhost) and a host the suite allows; the password goes only into an
<input type=password> on such a page; a failed sign-in stops the run with a reason instead of letting Jev try.

A seeded TEST account on a local dev host can also sign in without a person (Dash3 and Dash4, 6 Oct): a one-time
code computed from its seeded TOTP secret (`totp:`), or a session cookie its seed minted (`cookie:`). Both only for an
address at a reserved test domain (example.test, *.test, example.com...) on a local dev host (localhost, 127.0.0.1,
*.test, *.localhost); both secrets are vault references, never written anywhere. The report says only that it
happened ("TOTP from seed: yes").
"""

import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlsplit

from . import vault
from .config import is_local_dev, is_test_email, redact

# In order of preference: the first selector with a visible match wins (a page-order search would pick a header's
# search box before the sign-in form's text field).
EMAIL_FIELDS = ["input[type=email]", "input[autocomplete=username]", "input[autocomplete=email]",
                "input[name*=email i]", "input[id*=email i]", "input[name*=user i]", "input[id*=user i]",
                "input[name=login i]", "form:has(input[type=password]) input[type=text]", "input[type=text]"]
PASSWORD_FIELD = "input[type=password]"
# A one-time code field, as sign-in pages mark it: the standard autocomplete first, then common names.
CODE_FIELDS = ["input[autocomplete=one-time-code]", "input[name*=otp i]", "input[id*=otp i]", "input[name*=totp i]",
               "input[id*=totp i]", "input[name*=code i]", "input[id*=code i]", "input[placeholder='000000']"]
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
  const otp = [...document.querySelectorAll(%s)].some((e) => {
    const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && e.type !== 'password'; });
  const errs = [...document.querySelectorAll(%s)].map((e) => e.innerText.trim()).filter(Boolean);
  return { href: location.href, path: location.pathname, pw, otp, error: (errs[0] || '').slice(0, 160),
           text: (document.body ? document.body.innerText : '').slice(0, 20000) };
})()""" % (json.dumps(", ".join(CODE_FIELDS)), json.dumps(ERRORS))


class SignInFailed(RuntimeError):
    """The site did not let the account in, or the account could not be used. Never carries the password."""


def totp(secret, at=None, *, digits=6, step=30):
    """The RFC 6238 code (HMAC-SHA1) for a base32 secret, at `at` (default: now)."""
    raw = re.sub(r"[\s-]", "", secret).upper()
    key = base64.b32decode(raw + "=" * (-len(raw) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    return str((int.from_bytes(mac[offset:offset + 4], "big") & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def _test_only(account, url, email, what):
    """A one-time code or a preset cookie: only a seeded test account, only on a local dev host, and only on the
    hosts its seed fixture allows (seed:FILE#KEY references: the fixture's "allowed_hosts")."""
    host = urlsplit(url).hostname or url
    if not is_local_dev(url):
        raise SignInFailed(f"refused: {what} is for a seeded test account on a local dev host (localhost, 127.0.0.1, "
                           f"*.test), not {host}")
    if not is_test_email(email):
        raise SignInFailed(f"refused: {what} is for a seeded test account at a reserved test domain (example.test, "
                           f"*.test, example.com...), not {email}")
    cookie = account.get("cookie") or {}
    refs = [account.get("email"), account.get("password"), account.get("totp"), cookie.get("value")]
    for file in {vault.parse(r)[1] for r in refs if isinstance(r, str) and r.startswith("seed:")}:
        allowed = vault.seed(file).get("allowed_hosts")
        if allowed is not None and host.strip("[]") not in {str(h).strip("[]") for h in allowed}:
            raise SignInFailed(f"refused: the seed fixture allows {allowed}, not {host}")
    return host


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
            return session.js_holds(spec["js"])
        except RuntimeError:
            return False
    if not spec:  # by default: away from the sign-in page, and no password or code field asking again
        return state.get("path") != urlsplit(login["url"]).path and not state.get("pw") and not state.get("otp")
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
    if account.get("cookie"):
        return _with_cookie(session, account, out, started, timeout)
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
    # Done when signed in, or when the page asks for a one-time code; given up early when the page says why not and
    # still asks for the password.
    state = _wait(session, lambda s: _signed_in(session, login, s) or s.get("otp")
                  or (s.get("error") and s.get("pw") and time.monotonic() - submitted > 3), timeout)
    if state and state.get("otp") and not _signed_in(session, login, state):
        state = _code(session, account, email, state, timeout)
        out["test_signin"] = {"host": urlsplit(state.get("href") or login["url"]).hostname, "totp": True}
    if state and not _signed_in(session, login, state):
        state = None
    if not state:
        last = _state(session)
        says = f"; the page says: {last['error']}" if last.get("error") else ""
        raise SignInFailed(redact(f"not signed in after {timeout:.0f} s: still on {last.get('path', '?')}{says}"))
    return {**out, "ok": True, "landed": state.get("href", "").split("?")[0],
            "seconds": round(time.monotonic() - started, 1)}


def _code(session, account, email, state, timeout):
    """The page asks for a one-time code: the seeded test account's TOTP code, typed into the code field."""
    login = account["login"]
    if not account.get("totp"):
        raise SignInFailed("the site asks for a one-time code: a seeded test account on a local dev host can give "
                           "its TOTP secret as account.totp (a vault reference); any other account needs a person")
    _test_only(account, state.get("href") or login["url"], email, "a one-time code")
    field = _pick(session, login.get("code_field") or CODE_FIELDS, "code")
    if not field:
        raise SignInFailed("no one-time code field found; set account.login.code_field")
    secret = vault.resolve(account["totp"])
    left = 30 - time.time() % 30
    if left < 3:  # a code about to expire would arrive stale: wait for the next one
        time.sleep(left + 0.2)
    session.check_host(state.get("href") or login["url"])
    session.type_into(field, totp(secret))
    button = _pick(session, login["code_submit"], "code-submit") if login.get("code_submit") else None
    if button:
        session.trusted_click(button["x"], button["y"])
    else:
        session.press("Enter")
    sent = time.monotonic()
    done = _wait(session, lambda s: _signed_in(session, login, s)
                 or (s.get("error") and s.get("otp") and time.monotonic() - sent > 3), timeout)
    if not done or not _signed_in(session, login, done):
        last = _state(session)
        says = f"; the page says: {last['error']}" if last.get("error") else ""
        raise SignInFailed(redact(f"the one-time code did not sign in: still on {last.get('path', '?')}{says}"))
    return done


def _with_cookie(session, account, out, started, timeout):
    """A seeded test account's session cookie, set before the sign-in page loads; that page must then be signed in."""
    login, cookie = account["login"], account["cookie"]
    email = vault.resolve(account["email"]) if vault.is_ref(account["email"]) else account["email"]
    host = _test_only(account, login["url"], email, "a preset session cookie")
    parts = urlsplit(login["url"])
    session.call("Network.setCookie", name=cookie["name"], value=vault.resolve(cookie["value"]),
                 url=f"{parts.scheme}://{parts.netloc}/", path=cookie.get("path") or "/",
                 httpOnly=bool(cookie.get("http_only", True)), secure=parts.scheme == "https")
    error = session.navigate(login["url"])
    if error:
        raise SignInFailed(f"could not open {login['url']}: {error}")
    state = _wait(session, lambda s: _signed_in(session, login, s), min(timeout, 10))
    if not state:
        last = _state(session)
        raise SignInFailed(redact(f"the session cookie did not sign in: still on {last.get('path', '?')} "
                                  "(set account.login.signed_in to say what a signed-in page shows)"))
    return {**out, "ok": True, "email": email, "landed": state.get("href", "").split("?")[0],
            "test_signin": {"host": host, "cookie": True}, "seconds": round(time.monotonic() - started, 1)}
