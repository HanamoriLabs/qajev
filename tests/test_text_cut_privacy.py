"""Generated diagnostics are cleaned before cuts; values used by checks remain exact."""
import types

import pytest

from qajev import mobile, openurl, session, verdict

OWNER = "hiddenfolderowner"
PATHS = (
    f"/Users/{OWNER}/Development/shop/qa.yaml",
    f"/home/{OWNER}/Development/shop/qa.yaml",
    f"C:\\Users\\{OWNER}\\Development\\shop\\qa.yaml",
    f'`"/Users/{OWNER}/Acer SSD/shop/qa.yaml"`',
)
REASON = "the diagnostic still explains the failure"


def output_at_cut(path, position, keep):
    suffix = " | " + "=" * (keep - len(path) + position - len(REASON) - 3) + REASON
    return "earlier output | " + path + suffix


def assert_private(text):
    assert all(OWNER[i:i + 4] not in text for i in range(len(OWNER) - 3)), text
    assert all(OWNER[-size:] + separator not in text
               for size in range(1, len(OWNER) + 1) for separator in ("/", "\\")), text
    assert REASON in text


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
def test_command_diagnostics_hide_home_paths_at_every_outer_cut_without_changing_matching(monkeypatch, channel):
    capture = session.Session.__new__(session.Session)
    capture.allow_commands, capture.cwd = True, None
    response = types.SimpleNamespace(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(session.subprocess, "run", lambda *args, **kwargs: response)
    for path in PATHS:
        for position in range(len(path) + 1):
            text = output_at_cut(path, position, 4000)
            setattr(response, channel, text)
            result = capture.command("fixture command", "https://example.test")
            assert result[channel] == text[-4000:], "Keep the command's existing semantic output contract"
            assert_private(result.get("diagnostic_" + channel, result[channel]))
            detail = verdict.command_check({"run": "fixture command"}, result)["detail"]
            assert_private(detail)
    response.returncode = 0
    response.stdout = PATHS[0]
    response.stderr = ""
    result = capture.command("fixture command", "https://example.test")
    assert verdict.command_check({"run": "fixture command", "stdout": PATHS[0]}, result)["ok"]


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
def test_command_reason_hides_home_paths_at_every_inner_cut(channel):
    for path in PATHS:
        for position in range(len(path) + 1):
            text = output_at_cut(path, position, verdict.COMMAND_KEEP)
            detail = verdict.command_check({"run": "fixture command"}, {"exit": 1, channel: text})["detail"]
            assert_private(detail)


def test_command_hook_uses_the_safe_complete_diagnostic_before_its_smaller_cut(monkeypatch):
    capture = session.Session.__new__(session.Session)
    capture.allow_commands, capture.cwd = True, None
    capture.observe = lambda: None
    text = output_at_cut(PATHS[0], 9, 300)
    monkeypatch.setattr(session.subprocess, "run", lambda *args, **kwargs:
                        types.SimpleNamespace(returncode=1, stdout="", stderr=text))
    with pytest.raises(session.HookFailed) as error:
        capture.run_hook({"command": "fixture command"}, "https://example.test")
    assert_private(str(error.value))


def test_open_url_child_output_hides_home_paths_at_every_cut(monkeypatch):
    response = types.SimpleNamespace(returncode=3, stdout="", stderr="")
    monkeypatch.setattr(openurl.subprocess, "run", lambda *args, **kwargs: response)
    for path in PATHS:
        for position in range(len(path) + 1):
            response.stderr = output_at_cut(path, position, 2000)
            code, diagnostic = openurl._run(["fixture", "--expect-url", "/dashboard"])
            assert code == 3
            assert_private(diagnostic)


def test_mobile_launch_failure_hides_home_paths_at_every_cut(monkeypatch):
    app = mobile.MobileApp.__new__(mobile.MobileApp)
    app.app = "fixture.app"
    text = ""
    app._adb = lambda *args, **kwargs: text if "monkey" in args else ""
    for path in PATHS:
        for position in range(len(path) + 1):
            text = output_at_cut(path, position, 300)
            clock = iter((0.0, 1.0))
            monkeypatch.setattr(mobile.time, "monotonic", lambda: next(clock))
            with pytest.raises(mobile.NativeError) as error:
                app._launch_android(wait=0)
            assert_private(str(error.value))


@pytest.mark.parametrize('prefix', ['', 'é' * (len('earlier output | ') + len('/Users/') + 2) + '\n'])
def test_dashboard_start_failure_hides_home_paths_before_json_or_wrapper_record(monkeypatch, tmp_path, capsys, prefix):
    import json
    import subprocess

    from qajev import cli, dashboard

    state = tmp_path / 'dashboard.json'
    monkeypatch.setattr(dashboard, 'STATE', state)
    monkeypatch.setattr(dashboard, 'running', lambda: None)
    monkeypatch.setattr(dashboard, 'wait_running', lambda **kwargs: None)
    monkeypatch.setattr(cli, '_job_dir', None)
    args = types.SimpleNamespace(command='dashboard', new_key=False, stop=False, dash_background=True,
                                 json=True, open=False, port=8877)
    text = ''

    def child(*args, **kwargs):
        kwargs['stdout'].write(text)
        kwargs['stdout'].flush()
        return types.SimpleNamespace(poll=lambda: 1)

    monkeypatch.setattr(subprocess, 'Popen', child)
    for path in PATHS:
        for position in range(len(path) + 1):
            state.with_suffix('.log').write_text(prefix)
            text = output_at_cut(path, position, 600)
            assert cli.cmd_dashboard(args) != 0
            reason = json.loads(capsys.readouterr().out)['error']
            assert_private(reason)


@pytest.mark.parametrize('stdout', ['', 'not-json'])
def test_nightly_failure_hides_home_paths_before_digest_or_wrapper_record(monkeypatch, stdout):
    from qajev import nightly

    response = types.SimpleNamespace(returncode=1, stdout=stdout, stderr='')
    monkeypatch.setattr(nightly.subprocess, 'run', lambda *args, **kwargs: response)
    for path in PATHS:
        for position in range(len(path) + 1):
            response.stderr = output_at_cut(path, position, 300)
            assert_private(nightly._run(['fixture'])['error'])



def test_stdout_expectation_mismatch_uses_safe_head_of_long_output_at_every_cut(monkeypatch):
    capture = session.Session.__new__(session.Session)
    capture.allow_commands, capture.cwd = True, None
    response = types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(session.subprocess, "run", lambda *args, **kwargs: response)
    wanted = "fixture expected marker never printed"
    for path in PATHS:
        for position in range(len(path) + 1):
            raw = output_at_cut(path, position, 4000)
            assert len(raw) > 4000
            assert wanted not in raw
            response.stdout = raw
            result = capture.command("fixture command", "https://example.test")
            assert result["exit"] == 0, "Exercise stdout mismatch, not a failed command exit"
            assert result["stdout"] == raw[-4000:], "Keep the original matching copy unchanged"
            check = verdict.command_check({"run": "fixture command", "stdout": wanted}, result)
            assert check["ok"] is False
            assert check["detail"].startswith("stdout ")
            assert f"lacks {wanted!r}" in check["detail"]
            text = str(check)
            assert all(OWNER[i:i + 4] not in text for i in range(len(OWNER) - 3)), text
            assert all(OWNER[-size:] + separator not in text
                       for size in range(1, len(OWNER) + 1) for separator in ("/", "\\")), text
