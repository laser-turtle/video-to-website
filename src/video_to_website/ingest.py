"""Accepting videos over HTTP, so a course can be added from a browser.

Deliberately thin: an upload lands in the library as an ordinary file and the
watcher picks it up on its next poll. The file appearing *is* the trigger, so
nothing here knows how to build anything.

Raw PUT rather than a multipart form. The body is then just the file, which
streams to disk in constant memory -- and the stdlib lost its multipart parser
when cgi was removed in 3.13, so parsing one would mean writing it here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlparse

from .util import VIDEO_SUFFIXES, log, warn

API_PREFIX = "/api/"
CHUNK = 1 << 20
# Generous: course videos run to a couple of GB and the point is not to be the
# thing that stops one landing.
MAX_UPLOAD = 16 * 1024**3
# Refuse an upload that would leave the box with less than this free.
DISK_MARGIN = 1024**3

_UNSAFE = re.compile(r"[^\w .\-]+")


def service_user() -> str:
    """Who this process is, for an error message that can be acted on."""
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError):
        return f"uid {os.geteuid()}"


def safe_component(name: str) -> str | None:
    """A single path component, or None if nothing usable survives.

    Everything arrives from a browser, so this is the boundary: no separators,
    no traversal, no leading dot to hide a file from the watcher.
    """
    name = unquote(name).strip().replace("\x00", "")
    if "/" in name or "\\" in name:
        return None
    name = _UNSAFE.sub("_", name).strip(" .")
    name = re.sub(r"\s{2,}", " ", name)
    if not name or name in (".", ".."):
        return None
    return name[:120]


def course_names(library: Path) -> list[str]:
    try:
        entries = sorted(p.name for p in library.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    return entries


class IngestMixin:
    """Serves /api/* on top of whatever else a handler already does."""

    protocol_version = "HTTP/1.1"

    # -- plumbing -----------------------------------------------------------

    @property
    def library(self) -> Path | None:
        return getattr(self.server, "library", None)

    def log_message(self, fmt, *args):
        """Quiet: an upload logs itself, and journald does not need the rest."""
        return

    def _denied(self, where: Path) -> str:
        """Why a write was refused, in terms of something to go and do.

        Worth the words: a course folder copied in over ssh belongs to root,
        the builder can read it but not write it, so the course builds happily
        and then every upload into it fails. '[Errno 13]' does not hint at any
        of that.
        """
        user = service_user()
        return (
            f"the builder runs as {user} and cannot write into {where.name}/. "
            f"That folder belongs to someone else -- usually a copy made as root. "
            f"On the server: chown -R {user} {self.library}"
        )

    def _json(self, code: int, payload: dict, *, close: bool = False) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # An error answered mid-upload leaves the client still sending, and
        # there is no way to resynchronise a kept-alive connection from here.
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    # -- routing ------------------------------------------------------------

    def api_get(self) -> bool:
        """Answer a GET if it is an API call. Returns whether it was."""
        path = urlparse(self.path).path
        if not path.startswith(API_PREFIX):
            return False
        if self.library is None:
            self._json(503, {"error": "uploads are not enabled here"})
        elif path == "/api/courses":
            self._json(200, {"courses": course_names(self.library)})
        else:
            self._json(404, {"error": "no such endpoint"})
        return True

    def do_PUT(self):
        path = urlparse(self.path).path
        if not path.startswith(API_PREFIX):
            self._json(405, {"error": "method not allowed"}, close=True)
            return
        if self.library is None:
            self._json(503, {"error": "uploads are not enabled here"}, close=True)
            return
        parts = path[len(API_PREFIX):].split("/")
        if len(parts) != 3 or parts[0] != "library":
            self._json(404, {"error": "no such endpoint"}, close=True)
            return
        self._store(parts[1], parts[2])

    # -- the upload itself --------------------------------------------------

    def _store(self, raw_course: str, raw_name: str) -> None:
        course = safe_component(raw_course)
        name = safe_component(raw_name)
        if not course or not name:
            self._json(400, {"error": "unusable course or file name"}, close=True)
            return
        if Path(name).suffix.lower() not in VIDEO_SUFFIXES:
            self._json(415, {"error": f"{Path(name).suffix or 'that'} is not a video file"}, close=True)
            return

        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(411, {"error": "a Content-Length is required"}, close=True)
            return
        if length <= 0 or length > MAX_UPLOAD:
            self._json(413, {"error": "file is empty or too large"}, close=True)
            return

        library = self.library
        target = library / course / name
        if target.exists() and "overwrite" not in urlparse(self.path).query:
            self._json(409, {"error": f"{course}/{name} is already there"}, close=True)
            return

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(target.parent).free
        except PermissionError:
            self._json(403, {"error": self._denied(target.parent)}, close=True)
            return
        except OSError as exc:
            self._json(500, {"error": f"cannot write to the library: {exc}"}, close=True)
            return
        if length + DISK_MARGIN > free:
            self._json(507, {"error": "not enough free space for that file"}, close=True)
            return

        # Hidden while it is arriving: find_videos skips dotfiles, so a partial
        # upload cannot start a build of itself.
        partial = target.parent / f".incoming-{name}"
        received = 0
        try:
            with partial.open("wb") as out:
                while received < length:
                    chunk = self.rfile.read(min(CHUNK, length - received))
                    if not chunk:
                        raise OSError("the connection closed before the file finished")
                    out.write(chunk)
                    received += len(chunk)
            partial.replace(target)
        except PermissionError:
            partial.unlink(missing_ok=True)
            warn(f"upload of {course}/{name} refused: {target.parent} is not writable by {service_user()}")
            self._json(403, {"error": self._denied(target.parent)}, close=True)
            return
        except OSError as exc:
            partial.unlink(missing_ok=True)
            warn(f"upload of {course}/{name} failed after {received} bytes: {exc}")
            self._json(500, {"error": str(exc)}, close=True)
            return

        log(f"uploaded {course}/{name} ({received / 1024**2:.0f} MiB)")
        self._json(201, {"course": course, "name": name, "bytes": received})


class IngestHandler(IngestMixin, BaseHTTPRequestHandler):
    """The API on its own, for the port nginx proxies to."""

    def do_GET(self):
        if not self.api_get():
            self._json(404, {"error": "this port only serves the upload API"})

    def do_HEAD(self):
        self.do_GET()
