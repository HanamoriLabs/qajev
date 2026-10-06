"""Guard configuration and the in-page script source.

The label lists merge what earlier site checkers and onboarding harnesses learned the hard way (a "Close all
tabs" button that closed a real browser's tabs, payment pages, sign-out-everywhere).
Goal text is not a control: anything Jev must never press is hidden before it can see the page.
"""

import hashlib
import json
from pathlib import Path

SOURCE = (Path(__file__).parent / "js" / "guard.js").read_text()

# Always hidden, in every mode.
DENY = [
    r"\b(sign|log)\s*-?\s*(out|off)\b",
    r"\bclose\s+all\b",
    r"\bconfirm\s+close\b",
    r"\b(delete|erase|destroy|remove|close|deactivate|terminate)\s+(my\s+|your\s+|this\s+|the\s+)?"
    r"(account|profile|everything|all\b|data|workspace|organi[sz]ation|org\b|team|project|flock)",
    r"\bclear\s+(all|data|everything|history|cache)\b",
    r"\breset\s+(everything|all|password|data)\b",
    r"\b(revoke|rotate|regenerate|roll)\b",
    r"\b(remove|delete)\s+(key|token|passkey|member|user)",
    r"\bchange\s+(password|unlock|email)\b",
    r"\b(disconnect|unlink|transfer\s+ownership|leave\s+(team|workspace|organi[sz]ation|org|flock|group))\b",
    r"\b(cancel|end)\s+(my\s+)?(plan|subscription|membership|account)\b",
    r"\b(downgrade|upgrade)\b",
    r"\b(manage\s+)?billing\b",
    r"\b(checkout|check\s+out|subscribe|unsubscribe|pay\s+now|pay\b|buy\b|purchase|place\s+order|complete\s+(purchase|order))",
    r"\bstart\s+(my\s+)?(free\s+)?(trial|team|fleet)\b",
    r"\bconfirm\s+(and\s+)?pay\b",
    r"\bdanger\s+zone\b",
    r"\bkill\s+switch\b",
    r"\b(archive|save|delete)\s+all\b",
]

# Hidden unless the run supplies a fake transcript: the recogniser is stubbed either way.
MIC = [r"\bspeak\b|\bhold\s+to\s+talk\b|\btap\s+to\s+speak\b|\bmicrophone\b|\bdictat|\bvoice\s+(input|mode)\b"]

# Hidden only in read-only runs: controls whose name says they change something.
# Matched against the control's accessible name (aria-label, else its text), from the start.
MUTATING = [
    r"^\W*(save|apply|reopen|archive|delete|remove|create|sync|connect|confirm|submit|send|post|publish|"
    r"update|add|freeze|unfreeze|pause|resume|reset|set|clear|invite|share|approve|reject|accept|decline|"
    r"import|upload|restore|merge|move|rename|duplicate|enable|disable|turn\s+(on|off)|mark\s+as)\b",
    r"^\W*new(\s+\S+)?\s*$",  # "New", "+ New", "New project"; not "New York office"
]

# Production is read-only. Destructive is never (José, 6 Oct, 0.4.0): controls that destroy something are hidden in
# every mode, on every host. `allow` never shows them; only `allow_destructive` does, and only on a local dev host
# (the guard decides per page; suite.check_safety refuses the flag for a production host before Chrome starts).
# Matched against everything a person reads on the control, anywhere in it.
DESTRUCTIVE = [
    r"\b(delete|deleting|remove|removing|erase|destroy|drop(?![\s-]?down)|purge|wipe|cancel|refund|void|revoke|"
    r"deactivate|unsubscribe|archive|reset|uninstall|disconnect)\b",
    r"\bempty\s+(the\s+)?(trash|bin|recycle)",
    r"\b(close|terminate)\s+(my\s+|your\s+|this\s+|the\s+)?account\b",
]
# A reset, clear or cancel of something harmless is not destructive: filters, a search, a selection, the form being
# edited (a hidden "Reset filters" must not strand a run). Its words are set aside before DESTRUCTIVE is matched, so
# any other destructive word on the control still counts ("Clear search and delete item" stays hidden).
HARMLESS = [
    r"\b(reset|clear|cancel)\s+(all\s+)?(the\s+|my\s+|your\s+|this\s+)?(filters?|search(es)?|search\s+query|"
    r"selection|selected|edits?|editing|form|changes)\b",
]
# ...unless the control names something that matters: then it stays destructive, whatever else it says.
GRAVE = [r"\b(accounts?|orders?|subscriptions?|plans?|bookings?|data|payments?|memberships?)\b"]

MODES = ("readonly", "mutate")


def build_config(*, mode="readonly", hosts=(), deny=(), allow=(), allow_requests=(), speech=None,
                 redact_emails=False, allow_secret_fields=False, allow_destructive=False):
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    cfg = {
        "mode": mode,
        "hosts": sorted(set(hosts)),
        "deny": DENY + ([] if speech else MIC) + list(deny),
        "mutating": MUTATING,
        "destructive": DESTRUCTIVE,
        "harmless": HARMLESS,
        "grave": GRAVE,
        "allow": list(allow),
        "allow_requests": list(allow_requests),
        "speech": speech,
        "redact_emails": bool(redact_emails),
        "allow_secret_fields": bool(allow_secret_fields),
        "allow_destructive": bool(allow_destructive),
    }
    cfg["v"] = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]
    return cfg


def script(cfg):
    return SOURCE.replace("__QAJEV_CONFIG__", json.dumps(cfg))
