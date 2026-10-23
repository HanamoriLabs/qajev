"""Bound diagnostic replies in the browser before they cross CDP; local fixtures only."""

import json
import os

import pytest
from test_check_evidence_live import open_fixture
from test_ux_dom_live import ENDPOINT
from test_ux_dom_live import no_model_initialization as no_model_initialization
from test_ux_dom_live import page as page
from test_ux_dom_live import site as site

from qajev import config, verdict

pytestmark = pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux",
                               reason="requires the approved CPU fixture browser")


@pytest.mark.parametrize("length", [1048575, 1048576, 1048577, 2_000_000])
@pytest.mark.parametrize("kind", ["console", "exception", "rejection", "resource", "csp"])
def test_page_diagnostic_source_boundary(page, site, length, kind):
    session, _ = open_fixture(page, site)
    events = {
        "console": "console.error(text)",
        "exception": "dispatchEvent(new ErrorEvent('error', {message:text}))",
        "rejection": "dispatchEvent(new PromiseRejectionEvent('unhandledrejection', {promise:Promise.resolve(),reason:text}))",
        "resource": "const image = document.createElement('img'); Object.defineProperty(image,'src',{value:text}); "
                    "document.body.append(image); "
                    "image.dispatchEvent(new Event('error'))",
        "csp": "dispatchEvent(new SecurityPolicyViolationEvent('securitypolicyviolation', {blockedURI:text}))",
    }
    session.evaluate(f"(() => {{ const text = 'x'.repeat({length}); {events[kind]}; return true; }})()")
    probe = session.probe({})["probe"]
    [row] = [row for row in probe["errors"] if row["kind"] == kind]
    expected = "x" * length if length <= 1048576 else f"[truncated {length} characters before transfer; exceeds 1 MiB]"
    matches = row["detail"] == expected
    assert matches
    if kind == "console":
        matches = probe["page_console"][0]["text"] == expected
        assert matches


@pytest.mark.parametrize("count", [262144, 262145])
def test_source_limit_uses_utf8_bytes_and_counts_whole_characters(page, site, count):
    session, _ = open_fixture(page, site)
    observed = session.probe({"js": f"'🧪'.repeat({count})"})
    expected = "🧪" * count if count == 262144 else f"[truncated {count} characters before transfer; exceeds 1 MiB]"
    matches = observed["js"]["value"] == expected
    assert matches


@pytest.mark.parametrize("kind", ["string", "error", "shown", "object", "array"])
def test_js_replies_are_bounded_before_transfer_without_changing_failure_kind(page, site, kind):
    session, _ = open_fixture(page, site)
    expressions = {
        "string": "'x'.repeat(2000000)",
        "error": "(() => {throw new Error('TypeError: ' + 'x'.repeat(2000000));})()",
        "shown": "(() => { const f = () => {}; f.toString = () => 'x'.repeat(2000000); return f; })()",
        "object": "({message: 'x'.repeat(2000000)})",
        "array": "['x'.repeat(2000000)]",
    }
    observed = session.probe({"js": expressions[kind]})
    raw = json.dumps(observed["js"])
    size = len(raw.encode())
    assert size < 1000
    assert "[truncated " in raw
    [check] = verdict.page_checks({"js": "fixture"}, observed)
    assert check["ok"] is False
    if kind == "error":
        assert observed["js"]["error"].startswith("TypeError: [truncated ")
        assert check.get("broke") is True
    if kind == "array":
        assert isinstance(observed["js"]["value"], list)
    if kind in {"array", "object"}:
        assert check["detail"].endswith(kind)


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("large", ["says", "evidence", "extra"])
def test_large_measured_results_keep_the_original_boolean_verdict(page, site, ok, large):
    session, _ = open_fixture(page, site)
    expression = "(() => { const result = {ok:" + str(ok).lower() + ",says:'measured',evidence:{ratio:0.29}}; "
    expression += {"says": "result.says = 'x'.repeat(2000000);",
                   "evidence": "result.evidence.message = 'x'.repeat(2000000);",
                   "extra": "result.unused = 'x'.repeat(2000000);"}[large] + " return result; })()"
    observed = session.probe({"js": expression})
    size = len(json.dumps(observed["js"]).encode())
    assert size < 1000
    [check] = verdict.page_checks({"js": "fixture"}, observed)
    assert check["ok"] is ok and check["evidence_type"] == "js"
    assert "[truncated " in check["value"]
    if large != "evidence":
        assert check["evidence"] == {"ratio": 0.29}
    if large != "says" and not ok:
        assert check["detail"] == "measured"


def test_source_omission_never_sends_a_partial_secret_or_path(page, site, monkeypatch):
    session, _ = open_fixture(page, site)
    secret = "SYNTHETIC-BROWSER-BOUNDARY-SECRET-ABCDEFGHIJKLMNOP"
    monkeypatch.setattr(config, "_REMEMBERED", {secret})
    observed = session.probe({"js": "'x'.repeat(1048570) + " + json.dumps(secret) +
                              " + '/Users/BrowserPerson/private.js' + 'z'.repeat(1000)"})
    raw = observed["js"]["value"]
    size = len(raw)
    assert size < 200
    assert raw.startswith("[truncated ")
    assert "SYNTHETIC" not in raw and "BrowserPerson" not in raw


def test_source_limit_does_not_weaken_boolean_or_shape_rules(page, site):
    session, _ = open_fixture(page, site)
    for expression in ["({ok:'true',says:'x'.repeat(2000000),evidence:{}})",
                       "({ok:true,says:'measured',evidence:['x'.repeat(2000000)]})",
                       "({ok:true,says:17,evidence:{message:'x'.repeat(2000000)}})",
                       "({ok:true,says:'x'.repeat(2000000),evidence:null})",
                       "({ok:true,says:'measured',evidence:'x'.repeat(2000000)})"]:
        observed = session.probe({"js": expression})
        size = len(json.dumps(observed["js"]).encode())
        assert size < 1000
        [check] = verdict.page_checks({"js": "fixture"}, observed)
        assert check["ok"] is False and "evidence" not in check
        assert "ok" not in observed["js"]["value"]


@pytest.mark.parametrize("name", ["TypeError", "ReferenceError", "SyntaxError", "Uncaught TypeError", "Error"])
def test_source_omission_preserves_check_error_classification(page, site, name):
    session, _ = open_fixture(page, site)
    expression = "(() => { throw new Error(" + json.dumps(name + ": ") + " + 'x'.repeat(2000000)); })()"
    observed = session.probe({"js": expression})
    [check] = verdict.page_checks({"js": "fixture"}, observed)
    expected = name.removeprefix("Uncaught ") + ": [truncated " if name != "Error" else "[truncated "
    assert observed["js"]["error"].startswith(expected)
    assert bool(check.get("broke")) is (name != "Error")


def test_measured_result_is_bounded_when_multiple_fields_together_exceed_the_cap(page, site):
    session, _ = open_fixture(page, site)
    observed = session.probe({"js": "({ok:true,says:'x'.repeat(700000),evidence:{message:'x'.repeat(700000)}})"})
    size = len(json.dumps(observed["js"]).encode())
    assert size < 1000
    [check] = verdict.page_checks({"js": "fixture"}, observed)
    assert check["ok"] is True
    assert "[truncated " in observed["js"]["value"]["says"]
    assert "_qajev_source_truncated" in check["evidence"]
