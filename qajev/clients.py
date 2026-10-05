"""Multiplayer scenarios: N players, each in a browser context of its own, acting together, checked against each other.

A scenario with `clients:` (suite.py) runs here, not with Jev (José, 5 Oct: "spawn like 5 browsers, all join, and make
sure there's fidelity, consistency and concurrency"). Each client is a Session(isolated=True): its own cookies,
storage, cache and service workers, its own tab, QAJev's guard armed in it as in any scenario. `steps` drive them:
`all` acts on every client at the same instant (or staggered, or jittered), `client` on some, `snapshot` reads every
client's `state` at once (after `until` holds on it, timed). Then the expectations hold on every client, and `across`
checks are judged over all their states in a blank tab of QAJev's own, where page code never sees them. No model
calls: $0.
"""

import base64
import json
import random
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from . import verdict
from .suite import page_checks as wants_page_checks

SETTLE_POLL = 0.5
STATE_KEEP = 2000  # characters of each client's state kept in the report (the checks see all of it)
# The browser daemon reads each command as one line of at most 64 KiB (asyncio's default), and refuses a longer one
# ("Separator is found, but chunk is longer than limit"). Five game states over two snapshots passed that (verse1,
# 5 Oct), so the judge tab gets them in pieces of this many base64 characters (base64: nothing to escape on the way).
UPLOAD_CHUNK = 32 * 1024

# Every client's `state` at one moment: after `until` holds in that page (polled every 10 ms, in the page), its time
# (Date.now(), the same clock for every client on this machine) and its state, or the error reading it.
SNAPSHOT_JS = """(async () => {
  const read = async () => {
    try { return { state: await (async () => (%(state)s))() }; }
    catch (e) { return { error: String(e && e.message || e) }; }
  };
  const until = %(until)s;
  const t0 = Date.now();
  if (until) {
    for (;;) {
      let ok = false;
      try { ok = !!(await until()); } catch (e) {}
      if (ok) break;
      if (Date.now() - t0 > %(timeout_ms)d) return { at: null, timed_out: true, ...(await read()) };
      await new Promise((r) => setTimeout(r, 10));
    }
  }
  const at = Date.now();
  return { at, ...(await read()) };
})()"""


@dataclass
class Client:
    name: str
    url: str
    session: Any


def client_url(template, name, i, run):
    """A client's address: {client} its name, {i} its number (1..N), {run} this scenario run's own token."""
    return template.replace("{client}", name).replace("{i}", str(i)).replace("{run}", run)


def offsets(n, stagger_ms, jitter_ms, rng):
    """When each of n clients acts, in seconds from a common start: i * stagger, plus or minus up to jitter."""
    return [max(0.0, (i * stagger_ms + (rng.uniform(-jitter_ms, jitter_ms) if jitter_ms else 0.0)) / 1000)
            for i in range(n)]


def together(clients, act, starts=None):
    """act(client) on every client at once, each after its start offset (seconds). -> [(name, value, error)]."""
    begin = time.monotonic() + 0.05  # every thread up before the first one acts

    def one(i):
        wait = begin + (starts[i] if starts else 0.0) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return clients[i].name, act(clients[i]), None
        except Exception as e:  # noqa: BLE001 (one client's error belongs to that client, reported by name)
            return clients[i].name, None, e

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        return list(pool.map(one, range(len(clients))))


def snapshot(clients, state, until=None, timeout=10.0):
    """Every client's state, at once. -> [{name, url, at, state} | {..., error} | {..., timed_out}]."""
    expression = SNAPSHOT_JS % {"state": state, "until": f"async () => ({until})" if until else "null",
                                "timeout_ms": int(timeout * 1000)}
    out = []
    for name, value, error in together(clients, lambda c: c.session.evaluate(expression,
                                                                             timeout_ms=int(timeout * 1000) + 15000)):
        url = next(c.url for c in clients if c.name == name)
        out.append({"name": name, "url": url, **(value or {})} if error is None else
                   {"name": name, "url": url, "at": None, "error": str(error)[:300]})
    return out


def _evaluate(tab, expression):
    r = tab.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True, timeout=10000)
    if r.get("exceptionDetails"):
        raise RuntimeError(f"judge tab: {(r['exceptionDetails'].get('exception') or {}).get('description')}"[:300])
    return r


