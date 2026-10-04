"""`qajev smoke`: crawl a site's same-origin pages with no model calls and lint what a browser can see.

Free and fast; a good first pass before spending Jev decisions on goals. The page is still guarded
(read-only, deaf), and links that look like they change state are never followed.
"""

import re
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from . import chrome, verdict
from .config import is_loopback, redact_tree, secret_values
from .ledger import Ledger
from .runner import ConfigError, slug, wait_for_quiet

FACTS = (Path(__file__).parent / "js" / "page_facts.js").read_text()
SKIP = re.compile(
    r"log.?out|sign.?out|log.?off|delete|remove|destroy|unsubscribe|cancel|revoke|reset|disconnect|checkout|"
    r"/api/|\.(pdf|zip|dmg|exe|pkg|msi|png|jpe?g|gif|svg|webp|mp4|webm|mp3|csv|xlsx?)(\?|$)",
    re.I,
)


def lint(facts, *, mobile, name):
    out = []
    url = facts.get("url")

    def add(severity, kind, detail=""):
        out.append({"severity": severity, "kind": kind, "detail": str(detail), "scenario": name, "url": url})

    status = facts.get("status") or 0
    if status >= 400:
        add("S1" if status >= 500 else "S2", f"HTTP {status}", "document response")
    if facts.get("text_chars", 0) < 30:
        add("S2", "blank page", f"{facts.get('text_chars', 0)} characters of text")
    for src in facts.get("broken_images", []):
        add("S2", "broken image", src)
    if facts.get("unlabelled_fields"):
        add("S2", "form fields without a label", f"{facts['unlabelled_fields']} field(s)")
    if facts.get("unnamed_buttons"):
        add("S2", "buttons without an accessible name", f"{facts['unnamed_buttons']} button(s)")
    for src in facts.get("mixed_content", []):
        add("S2", "mixed content", src)
    if facts.get("horizontal_overflow"):
        add("S2" if mobile else "S3", "horizontal scroll", "page is wider than the viewport")
    if not facts.get("title"):
        add("S3", "missing <title>")
    if not facts.get("lang"):
        add("S3", "missing <html lang>")
    if facts.get("h1", 0) == 0:
        add("S3", "no <h1>")
    elif facts.get("h1", 0) > 1:
        add("S3", "several <h1>", f"{facts['h1']}")
    if not facts.get("viewport_meta"):
        add("S3", "missing viewport meta")
    if not facts.get("description"):
        add("S3", "missing meta description")
    if facts.get("images_without_alt"):
        srcs = facts["images_without_alt"]
        add("S3", "images without alt", f"{len(srcs)} visible image(s): " + ", ".join(srcs[:3]))
    if facts.get("duplicate_ids"):
        add("S3", "duplicate ids", ", ".join(facts["duplicate_ids"][:5]))
    if mobile and facts.get("small_targets"):
        add("S3", "tap targets under 24 px", f"{facts['small_targets']} target(s)")
    if (facts.get("lcp") or 0) > 4000:
        add("S3", "slow largest contentful paint", f"{facts['lcp']} ms")
    if (facts.get("cls") or 0) > 0.25:
        add("S3", "layout shift", f"CLS {facts['cls']}")
    return out


USER_AGENT = "QAJev"


def canon(url):
    """One spelling per page: no fragment, and an empty path is "/"."""
    parts = urlsplit(url)
    return parts._replace(path=parts.path or "/", fragment="").geturl()


def page_name(url):
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


class _KeepHead(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.method = req.get_method()  # urllib would turn a redirected HEAD into a GET
        return new


_opener = urllib.request.build_opener(_KeepHead())


def http_status(url, timeout=15):
    """A link's answer over plain HTTP (not the page's fetch, which CORS blinds to cross-origin redirects).
    -> (status | error text, final URL, content type). A GET fallback asks for one byte and reads none: a full GET
    of an installer, dropped after its headers, is a client hang-up on the server's side (and its egress)."""
    for method in ("HEAD", "GET"):
        headers = {"User-Agent": USER_AGENT, **({"Range": "bytes=0-0"} if method == "GET" else {})}
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with _opener.open(request, timeout=timeout) as r:
                return r.status, r.geturl(), r.headers.get("content-type", "")
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 405, 501):
                continue  # some servers refuse HEAD; ask again with GET
            return e.code, e.geturl() or url, e.headers.get("content-type", "") if e.headers else ""
        except (urllib.error.URLError, OSError, ValueError) as e:
            return f"{type(e).__name__}: {getattr(e, 'reason', e)}", url, ""
    return "no answer", url, ""


