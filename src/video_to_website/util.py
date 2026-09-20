"""Small shared helpers: logging, subprocess, slugs, time formatting, stage caching."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, NoReturn, Sequence

from . import __version__

VIDEO_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".avi",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".ts",
    ".wmv",
    ".flv",
}

_USE_COLOR = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


class CommandError(RuntimeError):
    """A subprocess exited non-zero."""


class BuildCancelled(RuntimeError):
    """The requested source revision is no longer wanted."""


_process_check = contextvars.ContextVar("v2w_process_check", default=None)


@contextlib.contextmanager
def process_control(check):
    token = _process_check.set(check)
    try:
        yield
    finally:
        _process_check.reset(token)


def digest_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_write(path: Path, text: str) -> None:
    """Publish a complete file; distinct writers never share a temporary path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def file_lock(path: Path, *, blocking: bool = True):
    """Coordinate local processes on the supported Linux/macOS deployments."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def log(msg: str) -> None:
    print(f"{_paint('v2w', '36')} {msg}", file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(f"{_paint('v2w warning', '33')} {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> NoReturn:
    print(f"{_paint('v2w error', '31')} {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def which_or_die(binary: str, hint: str) -> str:
    found = shutil.which(binary)
    if not found:
        die(f"'{binary}' was not found on PATH. {hint}")
    return found


def run(
    cmd: Sequence[str],
    *,
    capture_stdout: bool = True,
    capture_stderr: bool = True,
    check: bool = True,
    cwd: Path | None = None,
    text: bool = True,
    timeout: float = 7200,
) -> subprocess.CompletedProcess:
    """Run a command. stderr is captured by default; pass capture_stderr=False to stream it."""
    check_cancelled = _process_check.get()
    if check_cancelled:
        check_cancelled()
    process = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE if capture_stdout else None,
        stderr=subprocess.PIPE if capture_stderr else None,
        stdin=subprocess.DEVNULL,
        text=text,
        errors="replace" if text else None,
        cwd=str(cwd) if cwd else None,
        start_new_session=True,
    )
    started = time.monotonic()
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if check_cancelled:
                    check_cancelled()
                if time.monotonic() - started > timeout:
                    raise CommandError(f"{cmd[0]} exceeded its {timeout:g}s timeout")
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise
    proc = subprocess.CompletedProcess(list(cmd), process.returncode, stdout, stderr)
    if check and proc.returncode != 0:
        error = proc.stderr or ""
        if isinstance(error, bytes):
            error = error.decode(errors="replace")
        tail = error.strip().splitlines()[-12:]
        detail = "\n  ".join(tail)
        raise CommandError(
            f"{cmd[0]} exited {proc.returncode}\n  command: {' '.join(cmd)}"
            + (f"\n  {detail}" if detail else "")
        )
    return proc


def slugify(text: str, *, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return (text[:max_len].rstrip("-")) or "untitled"


_NUM_RE = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """Sort key so 'lesson 2' comes before 'lesson 10'."""
    return tuple(
        int(part) if part.isdigit() else part.lower() for part in _NUM_RE.split(text)
    )


def hms(seconds: float) -> str:
    """Format seconds as m:ss, or h:mm:ss past an hour."""
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def title_from_filename(path: Path) -> str:
    """Best-effort human title: strip ordering prefixes and separators."""
    stem = path.stem
    stem = re.sub(r"^[\s\-_.]*\d{1,3}[\s\-_.]+", "", stem)
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"\s*-\s*", " - ", stem)
    stem = re.sub(r"\s{2,}", " ", stem).strip(" -")
    return stem or path.stem


def file_fingerprint(path: Path) -> dict[str, Any]:
    """Identify a video by its contents, not by where it sits.

    Deliberately no path: the cache already lives in a directory of its own per
    lesson, so the path adds nothing, and including it threw away hours of
    transcription every time a course folder was renamed or moved.
    """
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def read_stage(cache_path: Path, fingerprint: dict, params: dict) -> Any | None:
    """Return cached stage output if it matches the source file and stage params."""
    if not cache_path.exists():
        return None
    try:
        blob = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if blob.get("tool_version") != __version__:
        return None
    if blob.get("params") != params:
        return None
    # Compared key by key rather than whole: a cache written by an older build
    # carries fields this one no longer asks about, and is still valid.
    stored = blob.get("fingerprint")
    if not isinstance(stored, dict) or any(stored.get(k) != v for k, v in fingerprint.items()):
        return None
    return blob.get("data")


def write_stage(cache_path: Path, fingerprint: dict, params: dict, data: Any) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tool_version": __version__,
        "fingerprint": fingerprint,
        "params": params,
        "data": data,
    }
    atomic_write(cache_path, json.dumps(payload, indent=2))


def find_videos(root: Path) -> list[Path]:
    """All video files under root, in natural filename order."""
    if root.is_file():
        return [root] if root.suffix.lower() in VIDEO_SUFFIXES else []
    found = [
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
        and not any(part.startswith(".") for part in p.relative_to(root).parts)
    ]
    return sorted(found, key=lambda p: natural_key(str(p.relative_to(root))))
