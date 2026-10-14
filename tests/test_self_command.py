"""QAJev's own child processes run the installed QAJev, never a qajev/ folder in their working folder (qajev2's MCP
server ran an old checkout's 0.1.0 because its session started in that checkout, 14 Oct)."""

import re
import subprocess
import sys
from pathlib import Path

import qajev
from qajev import config


def _old_checkout(tmp_path):
    old = tmp_path / "qajev"
    old.mkdir()
    (old / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (old / "__main__.py").write_text('print("qajev 0.1.0")\n')
    return tmp_path


def test_a_qajev_child_started_in_an_old_checkout_runs_the_installed_qajev(tmp_path):
    folder = _old_checkout(tmp_path)
    child = subprocess.run(config.qajev_argv("--version"), cwd=folder, capture_output=True, text=True, timeout=60)
    assert child.stdout.strip() == f"qajev {qajev.__version__}", child.stderr
    plain = subprocess.run([sys.executable, "-m", "qajev", "--version"], cwd=folder, capture_output=True, text=True,
                           timeout=60)
    assert plain.stdout.strip() == "qajev 0.1.0"  # the bug, without -P: the folder's package wins


def test_every_qajev_child_command_goes_through_qajev_argv():
    """mcp_server, jobs, nightly, the dashboard and open_url: none builds its own `python -m qajev`."""
    root = Path(qajev.__file__).parent
    own = [f"{p.name}:{n}" for p in sorted(root.glob("*.py")) if p.name != "config.py"
           for n, line in enumerate(p.read_text().splitlines(), 1) if re.search(r'"-m",\s*"qajev"', line)]
    assert own == []
