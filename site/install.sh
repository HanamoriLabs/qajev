#!/bin/sh
# QAJev installer for macOS and Linux, and Windows through WSL (https://qajev.com/install.sh):
#
#   curl -LsSf https://qajev.com/install.sh | sh
#
# It installs uv (https://docs.astral.sh/uv/, which brings its own Python) only when uv is missing, then QAJev as a
# uv tool, then puts QAJev on your PATH, and checks for Chrome. Running it again updates QAJev.
#
# Options, as environment variables:
#   QAJEV_VERSION=v0.4.8     install that release tag (default: the latest code on main)
#   QAJEV_NO_MODIFY_PATH=1   change no shell file; it prints the PATH line to add yourself
#
# On Windows, use PowerShell instead: irm https://qajev.com/install.ps1 | iex
set -eu

REPO="https://github.com/hanamorilabs/qajev"

say() { printf 'qajev-install: %s\n' "$*"; }
fail() { printf 'qajev-install: error: %s\n' "$*" >&2; exit 1; }

# --- Which system is this?
os=$(uname -s 2>/dev/null || echo unknown)
case "$os" in
  Darwin) platform="macOS" ;;
  Linux)
    # WSL reports Linux; its kernel string names Microsoft. (QAJEV_PROC_VERSION: for the installer's own tests.)
    if grep -qi microsoft "${QAJEV_PROC_VERSION:-/proc/version}" 2>/dev/null; then platform="Linux (WSL)"; else platform="Linux"; fi ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    fail "this is a Windows shell. QAJev runs on Windows through WSL. In PowerShell, run: irm https://qajev.com/install.ps1 | iex" ;;
  *)
    fail "QAJev runs on macOS and Linux, and on Windows through WSL. This system says it is '$os'." ;;
esac
say "installing QAJev on $platform ($(uname -m 2>/dev/null || echo unknown))"

download() {
  if command -v curl >/dev/null 2>&1; then curl -LsSf "$1"
  elif command -v wget >/dev/null 2>&1; then wget -qO- "$1"
  else fail "needs curl or wget to download uv"
  fi
}

find_uv() {
  for candidate in "${UV_INSTALL_DIR:-}/uv" "${XDG_BIN_HOME:-}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then echo "$candidate"; return 0; fi
  done
  return 1
}

# --- uv: use the one there is, else install it
uv=$(command -v uv 2>/dev/null || find_uv || true)
if [ -n "$uv" ]; then
  say "uv found: $uv"
else
  say "uv is not installed: installing it from https://astral.sh/uv"
  if [ "${QAJEV_NO_MODIFY_PATH:-}" = 1 ]; then
    download https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  else
    download https://astral.sh/uv/install.sh | sh
  fi
  uv=$(find_uv) || fail "uv's installer finished, but uv is not where it should be. See https://docs.astral.sh/uv/getting-started/installation/"
fi

# --- QAJev
source_url="git+$REPO"
if [ -n "${QAJEV_VERSION:-}" ]; then source_url="$source_url@$QAJEV_VERSION"; fi
say "installing QAJev from $source_url"
"$uv" tool install --force "$source_url" || fail "uv could not install QAJev (see the lines above)"
if [ "${QAJEV_NO_MODIFY_PATH:-}" != 1 ]; then
  "$uv" tool update-shell >/dev/null 2>&1 || true
fi
bin_dir=$("$uv" tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")
version=$("$bin_dir/qajev" --version 2>/dev/null) || fail "QAJev did not start after installing: run $bin_dir/qajev --version to see why"
say "installed: $version"

# --- Chrome: QAJev drives a real Chrome or Chromium
chrome=""
if [ -n "${QAJEV_CHROME:-}" ] && [ -x "$QAJEV_CHROME" ]; then chrome=$QAJEV_CHROME; fi
for name in google-chrome google-chrome-stable chromium chromium-browser; do
  if [ -z "$chrome" ] && command -v "$name" >/dev/null 2>&1; then chrome=$(command -v "$name"); fi
done
if [ -z "$chrome" ] && [ "$os" = Darwin ]; then
  for app in "/Applications/Google Chrome.app" "$HOME/Applications/Google Chrome.app" "/Applications/Chromium.app"; do
    if [ -d "$app" ]; then chrome=$app; break; fi
  done
fi
if [ -n "$chrome" ]; then
  say "Chrome found: $chrome"
elif [ "$platform" = macOS ]; then
  say "warning: no Chrome or Chromium found. QAJev needs one: get Google Chrome from https://www.google.com/chrome/ (or: brew install --cask google-chrome)"
else
  say "warning: no Chrome or Chromium found. QAJev needs one: install chromium with your package manager (sudo apt install chromium, sudo dnf install chromium or sudo pacman -S chromium)"
fi

# --- What next
if command -v qajev >/dev/null 2>&1; then
  say "done. Next: qajev doctor"
elif [ "${QAJEV_NO_MODIFY_PATH:-}" = 1 ]; then
  say "done. Add QAJev to your PATH: export PATH=\"$bin_dir:\$PATH\""
  say "then: qajev doctor"
else
  say "done. Open a new terminal (or run: export PATH=\"$bin_dir:\$PATH\"), then: qajev doctor"
fi