def judge(tab, checks, clients_state, snapshots):
    """Across checks, judged over every client's state in QAJev's own blank tab. The states go there first, in
    pieces under the daemon's line limit; each check then parses its own copy (a check that sorts or edits them
    cannot change what the next one sees). -> the checks."""
    from .session import awaited

    if not checks:
        return []
    text = base64.b64encode(json.dumps({"clients": clients_state, "snapshots": snapshots}).encode()).decode()
    _evaluate(tab, "window.__qajevIn = []")
    for i in range(0, len(text), UPLOAD_CHUNK):
        _evaluate(tab, f"window.__qajevIn.push('{text[i:i + UPLOAD_CHUNK]}')")
    _evaluate(tab, "window.__qajevStates = new TextDecoder().decode(Uint8Array.from(atob(window.__qajevIn.join('')),"
                   " (c) => c.charCodeAt(0))); delete window.__qajevIn; true")
    out = []
    for check in checks:
        r = _evaluate(tab, awaited(f"(({{clients, snapshots}}) => ({check['js']}))"
                                   "(JSON.parse(window.__qajevStates))"))
        value = (r.get("result") or {}).get("value") or {}
        if "error" in value:
            out.append({"check": check["check"], "ok": False, "detail": f"error: {value['error']}"[:300]})
            continue
        ok = value.get("value") is True
        out.append({"check": check["check"], "ok": ok,
                    "detail": None if ok else f"returned {value.get('value')!r}"[:300]})
    return out


def _trim(snaps):
    """Snapshots for the report: each state as JSON, cut to STATE_KEEP characters."""
    out = {}
    for name, rows in snaps.items():
        out[name] = [{**{k: v for k, v in r.items() if k != "state"},
                      **({"state": json.dumps(r["state"])[:STATE_KEEP]} if "state" in r else {})} for r in rows]
    return out


def _first_error(results, where):
    """The first client error in a together() round, as (stop, detail), or None."""
    from .session import GuardMissing, HookFailed, LeftSite

    for name, _value, error in results:
        if error is None:
            continue
        stop = {HookFailed: "hook_failed", GuardMissing: "guard_missing", LeftSite: "left_site"}.get(
            type(error), "browser_error")
        text = str(error) if stop != "browser_error" else f"{type(error).__name__}: {error}"
        return stop, f"[{name}] {where}: {text}"[:500]
    return None