DOWNLOAD = re.compile(r"(/downloads?\b|\.(dmg|pkg|exe|msi|msix|zip|7z|rar|gz|tgz|bz2|xz|apk|aab|ipa|deb|rpm|"
                      r"appimage|iso|img|jar)$)", re.I)


def looks_like_download(url):
    """A link that may serve a file (a download page, or an installer or archive by its extension)."""
    parts = urlsplit(url)
    return bool(DOWNLOAD.search(parts.path))


def served_file(url):
    """For a download-looking link: ask over HEAD what it serves. -> (status, final URL, type) when it is a file
    (not an HTML page), else None, so the browser never starts pulling an installer only to abort it."""
    if not looks_like_download(url):
        return None
    status, final, kind = http_status(url)
    if isinstance(status, int) and status < 400 and "text/html" not in kind:
        return status, final, kind
    return None


def robots_for(start_url, timeout=10):
    """robots.txt for a public host (None for loopback). Unreachable or missing robots.txt allows all;
    401/403 disallows all, as the standard parser does."""
    import urllib.error
    import urllib.request
    import urllib.robotparser

    if is_loopback(start_url):
        return None
    parts = urlsplit(start_url)
    url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    robots = urllib.robotparser.RobotFileParser(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            robots.parse(r.read().decode("utf-8", errors="replace").splitlines())
    except urllib.error.HTTPError as e:
        robots.parse(["User-agent: *", "Disallow: /"] if e.code in (401, 403) else [])
    except OSError:
        robots.parse([])  # unreachable: allow all
    return robots


def _examine(session, result, page, dev, *, settle, host, opts, run_dir):
    """Lint the loaded page into `result` (findings, checks, outcome, screenshot). -> the page's facts."""
    time.sleep(settle)
    facts = session.evaluate(FACTS) or {}
    probe = session.probe({}).get("probe") or {}
    facts.update(lcp=probe.get("lcp"), cls=probe.get("cls"))
    findings = lint(facts, mobile=dev.get("mobile"), name=page)
    findings += verdict.findings_from_probe(probe, scenario=page, url=facts.get("url"), first_party_hosts={host},
                                            why=getattr(session, "why_failed", None))
    result["findings"] = verdict.dedupe(findings)
    result["end_url"] = facts.get("url")
    result["timing"] = facts.get("timing")
    status = facts.get("status")
    errors = [f for f in findings if f["kind"] == "page error"]
    result["checks"] = [
        {"check": "document loads with HTTP < 400", "ok": not status or status < 400,
         "detail": f"HTTP {status}" if status and status >= 400 else None},
        {"check": "no uncaught script errors", "ok": not errors, "detail": errors[0]["detail"] if errors else None},
    ]
    ok = verdict.all_ok(result["checks"])
    result.update(outcome="pass" if ok else "fail",
                  reason="loaded" if ok else "; ".join(c["check"] for c in result["checks"] if not c["ok"]))
    wall = verdict.sign_in_wall(result["url"], {"probe": probe, "url": facts.get("url")})
    if wall:  # it sent the crawl to a sign-in page: what is behind it was not checked
        result["needs_sign_in"] = wall
        result["reason"] += f"; behind sign-in (sent to {wall['url']}), not checked"
    if opts.shots:
        shot = session.screenshot(run_dir / "shots" / f"{slug(page)}.jpg")
        result["shot"] = str(shot.relative_to(run_dir)) if shot else None
    return facts


def run(start_url, opts, *, max_pages=20, device=None, devices=None, check_links=False, settle=0.8, delay=None):
    from . import report
    from . import session as session_mod
    from .suite import _device, wanted_devices

    if urlsplit(start_url).scheme not in {"http", "https"}:
        raise ConfigError("smoke needs an http(s) URL")
    names = [device] if device else wanted_devices(devices)  # desktop and a phone, unless one device is pinned
    dev = _device(names[0], "device")
    host = urlsplit(start_url).netloc
    name = f"smoke {host}"
    reaped = chrome.reap()
    if reaped and callable(opts.emit):
        opts.emit({"event": "reaped", "items": reaped})
    started_at = time.time()
    run_dir = opts.out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug(name)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    owned = None
    if opts.cdp_url:
        cdp_url = opts.cdp_url.rstrip("/")
        if not chrome.version(cdp_url):
            raise ConfigError(f"no Chrome DevTools endpoint at {cdp_url}")
        browser = {"cdp_url": cdp_url, "managed": False}
    else:
        owned = chrome.start(opts.profile, headless=opts.headless, ephemeral=opts.ephemeral)
        cdp_url = owned["cdp_url"]
        browser = {"cdp_url": cdp_url, "managed": True, "profile": owned["profile"], "headless": owned["headless"]}

    def emit(event, **data):
        if callable(opts.emit):
            opts.emit({"event": event, **data})

    robots = robots_for(start_url)
    # Public sites get at most one page per second (or robots.txt's crawl-delay); loopback runs flat out.
    crawl_delay = (robots.crawl_delay(USER_AGENT) if robots else None) or 0
    delay = max(delay if delay is not None else (0.0 if robots is None else 1.0), float(crawl_delay))
    emit("run", suite=name, run_dir=str(run_dir), browser=browser)
    session_mod.configure_env(cdp_url)
    ledger = Ledger(0.0)  # smoke makes no model calls; a zero cap proves it
    start_url = canon(start_url)
    results, queue, seen, discovered, disallowed = [], deque([start_url]), {start_url}, set(), set()
    last_load = 0.0
    interrupted = False
    session = None
    try:
        session = session_mod.Session(ledger, headless=browser.get("headless", False), hosts={host},
                                      motion=opts.motion or "reduce")
        session.set_device(dev)
        session.arm("readonly")
        while queue and len(results) < max_pages:
            url = queue.popleft()
            page = page_name(url)
            began = time.monotonic()
            quiet, load1 = wait_for_quiet(opts)
            result = {"name": page, "url": url, "goal": None, "mode": "readonly", "checks": [], "findings": [],
                      "screens": [], "load1": round(load1, 1)}
            if not quiet:
                result.update(outcome="skipped", reason=f"machine busy (load1 {load1:.0f})", seconds=0)
                results.append(result)
                continue
            if robots is not None and not robots.can_fetch(USER_AGENT, url):
                result.update(outcome="skipped", reason="disallowed by robots.txt", seconds=0)
                results.append(result)
                emit("scenario", result=result)
                continue
            emit("start", scenario=page)
            time.sleep(max(0.0, last_load + delay - time.monotonic()))
            last_load = time.monotonic()
            served = served_file(url)
            error = None if served else session.navigate(url)
            if served:
                status, final, kind = served
                result.update(outcome="pass", stop="download",
                              reason=f"a download ({kind or 'unknown type'}, HTTP {status}, {final}); not opened",
                              checks=[{"check": "link answers with HTTP < 400", "ok": True, "detail": None}])
            elif error == "net::ERR_ABORTED":
                # QAJev refuses downloads, so a link to a file aborts: say what the link serves instead of failing it
                status, final, kind = http_status(url)
                html = "text/html" in kind
                ok = isinstance(status, int) and status < 400 and not html
                result.update(outcome="pass" if ok else "fail", stop="download",
                              reason=(f"a download ({kind or 'unknown type'}, HTTP {status}, {final}); not opened"
                                      if ok else f"page did not load: {error} (HTTP {status})"),
                              checks=[{"check": "link answers with HTTP < 400", "ok": ok,
                                       "detail": None if ok else f"HTTP {status}"}])
            elif error:
                result.update(outcome="fail", reason=f"page did not load: {error}", stop="unreachable")
            else:
                facts = _examine(session, result, page, dev, settle=settle, host=host, opts=opts, run_dir=run_dir)
                if urlsplit(facts.get("url") or "").netloc == host:
                    for link in map(canon, facts.get("links", [])):
                        discovered.add(link)
                        if link in seen or SKIP.search(link):
                            continue
                        seen.add(link)
                        if robots is not None and not robots.can_fetch(USER_AGENT, link):
                            disallowed.add(link)  # robots.txt says no: not a page we owe a visit
                            continue
                        queue.append(link)
            result["seconds"] = round(time.monotonic() - began, 2)
            results.append(result)
            emit("scenario", result=result)
        # The same pages again on each further device (a phone by default): what a phone visitor gets.
        crawled = [r for r in results if r.get("end_url") and r.get("stop") != "download"]
        for extra in names[1:]:
            other = _device(extra, "devices")
            session.set_device(other)
            for first in crawled:
                page = f"{first['name']} ({extra})"
                began = time.monotonic()
                result = {"name": page, "url": first["url"], "goal": None, "mode": "readonly", "checks": [],
                          "findings": [], "screens": [], "device": extra}
                emit("start", scenario=page)
                time.sleep(max(0.0, last_load + delay - time.monotonic()))
                last_load = time.monotonic()
                error = session.navigate(first["url"])
                if error:
                    result.update(outcome="fail", reason=f"page did not load: {error}", stop="unreachable")
                else:
                    _examine(session, result, page, other, settle=settle, host=host, opts=opts, run_dir=run_dir)
                result["seconds"] = round(time.monotonic() - began, 2)
                results.append(result)
                emit("scenario", result=result)
        if check_links:
            unvisited = sorted(link for link in discovered - {r["url"] for r in results} if not SKIP.search(link))
            if robots is not None:
                unvisited = [u for u in unvisited if robots.can_fetch(USER_AGENT, u)]
            results.append(check_link_list(unvisited[:100], name="links (not crawled)", delay=delay))
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if session is not None:
            session.close()
        session_mod.stop_daemon()
        if owned and owned.get("ephemeral"):
            chrome.stop(owned["state_key"])

    suite = SimpleNamespace(name=name)
    built = report.build(suite, results, [ledger.summary()], browser=browser, started_at=started_at,
                         strict=opts.strict, interrupted=interrupted, run_dir=run_dir)
    built["motion"] = opts.motion or "reduce"
    built["smoke"] = {"start_url": start_url, "pages": len(results), "discovered_links": len(discovered),
                      "max_pages": max_pages, "delay_s": delay, "robots_txt": robots is not None,
                      "robots_disallowed": sorted(disallowed)[:50]}
    behind = sorted({r["url"] for r in results if r.get("needs_sign_in")})
    if behind:
        built["smoke"]["behind_sign_in"] = behind[:50]
    built = redact_tree(built, secret_values())
    report.write(run_dir, built)
    emit("done", gate=built["gate"], run_dir=str(run_dir))
    return built


def check_link_list(urls, *, name, delay=0.0):
    """Check links one at a time over HTTP, following redirects to wherever they lead (other hosts included)."""
    began = time.monotonic()
    broken, redirected = [], 0
    for i, u in enumerate(urls):
        if i and delay:
            time.sleep(delay)
        status, final, _kind = http_status(u)
        if not isinstance(status, int) or status >= 400:
            broken.append((u, status, final))
        elif canon(final) != canon(u):
            redirected += 1
    findings = [{"severity": "S2", "kind": "broken link", "detail": f"{u} -> {s}" + (f" (at {f})" if f != u else ""),
                 "scenario": name, "url": u} for u, s, f in broken]
    ok = not broken
    return {"name": name, "url": None, "goal": None, "mode": "readonly", "screens": [], "findings": findings,
            "checks": [{"check": f"{len(urls)} link(s) answer with HTTP < 400", "ok": ok,
                        "detail": None if ok else f"{len(broken)} broken"}],
            "outcome": "pass" if ok else "fail",
            "reason": f"{len(broken)} of {len(urls)} broken, {redirected} redirect(s) followed",
            "seconds": round(time.monotonic() - began, 2)}
