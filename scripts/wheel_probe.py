"""Does mouse-wheel scrolling get a person to the bottom of a page? No model calls.

Turns the wheel N times over one point (by default the centre of --over) in QAJev's own throwaway Chrome, read-only
guard armed, and prints scrollY after every turn plus what sits under the pointer whenever the page stalls.

  uv run python scripts/wheel_probe.py http://127.0.0.1:5612/ --over "iframe" \
      --ready "document.querySelector('iframe')?.contentDocument?.readyState === 'complete'"
"""

import argparse
import json
import sys
import time
from urllib.parse import urlsplit

from qajev import chrome, config
from qajev import session as S
from qajev.ledger import Ledger

UNDER = """((x, y) => { const chain = [];
  for (let e = document.elementFromPoint(x, y); e && chain.length < 6; e = e.parentElement)
    chain.push(e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (typeof e.className === 'string'
      && e.className ? '.' + e.className.trim().split(/\\s+/).slice(0, 2).join('.') : ''));
  return chain; })(%d, %d)"""

# Centre of the element, clamped into the viewport; null until the element is there and hit-testable at that point
# (an inert or pointer-events:none element is not, so this also waits for "the demo finished loading"). An element
# below the fold is scrolled to the middle of the screen first, as a person would scroll to it.
POINT = """(sel => { const el = document.querySelector(sel); if (!el) return null;
  let r = el.getBoundingClientRect();
  if (r.bottom <= 0 || r.top >= innerHeight) { el.scrollIntoView({block: 'center', behavior: 'instant'});
    r = el.getBoundingClientRect(); }
  const x = Math.round(Math.min(Math.max(r.left + r.width / 2, 1), innerWidth - 1));
  const y = Math.round(Math.min(Math.max(r.top + r.height / 2, 1), innerHeight - 1));
  const hit = document.elementFromPoint(x, y);
  return hit && (hit === el || el.contains(hit)) ? [x, y] : null; })(%s)"""


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url")
    p.add_argument("--over", help="CSS selector: put the pointer over this element's centre (default: viewport centre)")
    p.add_argument("--ready", help="JS expression to wait for (truthy) before the first turn")
    p.add_argument("--wait", type=float, default=30, help="seconds to wait for --ready / --over (default 30)")
    p.add_argument("--turns", type=int, default=40)
    p.add_argument("--delta", type=int, default=300, help="pixels per wheel turn (default 300)")
    p.add_argument("--pause", type=float, default=0.25, help="seconds between turns (default 0.25)")
    p.add_argument("--size", default="1280x900")
    p.add_argument("--headless", action="store_true", help="headless (default: headed, minimised)")
    p.add_argument("--env-file")
    a = p.parse_args()
    width, height = (int(v) for v in a.size.lower().split("x"))
    config.load_env(a.env_file)
    rec = chrome.start("wheel", headless=a.headless, ephemeral=True)
    try:
        S.configure_env(rec["cdp_url"])
        s = S.Session(Ledger(0.0), headless=a.headless, hosts={urlsplit(a.url).hostname})
        try:
            s.set_device({"width": width, "height": height, "mobile": False, "scale": 1})
            s.arm("readonly")
            if err := s.navigate(a.url):
                sys.exit(f"navigation failed: {err}")
            deadline = time.monotonic() + a.wait
            point = [width // 2, height // 2]
            while True:
                ready = not a.ready or s.evaluate(f"!!({a.ready})")
                found = s.evaluate(POINT % json.dumps(a.over)) if a.over else point
                if ready and found:
                    point = found
                    break
                if time.monotonic() > deadline:
                    sys.exit(f"not ready after {a.wait}s: ready={ready} over={a.over!r} hit-testable={bool(found)}")
                time.sleep(0.25)
            total = s.evaluate("document.documentElement.scrollHeight - innerHeight")
            start = s.evaluate("Math.round(scrollY)")
            print(f"start at scrollY {start}; pointer at {point}, under it: {s.evaluate(UNDER % tuple(point))}")
            print(f"scrollable height {total} px; {a.turns} turns of {a.delta} px would cover {a.turns * a.delta} px")
            ys, stalls = [], []
            for i in range(a.turns):
                before = s.evaluate("Math.round(scrollY)")
                s.call("Input.dispatchMouseEvent", type="mouseWheel", x=point[0], y=point[1], deltaX=0,
                       deltaY=a.delta)
                time.sleep(a.pause)
                y = s.evaluate("Math.round(scrollY)")
                ys.append(y)
                if y == before and y < total:
                    stalls.append({"turn": i + 1, "scrollY": y, "under": s.evaluate(UNDER % tuple(point))})
            print("scrollY after each turn:", ys)
            end = ys[-1] if ys else start
            print(f"moved {end - start} px (scrollY {start} -> {end} of {total}); {len(stalls)} turns did not move it")
            for st in stalls[:5]:
                print(f"  stall at turn {st['turn']} (scrollY {st['scrollY']}): {st['under']}")
        finally:
            s.close()
            S.stop_daemon()
    finally:
        chrome.stop(rec["state_key"])


if __name__ == "__main__":
    main()
