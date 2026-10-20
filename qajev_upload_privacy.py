"""Portable path-disclosure policy shared by CLI and dispatcher upload boundaries."""

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

MASK = "[local path]"
_WEB = re.compile(r"(?:https?|wss?)://[^\s<>\"'`]+", re.I)
_WEB_ROOT = re.compile(r"((?:\bfetch\s*\(\s*|\burl\s*\(\s*|\b(?:href|src|action)\s*=\s*)[\"'])(/[^\"']*)([\"'])", re.I)
_WEB_FIELD = re.compile(
    r"(\b(?:[a-z0-9_]*url|href|src|action|smoke_start)\s*[:=]\s*[\"']?)(/[^\s,{}\[\]\"'<>]+)", re.I)
_DATA = re.compile(r"(data:[a-z0-9.+/-]+(?:;charset=[a-z0-9_-]+)?;base64,)([a-z0-9+/=]+)", re.I)
_FILE = re.compile(r"\bfile://[^\r\n<>\"'`]+", re.I)
_CLOSE_TAG = re.compile(r"</[a-z][a-z0-9_.:-]*\s*>", re.I)
_PATH = re.compile(
    r"(?<![\w/\\])(?:[a-z]:[\\/]+|\\\\|(?<!\.)/{2,}|(?<!\.)(?:/|\\/|%2f)(?![/\*^])|~[\\/])"
    r"[\w.~%][^\x00-\x1f<>\"'`|:;#?(){}\[\]]*", re.I)
_BINARY_PATH = re.compile(
    rb"(?:[/\\](?:users|home)[/\\][a-z0-9._ -]+|[a-z]:[\\/]+[a-z0-9._ -]+[\\/][a-z0-9._ -]+|file://)", re.I)
_URL_KEYS = {"url", "base_url", "href", "src", "action"}


_HOME_COMPONENT = re.compile(r"(?:users|home[0-9]*|documents and settings)")

def _repository_label(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 2000 and not value.startswith(".")
            and not re.search(r"[\x00-\x1f\x7f-\x9f/\\:@?#]", value))

def _home_repo_path(value):
    """Reject home locations with the same Unicode and component rules as the API."""
    path = value
    for _ in range(8):
        path = unicodedata.normalize("NFKC", path)
        if re.search(r"%(?![0-9a-fA-F]{2})", path):
            return True
        try:
            decoded = unquote(path, errors="strict")
        except UnicodeError:
            return True
        if decoded == path:
            break
        path = decoded
    else:
        return True
    if re.search(r"[\x00-\x1f\x7f-\x9f\\]", path) or any(p.startswith(".") for p in path.split("/")):
        return True
    path = re.sub(r"/+", "/", path).lower()
    path = re.sub(r"^/?[a-z]:", "", path)
    parts = [part for part in path.split("/") if part]
    if (path.lstrip("/").startswith("~") or path.startswith("/root")
            or any(_HOME_COMPONENT.fullmatch(part)
                   and (index + 1 < len(parts) or (index == 0 and path.startswith("/")))
                   for index, part in enumerate(parts))):
        return True
    try:
        home = unicodedata.normalize("NFKC", Path.home().as_posix()).casefold().rstrip("/") or "/"
    except (OSError, RuntimeError):
        home = ""
    local_path = path.casefold()
    return (local_path.startswith("/") if home == "/"
            else bool(home and (local_path == home or local_path.startswith(home + "/"))))

