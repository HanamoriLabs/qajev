"""site/install.sh, the one-line installer on qajev.com (new-user test, 11 Oct 2026): a machine with no uv got
"command not found: uv" from the old install line. It runs under dash (strict POSIX sh) with stand-in uname, curl and
uv on PATH and a throwaway HOME, so nothing real is downloaded or installed."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "site" / "install.sh"
SH = shutil.which("dash") or "/bin/sh"

# The stand-ins log every call to $LOG. uv's own installer (what curl returns for astral.sh) makes a uv that logs its
# arguments and, on "tool install", puts a qajev in ~/.local/bin that prints its version.
FAKE_UV = """#!/bin/sh
echo "uv $*" >> "$LOG"
case "$1 $2" in
  "tool install") mkdir -p "$HOME/.local/bin"; printf '#!/bin/sh\\necho "qajev 0.4.8"\\n' > "$HOME/.local/bin/qajev"
                  chmod +x "$HOME/.local/bin/qajev" ;;
  "tool dir") echo "$HOME/.local/bin" ;;
esac
"""
UV_INSTALLER = (
    "mkdir -p \"$HOME/.local/bin\"; cat > \"$HOME/.local/bin/uv\" <<'UV'\n" + FAKE_UV + "UV\n"
    "chmod +x \"$HOME/.local/bin/uv\"\n"
    "echo \"uv-installer UV_NO_MODIFY_PATH=${UV_NO_MODIFY_PATH:-}\" >> \"$LOG\"\n"
)
FAKE_CURL = """#!/bin/sh
echo "curl $*" >> "$LOG"
for a in "$@"; do last="$a"; done
case "$last" in
  https://astral.sh/uv/install.sh) cat "$UV_INSTALLER_FILE" ;;
  *) exit 22 ;;
esac
"""


def tool(bin_dir, name, body):
    p = bin_dir / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def install(tmp_path, *, system="Linux", proc_version="Linux version 6.8", uv_present=False, chrome=True, env=None):
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("")
    (tmp_path / "uv-installer.sh").write_text(UV_INSTALLER)
    tool(bin_dir, "uname", f'#!/bin/sh\ncase "$1" in -m) echo x86_64 ;; *) echo {system} ;; esac\n')
    tool(bin_dir, "curl", FAKE_CURL)
    if uv_present:
        tool(bin_dir, "uv", FAKE_UV)
    if chrome:
        tool(bin_dir, "chromium", "#!/bin/sh\nexit 0\n")
    (tmp_path / "proc_version").write_text(proc_version)
    keep = "/usr/bin:/bin"  # sed, grep, mkdir, cat and the like; no uv, curl or chrome lives there on a test machine
    run_env = {"HOME": str(home), "PATH": f"{bin_dir}:{keep}", "LOG": str(log),
               "UV_INSTALLER_FILE": str(tmp_path / "uv-installer.sh"),
               "QAJEV_PROC_VERSION": str(tmp_path / "proc_version"), **(env or {})}
    out = subprocess.run([SH, str(SCRIPT)], capture_output=True, text=True, env=run_env, timeout=30)
    return out, log.read_text().splitlines()


def test_a_machine_with_no_uv_gets_uv_then_qajev_on_its_path(tmp_path):
    out, calls = install(tmp_path)
    assert out.returncode == 0, out.stderr
    assert "curl -LsSf https://astral.sh/uv/install.sh" in calls
    assert "uv tool install --force git+https://github.com/hanamorilabs/qajev" in calls
    assert "uv tool update-shell" in calls
    assert "installed: qajev 0.4.8" in out.stdout
    assert "Linux" in out.stdout


def test_uv_already_there_is_used_and_not_downloaded(tmp_path):
    out, calls = install(tmp_path, uv_present=True)
    assert out.returncode == 0, out.stderr
    assert not [c for c in calls if c.startswith("curl")]
    assert "uv tool install --force git+https://github.com/hanamorilabs/qajev" in calls


def test_a_chosen_release_is_installed(tmp_path):
    out, calls = install(tmp_path, uv_present=True, env={"QAJEV_VERSION": "v0.4.8"})
    assert out.returncode == 0, out.stderr
    assert "uv tool install --force git+https://github.com/hanamorilabs/qajev@v0.4.8" in calls


def test_no_modify_path_leaves_the_shell_files_alone(tmp_path):
    out, calls = install(tmp_path, env={"QAJEV_NO_MODIFY_PATH": "1"})
    assert out.returncode == 0, out.stderr
    assert "uv-installer UV_NO_MODIFY_PATH=1" in calls
    assert "uv tool update-shell" not in calls
    assert "export PATH=" in out.stdout  # says how to reach qajev instead


def test_mac_is_named_and_installs(tmp_path):
    out, calls = install(tmp_path, system="Darwin")
    assert out.returncode == 0, out.stderr
    assert "macOS" in out.stdout
    assert "uv tool install --force git+https://github.com/hanamorilabs/qajev" in calls


def test_wsl_is_named_and_installs_as_linux(tmp_path):
    out, calls = install(tmp_path, proc_version="Linux version 5.15.153.1-microsoft-standard-WSL2")
    assert out.returncode == 0, out.stderr
    assert "WSL" in out.stdout
    assert "uv tool install --force git+https://github.com/hanamorilabs/qajev" in calls


@pytest.mark.parametrize("system", ["MINGW64_NT-10.0-22631", "MSYS_NT-10.0", "CYGWIN_NT-10.0"])
def test_a_windows_shell_is_sent_to_powershell_and_installs_nothing(tmp_path, system):
    out, calls = install(tmp_path, system=system)
    assert out.returncode != 0
    assert "irm https://qajev.com/install.ps1 | iex" in out.stderr
    assert not [c for c in calls if c.startswith(("curl", "uv"))]


def test_an_unknown_system_is_refused_by_name(tmp_path):
    out, calls = install(tmp_path, system="SunOS")
    assert out.returncode != 0
    assert "SunOS" in out.stderr
    assert not calls


def test_no_chrome_is_a_warning_with_the_way_to_get_it(tmp_path):
    out, calls = install(tmp_path, chrome=False)
    assert out.returncode == 0, out.stderr
    assert "no Chrome or Chromium" in out.stdout
    assert "chromium" in out.stdout  # the package to install


def test_it_is_strict_posix_sh():
    if not shutil.which("shellcheck"):
        pytest.skip("needs shellcheck")
    out = subprocess.run(["shellcheck", "-s", "sh", str(SCRIPT)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout


def test_the_site_serves_the_installer_as_text():
    headers = (SCRIPT.parent / "_headers").read_text()
    for name in ("/install.sh", "/install.ps1"):
        block = headers.split(name, 1)[1].split("\n/", 1)[0]
        assert "Content-Type: text/plain; charset=utf-8" in block, name
    assert os.access(SCRIPT, os.R_OK)
