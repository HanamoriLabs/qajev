"""Signing in with a stored test account, once, before the scenarios.

QAJev itself does it, in its own tab, before any guard is armed (a sign-in form posts, which the read-only guard
would block): it opens the sign-in page, types the email, reads the password from its vault reference and types it
into the password field, submits, and checks that the site let it in. Then that tab closes and the scenarios run as
usual, guarded, in the same browser, so they start signed in. Jev is never involved: the password is not in any
prompt, step, report or log, and the guard still disables password fields for Jev.

Rails: the sign-in page is https (or localhost) and a host the suite allows; the password goes only into an
<input type=password> on such a page; a failed sign-in stops the run with a reason instead of letting Jev try. And
(José, 6 Oct) a password only for a seeded test account on a local dev host, from seed:FILE#KEY, as a code or a
cookie below; any other account is signed in once by a person (`qajev browser login`), and the profile keeps it.

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
from .config import is_local_dev, is_test_email, plain_host, redact, remember_secret

# In order of preference: the first selector with a visible match wins (a page-order search would pick a header's
# search box before the sign-in form's text field).
EMAIL_FIELDS = ["input[type=email]", "input[autocomplete=username]", "input[autocomplete=email]",
                "input[name*=email i]", "input[id*=email i]", "input[name*=user i]", "input[id*=user i]",
                "input[name=login i]", "form:has(input[type=password]) input[type=text]", "input[type=text]"]
PASSWORD_FIELD = "input[type=password]"
# A one-time code field, as sign-in pages mark it: the standard autocomplete first, then exact names only (the
# review of #47: "*=code" matched postcode and coupon_code, and stopped a password-only sign-in at a code step).
_CODE_NAMES = ["otp", "totp", "mfa", "2fa", "code", "otp_code", "totp_code", "mfa_code", "one_time_code",
               "verification_code", "auth_code", "security_code"]
CODE_FIELDS = ["input[autocomplete=one-time-code]", "input[placeholder='000000']",
               *[f"input[{attr}={name!r} i]" for attr in ("name", "id") for name in _CODE_NAMES]]
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


# José, 6 Oct: QAJev types passwords for local test accounts only. Every other account: a person signs in once.
BROWSER_LOGIN = ("QAJev types a password only for a seeded test account on a local dev host (password: seed:FILE#KEY, "
                 "from a fixture that says \"test_account\": true and lists the host in allowed_hosts). For any other "
                 "account, sign in once yourself in QAJev's window: qajev browser login --url {url} (QAJev keeps that "
                 "session).")


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
    """A password, a one-time code or a preset cookie: only a seeded test account on a local dev host, its secret only
    from its seed fixture (seed:FILE#KEY: "test_account": true), and only on a host that fixture's non-empty
    "allowed_hosts" names. `url` is where the browser is, read as a browser reads it (the review of #47).
    -> the host."""
    host = plain_host(url)
    if not host or not is_local_dev(url):
        raise SignInFailed(f"refused: {what} is for a seeded test account on a local dev host (localhost, 127.0.0.1, "
                           f"*.test), not {host or 'an address a browser could read as another host'}")
    if not is_test_email(email):
        raise SignInFailed(f"refused: {what} is for a seeded test account at a reserved test domain (example.test, "
                           f"*.test, example.com...), not {email}")
    secrets = [account.get("password"), account.get("totp"), (account.get("cookie") or {}).get("value")]
    for ref in [r for r in secrets if r is not None]:
        if not (isinstance(ref, str) and ref.startswith("seed:")):
            raise SignInFailed(f"refused: {what} comes only from the app's seed fixture (seed:FILE#KEY)")
        allowed = vault.seed(vault.parse(ref)[1]).get("allowed_hosts")
        if not isinstance(allowed, list) or not allowed:
            raise SignInFailed(f"refused: the seed fixture must list its allowed_hosts for {what}")
        if host.strip("[]") not in {str(h).lower().strip("[]") for h in allowed}:
            raise SignInFailed(f"refused: the seed fixture allows {allowed}, not {host}")
    return host


def _password_ok(account, url, email):
    """The password's rails, checked where the browser is; a refusal says how a person signs in once instead."""
    try:
        return _test_only(account, url, email, "a password")
    except SignInFailed as e:
        raise SignInFailed(f"{e}. {BROWSER_LOGIN.format(url=account['login']['url'])}") from None


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
    if not vault.is_ref(account["email"]):  # an email kept by reference stays out of events, logs and reports
        out["email"] = email
    _password_ok(account, first.get("href") or "", email)  # before a key is pressed; an unread page is no host
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
    # Where the browser is, read from the page itself: never the configured URL (#57 review: a page whose state could
    # not be read fell back to it, and the password went to whatever host the browser was on). A navigation still
    # settling gets 2 s; a page that stays unreadable is no host, and nothing is typed.
    here = (_wait(session, lambda s: s.get("href"), 2) or {}).get("href") or ""
    _password_ok(account, here, email)  # a local dev host its seed fixture allows
    session.check_host(here)  # and a host the suite allows
    password = vault.resolve(account["password"])
    remember_secret(password)  # never in an error, a log or a report
    session.type_into(field, password)

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
        out["test_signin"] = {"host": plain_host(state.get("href") or login["url"]), "totp": True}
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
    try:
        code = totp(secret)
    except ValueError:  # binascii.Error: the seed's secret is not base32
        raise SignInFailed("the seed's TOTP secret is not base32 (RFC 4648); check the fixture") from None
    remember_secret(code)  # redacted from everything QAJev writes, like the secret itself
    here = _state(session).get("href") or ""  # where the browser is now, just before the code is typed
    _test_only(account, here, email, "a one-time code")
    session.check_host(here)
    session.type_into(field, code)
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
    port = f":{parts.port}" if parts.port else ""
    # A host-only cookie for the validated host (no userinfo, no backslash: plain_host checked the address)
    session.call("Network.setCookie", name=cookie["name"], value=vault.resolve(cookie["value"]),
                 url=f"{parts.scheme}://{host if ':' not in host else f'[{host}]'}{port}/",
                 path=cookie.get("path") or "/", httpOnly=bool(cookie.get("http_only", True)),
                 secure=parts.scheme == "https")
    error = session.navigate(login["url"])
    if error:
        raise SignInFailed(f"could not open {login['url']}: {error}")
    state = _wait(session, lambda s: _signed_in(session, login, s), min(timeout, 10))
    if state and plain_host(state.get("href", "")) != host:  # the page must still be the host the cookie is for
        raise SignInFailed(f"refused: the sign-in page went to {plain_host(state.get('href', '')) or '?'}, not {host}")
    if not state:
        last = _state(session)
        raise SignInFailed(redact(f"the session cookie did not sign in: still on {last.get('path', '?')} "
                                  "(set account.login.signed_in to say what a signed-in page shows)"))
    shown = {} if vault.is_ref(account["email"]) else {"email": email}  # a referenced email stays out of events
    return {**out, "ok": True, **shown, "landed": state.get("href", "").split("?")[0],
            "test_signin": {"host": host, "cookie": True}, "seconds": round(time.monotonic() - started, 1)}