def _network_repo(value):
    """A network repository identifier, without user info, query or fragment. Never a filesystem path."""
    if not isinstance(value, str) or not value or re.search(r"[\x00-\x1f\x7f-\x9f\\]", value) or value.startswith("//"):
        return None
    if value.startswith("/") or re.match(r"^[a-z]:", value, re.I):
        return None
    if re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
        try:
            parts = urlsplit(value)
            host, port = parts.hostname, parts.port
        except ValueError:
            return None
        if parts.scheme not in {"http", "https", "ssh", "git"} or not host or re.search(r"[\s/@%?#\\]", host):
            return None
        if _home_repo_path(parts.path):
            return None
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            if port < 1:
                return None
            authority += f":{port}"
        repo = urlunsplit((parts.scheme, authority, parts.path, "", ""))
        return repo if len(repo) <= 2000 else None
    # Match the API's supported SCP-like form, but never interpret a broken URL as an SCP path.
    if re.match(r"^(?:https?|ssh|git|file|ftps?|wss?):", value, re.I):
        return None
    match = re.fullmatch(r"(?:[^/@:\s]+@)?([a-z0-9.-]+):(.+)", value, re.I)
    if match:
        path = re.split(r"[?#]", match[2], maxsplit=1)[0]
        if path and "@" not in path and not path.startswith("//") and not _home_repo_path(path):
            repo = f"{match[1]}:{path}"
            return repo if len(repo) <= 2000 else None
    return None


def _relative_path(match, paths=None):
    original = unquote(match[0]).strip()
    path = original
    path = re.sub(r"\\+/|\\+", "/", path)
    if re.match(r"^[a-z]:", path, re.I):
        path = path.casefold()
    public = "local/" + hashlib.sha256(path.encode()).hexdigest()[:24]
    if paths is not None:
        if public in paths and paths[public] != original:
            raise ValueError("outgoing local references would collide")
        paths[public] = original
    return public


def _text(value, paths=None):
    protected = {}
    prefix = "\x00public-url-"
    while prefix in value:
        prefix += "-"

    def keep(text):
        token = f"{prefix}{len(protected)}\x00"
        protected[token] = text
        return token

    def media(match):
        try:
            raw = base64.b64decode(match[2], validate=True)
        except ValueError:
            raise ValueError("outgoing embedded data is not valid base64") from None
        clean = sanitize_upload(raw, paths=paths)
        return keep(match[0] if clean == raw else match[1] + base64.b64encode(clean).decode())

    def web(match):
        return keep(match[0])

    def web_root(match):
        return match[1] + keep(match[2]) + match[3]

    def web_field(match):
        return match[1] + keep(match[2])

    value = _DATA.sub(media, value)
    value = _FILE.sub(MASK, value)
    value = _WEB.sub(web, value)
    value = _WEB_ROOT.sub(web_root, value)
    value = _WEB_FIELD.sub(web_field, value)
    value = _CLOSE_TAG.sub(web, value)
    value = _PATH.sub(lambda match: _relative_path(match, paths), value)
    for token, text in protected.items():
        value = value.replace(token, text)
    return value


def _tree(value, key=None, paths=None):
    if isinstance(value, str):
        if key in ("repo", "repository"):
            # Keep the existing repository contract, including public SCP paths, at this boundary too.
            return _network_repo(value) or (value if _repository_label(value) else MASK)
        # A root-relative web URL is not a local filesystem path (for example an app's sign-in route).
        url_key = isinstance(key, str) and (key in _URL_KEYS or key.endswith("_url") or key == "smoke_start")
        if url_key and value.startswith("/"):
            return value
        return _text(value, paths)
    if isinstance(value, list):
        return [_tree(item, paths=paths) for item in value]
    if isinstance(value, dict):
        result = {}
        for name, item in value.items():
            clean = _text(name, paths)
            if clean in result:
                raise ValueError("outgoing path redaction would collide JSON keys")
            result[clean] = _tree(item, name, paths)
        return result
    return value


