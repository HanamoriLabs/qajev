"""Deterministic web UX checks. CDP and evidence stay here; DOM geometry lives in ux-probe.js.

The observer never uses Jev or a model. Probe actions run outside the action hooks,
and every temporary keyboard/scroll/overlay change is restored before the user's step.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path

from .ux_options import resolve

SOURCE = (Path(__file__).parent / "ux-probe.js").read_text()
ERRORS = (RuntimeError, ValueError, OSError, TimeoutError)
FRAMES = "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"


def attach(session, opts, run_dir, name, inherited=None):
    config = resolve(opts, inherited)
    old_script = getattr(session, "ux_script_id", None)
    if isinstance(old_script, str):
        session.call("Page.removeScriptToEvaluateOnNewDocument", identifier=old_script)
    session.ux_observer = None
    session.ux_script_id = None
    if config["ux"] == "off":
        return None
    observer = Observer(session, config, run_dir, name)
    session.ux_script_id = session.call("Page.addScriptToEvaluateOnNewDocument", source=SOURCE).get("identifier")
    session.ux_observer = observer
    return observer


class Observer:
    def __init__(self, session, config, run_dir, name):
        self.session, self.config = session, config
        self.run_dir = Path(run_dir) if run_dir else None
        self.stem = hashlib.sha256(name.encode()).hexdigest()[:12]
        self.result = {"mode": config["ux"], "status": "complete", "findings": [], "skipped": [], "notes": []}
        self.step = 0
        self.seen, self.messages, self.screens = {}, {}, set()
        self.counts, self.omitted = Counter(), Counter()
        self.in_action = False

    def probe(self, method, *args):
        arguments = ",".join(json.dumps(arg) for arg in args)
        return self.session.evaluate(f"window.__qajevUx.{method}({arguments})", timeout_ms=12000)

    def note(self, value):
        if value not in self.result["notes"]:
            self.result["notes"].append(value)

    def skipped(self, value):
        if value not in self.result["skipped"]:
            self.result["skipped"].append(value)

    def safe(self, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ERRORS as error:
            # A failed measurement is explicit. Advisory instrumentation must not turn
            # a working user's step into a harness error or prevent its execution.
            self.skipped({"check": "UX", "reason": f"measurement unavailable: {type(error).__name__}: {error}"})
            self.result["status"] = "incomplete"
            self.in_action = False
            return None

    def settle(self, quiet_ms=None, timeout=10):
        quiet_ms = self.config["ux_settle_ms"] if quiet_ms is None else quiet_ms
        began = time.monotonic()
        until = began + timeout
        while True:
            state = self.probe("settleInfo") or {}
            net = getattr(self.session, "net", None)
            network_quiet = net.ux_quiet_ms() if net and hasattr(net, "ux_quiet_ms") else quiet_ms
            if (state.get("quiet_ms", 0) >= quiet_ms and not state.get("finite_animations")
                    and network_quiet >= quiet_ms and (time.monotonic() - began) * 1000 >= quiet_ms):
                return True
            if time.monotonic() >= until:
                self.note("page did not settle")
                return False
            time.sleep(0.05)

    def start(self):
        self.session.evaluate(SOURCE)
        self.settle()
        self.scan()

    def scan(self):
        self.collect(self.probe("scanStatic", self.config) or {})
        if isinstance(getattr(self.session, "guard_cfg", None), dict):
            guarded = self.session.evaluate("Array.from(document.querySelectorAll('[data-qajev-guard]')).map(el => { "
                                            "const item=window.__qajevUx.element(el); "
                                            "return {key:item.key,desc:item.desc}; })") or []
            if guarded:
                self.collect({"status": "incomplete", "skipped": [{"check": "UX",
                              "reason": "scan incomplete: QAJev's guard changed these controls before measurement",
                              "unchecked": guarded}]})
        if "keyboard" in self.config["ux_checks"]:
            width = self.session.evaluate("innerWidth")
            if width > 500:
                self.skipped({"check": "UX-KEYBOARD", "reason": f"viewport is not a phone ({width} px wide)"})
        if "keyboard" in self.config["ux_checks"] and self.config["ux_keyboard_scope"] == "all":
            fields = self.probe("fields") or []
            screen = self.screen(fields)
            if screen not in self.screens:
                self.screens.add(screen)
                for field in fields:
                    self.keyboard(field["id"], screen)

    def screen(self, fields=None):
        fields = self.probe("fields") if fields is None else fields
        url = self.session.evaluate("location.href")
        return str(url) + "|" + "|".join(sorted(field["key"] for field in fields or []))

    def collect(self, output, screen=None):
        if output.get("status") == "incomplete" or any(
                str(item.get("reason", "")).startswith("scan incomplete") for item in output.get("skipped") or []):
            self.result["status"] = "incomplete"
        for item in output.get("skipped") or []:
            self.skipped(item)
        for note in output.get("notes") or []:
            self.note(note)
        for original in output.get("findings") or []:
            item = dict(original)
            check = item["check"]
            key = ((check, screen) if check.startswith("UX-KEYBOARD-") else
                   (check, item["element"]["key"], re.sub(r"\d+(?:\.\d+)?", "#", item["message"])))
            previous = self.seen.get(key)
            if previous is not None:
                previous["repeats"] += 1
                if check.startswith("UX-KEYBOARD-"):
                    labels = previous.setdefault("data", {}).setdefault("fields", [])
                    data = item.get("data") or {}
                    label = (data.get("field_label") or (data.get("field") or {}).get("label")
                             or item["element"].get("label") or item["element"]["desc"])
                    if label not in labels:
                        labels.append(label)
                        previous["message"] = self.messages[key].replace(labels[0], ", ".join(labels), 1)
                continue
            if self.counts[check] >= self.config["ux_max_findings"]:
                self.omitted[check] += 1
                self.result["notes"][:] = [note for note in self.result["notes"]
                                            if not isinstance(note, str) or not note.startswith(check + ": and ")]
                self.note(f"{check}: and {self.omitted[check]} more")
                continue
            item.update(step=self.step, repeats=1, severity="fail" if self.config["ux"] == "strict" else "advisory")
            if check.startswith("UX-KEYBOARD-"):
                data = item.setdefault("data", {})
                label = data.get("field_label") or (data.get("field") or {}).get("label") or item["element"]["desc"]
                item["data"]["fields"] = [label]
            try:
                item["screenshot"] = self.capture(item)
            except ERRORS as error:
                item["screenshot"] = None
                self.note(f"{check}: crop unavailable: {error}")
            if not item["screenshot"]:
                self.result["status"] = "incomplete"
            item.pop("mark", None)
            self.counts[check] += 1
            self.seen[key] = item
            self.messages[key] = item["message"]
            self.result["findings"].append(item)

    def capture(self, finding):
        if self.run_dir is None:
            self.note("UX crops need a report folder")
            return None
        token = self.probe("snapshot")
        try:
            if finding["check"].startswith("UX-INPUT-"):
                self.probe("scrollField", (finding.get("mark") or {}).get("elementId"))
                self.session.evaluate(FRAMES)
            clip = self.probe("mark", finding)
            if not clip:
                raise RuntimeError("the element is no longer available")
            relative = f"ux/{self.stem}-{finding['check']}-{self.counts[finding['check']] + 1}.png"
            data = self.session.capture_region_png({"name": finding["check"], **clip})
            path = self.run_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return relative
        finally:
            try:
                self.probe("unmark")
            finally:
                self.probe("restore", token)

    def keyboard(self, field_id, screen):
        view = self.session.evaluate("({width:innerWidth,height:innerHeight,scale:devicePixelRatio})")
        saved = dict(self.session.device) if self.session.device else None
        width, height = (saved or view)["width"], (saved or view)["height"]
        if width > 500:
            self.skipped({"check": "UX-KEYBOARD", "reason": f"viewport is not a phone ({width} px wide)"})
            return
        self.require_guard()
        token = self.probe("snapshot")
        try:
            full = self.probe("keyboardInfo", field_id, {**self.config, "resetMain": True}) or {}
            field = full.get("field")
            if not field:
                self.collect(full)
                return
            rect = field.get("visible_rect") or field["rect"]
            x, y, w, h = rect
            if w > 0 and h > 0 and field.get("center_clear", True):
                self.require_guard()
                self.session.trusted_click(x + w / 2, y + h / 2)
            focused = self.probe("keyboardInfo", field_id) or {}
            if not focused.get("active_is_field") and not field.get("center_clear", True):
                self.require_guard()
                self.probe("focusField", field_id)
                self.note("focused by script")
                focused = self.probe("keyboardInfo", field_id) or {}
            if not focused.get("active_is_field"):
                self.skipped({"check": "UX-KEYBOARD", "reason": f"could not focus {field['desc']}"})
                return
            self.session.call("Emulation.setDeviceMetricsOverride", width=width,
                              height=height - int(height * self.config["ux_keyboard_ratio"] + 0.5),
                              deviceScaleFactor=(saved or view).get("scale", 1), mobile=True)
            self.session.evaluate(FRAMES)
            self.settle(300, 2)
            self.probe("scrollField", field_id)
            self.session.evaluate(FRAMES)
            opened = self.probe("keyboardInfo", field_id, {**self.config, "phase": "open"}) or {}
            self.collect(opened, screen)
            dismissed = bool(opened.get("done")) or bool(opened.get("dismiss_ignored"))
            if not dismissed and opened.get("return_eligible"):
                dismissed = True
                if self.config["ux_keyboard_press_return"]:
                    self.require_guard()
                    self.probe("dismissStart", {"submit": True})
                    try:
                        for kind in ("rawKeyDown", "char", "keyUp"):
                            self.session.call("Input.dispatchKeyEvent", type=kind, key="Enter", code="Enter",
                                              windowsVirtualKeyCode=13, **({"text": "\r"} if kind == "char" else {}))
                        time.sleep(0.3)
                        dismissed = not (self.probe("keyboardInfo", field_id) or {}).get("active_is_text_field", True)
                    finally:
                        self.probe("dismissEnd")
            if not dismissed:
                point = self.probe("neutralPoint", field_id)
                if point:
                    self.require_guard()
                    self.probe("dismissStart", {"mouse": True})
                    try:
                        self.session.trusted_click(point["x"], point["y"])
                        time.sleep(0.3)
                        dismissed = not (self.probe("keyboardInfo", field_id) or {}).get("active_is_field", True)
                    finally:
                        self.probe("dismissEnd")
            if not dismissed:
                dismissed_result = self.probe("keyboardInfo", field_id,
                                              {**self.config, "phase": "open", "dismissFailed": True}) or {}
                dismissed_result["findings"] = [f for f in dismissed_result.get("findings", [])
                                                 if f["check"] == "UX-KEYBOARD-DISMISS"]
                self.collect(dismissed_result, screen)
        finally:
            try:
                self.probe("dismissEnd")
            finally:
                try:
                    if saved:
                        self.session.call("Emulation.setDeviceMetricsOverride",
                                          width=saved["width"], height=saved["height"],
                                          deviceScaleFactor=saved.get("scale", 1), mobile=bool(saved.get("mobile")))
                    else:
                        self.session.call("Emulation.clearDeviceMetricsOverride")
                finally:
                    self.probe("restore", token)
                    self.settle()

    def require_guard(self):
        if isinstance(getattr(self.session, "guard_cfg", None), dict):
            self.session.require_guard()

    def before(self, target=None, *, typing=False, action="action"):
        if self.in_action:
            return False
        self.in_action = True
        self.step += 1
        if typing and "keyboard" in self.config["ux_checks"]:
            field = self.probe("element", target)
            if field and field.get("is_text_field", True):
                self.keyboard(field["id"], self.screen())
        if "shift" in self.config["ux_checks"]:
            self.probe("shiftBefore", target)
        self.action = action
        return True

    def after(self):
        try:
            began = time.monotonic()
            self.settle()
            if "shift" in self.config["ux_checks"]:
                self.collect(self.probe("shiftAfter", {**self.config, "action": self.action,
                                                       "settle_ms": round((time.monotonic() - began) * 1000)}) or {})
            self.scan()
        finally:
            self.in_action = False

    def finish(self):
        self.settle()
        self.scan()

    def apply(self, result):
        legacy = result.get("ux")
        if isinstance(legacy, list):
            for note in legacy:
                self.note(note)
        result["ux"] = self.result
        if self.config["ux"] == "strict" and self.result["findings"]:
            result.update(outcome="fail", reason=f"ux: {len(self.result['findings'])} findings")
        if self.config["ux"] == "strict" and self.result["status"] == "incomplete":
            result.update(outcome="fail", reason="ux: scan incomplete")