def run(scenario, *, ledger, hosts, run_dir, suite_meta, step=None):
    """Run a scenario with clients. -> its result (outcome, reason, checks, findings, per-client shots and states)."""
    from . import session as session_mod

    step = step or (lambda doing: None)
    started = time.monotonic()
    token = secrets.token_hex(3)
    seed = scenario.seed if scenario.seed is not None else random.SystemRandom().randrange(2**31)
    rng = random.Random(seed)
    result: dict[str, Any] = {
        "name": scenario.name, "url": scenario.url, "goal": None, "mode": scenario.mode, "device": scenario.device,
        "checks": [], "findings": [], "screens": [], "blocked_writes": [], "guard_hidden": 0, "history": [],
        "jev": None, "shot": None, "seed": seed, "run_token": token, "steps": [],
        "clients": [{"name": c["name"], "url": client_url(c["url"], c["name"], i, token)}
                    for i, c in enumerate(scenario.clients, 1)],
    }
    findings, checks, snaps = [], [], {}
    stop, detail = "checked", None
    clients = []
    tab = None
    where, at, final_done = "opening", 0, False  # the part running now; the steps before step `at` + 1 are over

    def absorb(client, observed):
        probe = (observed or {}).get("probe") or {}
        findings.extend(verdict.findings_from_probe(probe, scenario=f"{scenario.name} [{client.name}]",
                                                    url=observed.get("url"), first_party_hosts=hosts,
                                                    why=client.session.why_failed))
        result["blocked_writes"].extend(probe.get("blocked") or [])
        result["guard_hidden"] = max(result["guard_hidden"], probe.get("hidden") or 0)
        return observed

    page_expect = {k: v for k, v in scenario.expect.items() if k != "across"}
    try:
        step(f"opening {len(scenario.clients)} clients, each in a browser context of its own")
        for c in result["clients"]:
            clients.append(Client(c["name"], c["url"], session_mod.Session(
                ledger, headless=suite_meta["headless"], hosts=hosts, guard_opts=suite_meta["guard"],
                motion=suite_meta.get("motion", "reduce"), isolated=True)))
        jev = clients[0].session.jev
        tab = session_mod.Tab(jev.cdp, jev.admin.ensure_daemon)  # the judge: a blank tab no page code reaches

        def open_page(c):
            c.session.arm(scenario.mode)
            c.session.set_device(scenario.device)
            error = c.session.navigate(c.url)
            if error:
                raise RuntimeError(f"page did not load: {error}")
            observed = absorb(c, c.session.probe(page_expect))
            findings.extend(verdict.document_findings(observed.get("status"), scenario.expect,
                                                      scenario=f"{scenario.name} [{c.name}]",
                                                      url=observed.get("url")))

        failed = _first_error(together(clients, open_page), "opening")
        if failed:
            stop, detail = "unreachable" if "page did not load" in failed[1] else failed[0], failed[1]

        for n, s in enumerate(scenario.steps if stop == "checked" else [], 1):
            began = time.monotonic()
            where, at = _label(n, s), n
            if s["kind"] == "snapshot":
                step(f"snapshot {s['name']}" + (f": waiting for {s['until']}" if s["until"] else ""))
                deadline = time.monotonic() + s["settle"]
                while True:
                    rows = snapshot(clients, scenario.state, s["until"], s["timeout"])
                    snaps[s["name"]] = rows
                    states = [{k: r.get(k) for k in ("name", "url", "at", "state", "error")} for r in rows]
                    got = judge(tab, s["expect"], states, snaps)
                    if verdict.all_ok(got) or not got or time.monotonic() >= deadline:
                        break
                    time.sleep(SETTLE_POLL)
                checks += [{**c, "check": f"snapshot {s['name']}: {c['check']}"} for c in got]
                late = [r["name"] for r in rows if r.get("timed_out")]
                broken = [f"{r['name']} ({r['error']})" for r in rows if r.get("error")]
                result["steps"].append({"step": n, "snapshot": s["name"], "timed_out": late, "errors": broken,
                                        "ms": round((time.monotonic() - began) * 1000)})
                continue
            targets = [c for c in clients if c.name in s["clients"]]
            starts = offsets(len(targets), s["stagger"], s["jitter"], rng) if s["kind"] == "all" else None
            kind = next(iter(s["hook"]))
            step(f"{kind} on {', '.join(c.name for c in targets)}"
                 + (f" (stagger {s['stagger']:.0f} ms)" if s.get("stagger") else "")
                 + (f" (jitter ±{s['jitter']:.0f} ms)" if s.get("jitter") else ""))
            done = together(targets, lambda c, hook=s["hook"]: c.session.run_hook(hook, c.url), starts)
            result["steps"].append({"step": n, "do": kind, "clients": [c.name for c in targets],
                                    "starts_ms": [round(x * 1000) for x in starts] if starts else None,
                                    "ms": round((time.monotonic() - began) * 1000)})
            failed = _first_error(done, f"step {n} ({kind})")
            if failed:
                stop, detail = failed
                break

        if stop == "checked" or stop == "hook_failed":
            where = "the final checks"
            step(f"checking every client and across them (settling up to {scenario.settle:.0f} s)")
            deadline = time.monotonic() + scenario.settle
            while True:
                per_client = []
                for name, observed, error in together(clients, lambda c: absorb(c, c.session.probe(page_expect))):
                    if error is not None:
                        raise error
                    per_client += [{**c, "check": f"[{name}] {c['check']}"}
                                   for c in verdict.page_checks(page_expect, observed)]
                across = []
                if scenario.expect.get("across"):
                    rows = snapshot(clients, scenario.state)
                    result["final_state"] = _trim({"final": rows})["final"]  # not a snapshot: no name to collide with
                    states = [{k: r.get(k) for k in ("name", "url", "at", "state", "error")} for r in rows]
                    across = [{**c, "check": f"across: {c['check']}"}
                              for c in judge(tab, scenario.expect["across"], states, snaps)]
                final = per_client + across
                if stop != "checked" or verdict.all_ok(final) or time.monotonic() >= deadline:
                    break
                time.sleep(SETTLE_POLL)
            checks += final
            final_done = True
    except Exception as e:  # noqa: BLE001 (a client that cannot even open is the run's trouble, not the product's)
        from .session import GuardMissing

        stop = "guard_missing" if isinstance(e, GuardMissing) else "browser_error"
        detail = f"{where}: {type(e).__name__}: {e}"[:500]
    finally:
        if run_dir:
            for c in clients:
                shot = c.session.screenshot(run_dir / "shots" / f"{_slug(scenario.name)}-{c.name}.jpg")
                entry = next(r for r in result["clients"] if r["name"] == c.name)
                entry["shot"] = str(shot.relative_to(run_dir)) if shot else None
            result["shot"] = next((r.get("shot") for r in result["clients"] if r.get("shot")), None)
        for c in clients:
            c.session.close()
        if tab is not None:
            tab.close()

    result["snapshots"] = _trim(snaps)
    result["checks"] = checks
    result["findings"] = verdict.dedupe(findings)
    result["stop"] = stop
    has = wants_page_checks(page_expect) or bool(scenario.expect.get("across")) or any(
        s["kind"] == "snapshot" and s["expect"] for s in scenario.steps)
    # A run that broke stops before its last steps: what never ran is named, and the run can never pass on the
    # checks that did (verse1, 5 Oct: two snapshot checks passed, then a browser error, graded PASS).
    not_run = [] if stop == "checked" else [_label(n, s) for n, s in enumerate(scenario.steps, 1) if n > at]
    not_run += [] if final_done else ["the final checks"]
    outcome, reason = verdict.classify(stop, checks, has_checks=has, stop_detail=detail, not_run=not_run)
    result.update(outcome=outcome, reason=reason, stop_detail=detail, not_run=not_run,
                  seconds=round(time.monotonic() - started, 2))
    return result


def _label(n, s):
    """A step as the report names it: "step 4 (snapshot walking)", "step 3 (js)"."""
    return f"step {n} (" + (f"snapshot {s['name']}" if s["kind"] == "snapshot" else next(iter(s["hook"]))) + ")"


def _slug(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")[:60] or "scenario"