def sanitize_upload(data: bytes, paths=None) -> bytes:
    """Return an idempotent public copy; leave safe bytes and local originals unchanged.

    Text and JSON paths are removed before hashes or transport. Opaque image/archive bytes stay exact;
    identifiable home/drive path metadata in binary data is refused instead of corrupting the artifact.
    """
    encoding, bom = "utf-8", b""
    if data.startswith(b"\xff\xfe"):
        encoding, bom = "utf-16-le", b"\xff\xfe"
    elif data.startswith(b"\xfe\xff"):
        encoding, bom = "utf-16-be", b"\xfe\xff"
    try:
        text = data[len(bom):].decode(encoding)
    except UnicodeError:
        if _BINARY_PATH.search(data):
            raise ValueError("outgoing binary artifact contains a local filesystem path") from None
        return data
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        clean = _text(text, paths)
    else:
        public = _tree(value, paths=paths)
        if public == value:
            return data
        clean = json.dumps(public, ensure_ascii=False, indent=2)
    return data if clean == text else bom + clean.encode(encoding)


def public_reference(value):
    """The same stable public string whether given a local path or an already public reference."""
    return json.loads(sanitize_upload(json.dumps(str(value)).encode()))


def seed_remote(file):
    """A fixture's public identifier, independent of the receiving machine's working folder."""
    reference = public_reference(file)
    return f"in/seed/{hashlib.sha256(reference.encode()).hexdigest()[:16]}.json"


_REF = re.compile(r"(?<![\w/])local/[0-9a-f]{24}(?![0-9a-f])")
_REGISTRY = "tunnel-paths.json"


def _registry_directory(directory):
    return Path(directory if directory is not None else os.environ.get("QAJEV_HOME") or Path.home() / ".qajev")


def _read_paths(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {}
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_size > 10 * 1024 * 1024):
            raise ValueError("the private tunnel reference registry is not a valid local file")
        with os.fdopen(fd, encoding="utf-8") as source:
            fd = -1
            data = json.load(source)
    finally:
        if fd != -1:
            os.close(fd)
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("paths"), dict):
        raise ValueError("the private tunnel reference registry has an invalid format")
    paths = data["paths"]
    for token, original in paths.items():
        if (not isinstance(token, str) or not _REF.fullmatch(token) or not isinstance(original, str)
                or public_reference(original) != token):
            raise ValueError("the private tunnel reference registry has an invalid entry")
    return paths


def remember_paths(paths, directory=None):
    """Keep tunnel locations on this computer only; never put this registry in an upload."""
    if not paths:
        return
    for token, original in paths.items():
        if (not isinstance(token, str) or not _REF.fullmatch(token) or not isinstance(original, str)
                or public_reference(original) != token):
            raise ValueError("a private tunnel reference is not valid")
    import fcntl

    folder = _registry_directory(directory)
    folder.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    lock = os.open(folder / "tunnel-paths.lock", flags, 0o600)
    temporary = None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = _read_paths(folder / _REGISTRY)
        for token, original in paths.items():
            if token in current and current[token] != original:
                raise ValueError("private tunnel references would collide")
        current.update(paths)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                         prefix=".tunnel-paths-", delete=False) as target:
            temporary = Path(target.name)
            os.fchmod(target.fileno(), 0o600)
            json.dump({"version": 1, "paths": current}, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, folder / _REGISTRY)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def restore_paths(value, directory=None):
    """Resolve registered local references before this computer's existing tunnel guard runs."""
    def contains(item):
        if isinstance(item, str):
            return bool(_REF.search(item))
        if isinstance(item, list):
            return any(contains(child) for child in item)
        if isinstance(item, dict):
            return any(contains(child) for child in item.values())
        return False

    if not contains(value):
        return value
    paths = _read_paths(_registry_directory(directory) / _REGISTRY)

    def restore(item):
        if isinstance(item, str):
            def replace(match):
                token = match[0]
                if token not in paths:
                    raise ValueError("the tunnel reference is not registered on this computer")
                return paths[token]

            return _REF.sub(replace, item)
        if isinstance(item, list):
            return [restore(child) for child in item]
        if isinstance(item, dict):
            return {name: restore(child) for name, child in item.items()}
        return item

    return restore(value)
