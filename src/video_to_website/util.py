"""Small shared helpers: logging, subprocess, slugs, time formatting, stage caching."""

from __future__ import annotations

import codecs
import contextlib
import contextvars
import hashlib
import json
import os
import re
import selectors
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import threading
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
        if hasattr(os, "fchmod"):
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
    """Coordinate local processes, including a helper's Windows cache."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if not blocking:
                        raise BlockingIOError("Another helper is using this state directory.") from exc
                    time.sleep(.1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
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
    on_stdout=None,
    on_stderr=None,
    tee_stdout: bool = False,
    tee_stderr: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command with optional live line callbacks.

    Without callbacks, uncaptured output inherits the terminal. With callbacks,
    tee_stdout/tee_stderr explicitly mirror that stream while it is drained.
    """
    check_cancelled = _process_check.get()
    if check_cancelled:
        check_cancelled()
    process = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE if capture_stdout or on_stdout else None,
        stderr=subprocess.PIPE if capture_stderr or on_stderr else None,
        stdin=subprocess.DEVNULL,
        text=text,
        errors="replace" if text else None,
        cwd=str(cwd) if cwd else None,
        start_new_session=True,
    )
    started = time.monotonic()
    try:
        if on_stdout or on_stderr:
            stdout, stderr = _communicate_live(process, capture_stdout, capture_stderr,
                                                on_stdout, on_stderr, text, timeout, check_cancelled,
                                                tee_stdout, tee_stderr)
        else:
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
        _stop_process(process)
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            _stop_process(process, force=True)
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


def _stop_process(process, *, force=False):
    with contextlib.suppress(ProcessLookupError):
        if os.name == "nt":
            # Native helpers invoke media binaries directly, without a shell.
            process.kill() if force else process.terminate()
        else:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)


def _communicate_live(process, capture_stdout, capture_stderr, on_stdout, on_stderr,
                      text, timeout, check_cancelled, tee_stdout, tee_stderr):
    """Drain both pipes on the calling thread, preserving its progress context.

    Reading only stderr can deadlock on a full stdout pipe. Nonblocking reads
    also keep cancellation responsive when a tool is silent or prints CR lines.
    """
    if os.name == "nt":
        return _communicate_windows(process, capture_stdout, capture_stderr, on_stdout, on_stderr,
                                    text, timeout, check_cancelled, tee_stdout, tee_stderr)
    started = time.monotonic()
    streams = []
    with selectors.DefaultSelector() as selector:
        for pipe, capture, callback, echo in ((process.stdout, capture_stdout, on_stdout, sys.stdout if tee_stdout else None),
                                               (process.stderr, capture_stderr, on_stderr, sys.stderr if tee_stderr else None)):
            state = {"chunks": [], "capture": capture, "callback": callback, "echo": echo,
                     "decoder": codecs.getincrementaldecoder("utf-8")("replace"), "pending": ""}
            streams.append(state)
            if pipe is not None:
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, state)
        while selector.get_map() or process.poll() is None:
            if check_cancelled:
                check_cancelled()
            if time.monotonic() - started > timeout:
                raise CommandError(f"{process.args[0]} exceeded its {timeout:g}s timeout")
            for key, _ in selector.select(timeout=0.25):
                state = key.data
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                decoded = state["decoder"].decode(chunk, final=not chunk)
                if state["capture"]:
                    state["chunks"].append(chunk)
                if state["echo"] is not None and decoded:
                    state["echo"].write(decoded)
                    state["echo"].flush()
                if state["callback"]:
                    lines = re.split(r"[\r\n]", state["pending"] + decoded)
                    state["pending"] = lines.pop()
                    for line in lines:
                        if line:
                            state["callback"](line)
                if not chunk:
                    selector.unregister(key.fileobj)
                    if state["callback"] and state["pending"]:
                        state["callback"](state["pending"])
            if not selector.get_map() and process.poll() is None:
                time.sleep(0.05)
    process.wait()
    values = []
    for state in streams:
        value = b"".join(state["chunks"]) if state["capture"] else None
        if value is not None and text:
            value = value.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
        values.append(value)
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()
    return values


def _communicate_windows(process, capture_stdout, capture_stderr, on_stdout, on_stderr,
                         text, timeout, check_cancelled, tee_stdout, tee_stderr):
    """Windows selectors cannot wait on subprocess pipes. Callbacks stay here."""
    events, stopped = queue.Queue(maxsize=32), threading.Event()
    streams, threads = [], []

    def read(pipe, index):
        try:
            while not stopped.is_set():
                chunk = os.read(pipe.fileno(), 65536)
                while not stopped.is_set():
                    try:
                        events.put((index, chunk), timeout=.1)
                        break
                    except queue.Full:
                        continue
                if not chunk:
                    break
        except (OSError, ValueError):
            pass

    for index, (pipe, capture, callback, echo) in enumerate((
        (process.stdout, capture_stdout, on_stdout, sys.stdout if tee_stdout else None),
        (process.stderr, capture_stderr, on_stderr, sys.stderr if tee_stderr else None),
    )):
        streams.append(dict(chunks=[], capture=capture, callback=callback, echo=echo,
                            decoder=codecs.getincrementaldecoder("utf-8")("replace"), pending=""))
        if pipe:
            thread = threading.Thread(target=read, args=(pipe, index), daemon=True)
            thread.start()
            threads.append(thread)
    remaining, started = len(threads), time.monotonic()
    try:
        while remaining or process.poll() is None:
            if check_cancelled:
                check_cancelled()
            if time.monotonic() - started > timeout:
                raise CommandError(f"{process.args[0]} exceeded its {timeout:g}s timeout")
            try:
                index, chunk = events.get(timeout=.25)
            except queue.Empty:
                continue
            state = streams[index]
            decoded = state["decoder"].decode(chunk, final=not chunk)
            if state["capture"]:
                state["chunks"].append(chunk)
            if state["echo"] is not None and decoded:
                state["echo"].write(decoded)
                state["echo"].flush()
            if state["callback"]:
                lines = re.split(r"[\r\n]", state["pending"] + decoded)
                state["pending"] = lines.pop()
                for line in lines:
                    if line:
                        state["callback"](line)
            if not chunk:
                remaining -= 1
                if state["callback"] and state["pending"]:
                    state["callback"](state["pending"])
        process.wait()
    finally:
        stopped.set()
    for thread in threads:
        thread.join(timeout=.5)
    for pipe in (process.stdout, process.stderr):
        if pipe:
            pipe.close()
    values = [b"".join(s["chunks"]) if s["capture"] else None for s in streams]
    return [v.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
            if text and v is not None else v for v in values]


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
