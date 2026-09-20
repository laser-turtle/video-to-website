"""Accepting videos over HTTP, so a course can be added from a browser.

Deliberately thin: an upload lands in the library as an ordinary file and the
watcher picks it up on its next poll. The file appearing *is* the trigger, so
nothing here knows how to build anything.

Raw PUT rather than a multipart form. The body is then just the file, which
streams to disk in constant memory -- and the stdlib lost its multipart parser
when cgi was removed in 3.13, so parsing one would mean writing it here.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import unicodedata
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .storage import StorageFull, StorageLocation, StorageManager
from .util import VIDEO_SUFFIXES, file_lock, log, natural_key, slugify, warn

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
    name = unquote(name).replace("\x00", "")
    # Downloaded names can contain invisible formatting controls or orphaned
    # variation selectors (not whitespace). Discard those before replacing
    # unsupported visible characters; otherwise a blank-looking prefix becomes '_'.
    name = "".join(
        char for char in name
        if unicodedata.category(char) != "Cf"
        and not (0xFE00 <= ord(char) <= 0xFE0F or 0xE0100 <= ord(char) <= 0xE01EF)
    ).strip()
    if "/" in name or "\\" in name:
        return None
    name = _UNSAFE.sub("_", name).strip(" .")
    name = re.sub(r"\s{2,}", " ", name)
    if not name or name in (".", ".."):
        return None
    return name[:120]


def existing_component(name: str) -> str | None:
    """Decode a lookup without rewriting the name of an existing source."""
    name = unquote(name)
    if not name or name.startswith(".") or any(c in name for c in ("/", "\\", "\x00")):
        return None
    return name


def course_videos(course_dir: Path) -> list[Path]:
    """The lessons in a course, in the order the builder will take them.

    Immediate children only. Videos nested deeper still build -- they are
    flattened into the same lesson list -- but they are not something this page
    can meaningfully rename, so it does not offer to.
    """
    try:
        found = [
            p
            for p in course_dir.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith(".")
        ]
    except OSError:
        return []
    return sorted(found, key=lambda p: natural_key(p.name))


def library_listing(library: Path) -> list[dict]:
    courses = []
    for name in course_names(library):
        videos = []
        for video in course_videos(library / name):
            try:
                stat = video.stat()
            except OSError:
                continue
            videos.append({"name": video.name, "bytes": stat.st_size, "mtime": stat.st_mtime})
        courses.append({"name": name, "videos": videos})
    return courses


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

    @property
    def catalog(self):
        return getattr(self.server, "catalog", None)

    @property
    def storage(self):
        if getattr(self.server, "storage", None) is not None:
            return self.server.storage
        state = self.catalog.directory / "storage" if self.catalog else self.library / ".storage"
        # The current API exposes one root. Capacity accounting already accepts
        # several locations and coordinates those sharing a filesystem.
        return StorageManager(state, [StorageLocation("library", self.library,
            buffer_bytes=getattr(self.server, "min_free_bytes", DISK_MARGIN))])

    def _library_lock(self):
        if self.catalog is not None:
            return self.catalog.lock()
        return file_lock(self.library / ".management.lock")

    def _contained(self, target: Path) -> bool:
        if not target.resolve().is_relative_to(self.library.resolve()):
            self._json(400, {"error": "path is outside the library"}, close=True)
            return False
        return True

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
        self.send_header("Cache-Control", "no-store")
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
        from .worker_api import handle_worker_api

        if handle_worker_api(self):
            return True
        path = urlparse(self.path).path
        if not path.startswith(API_PREFIX):
            return False
        if self.library is None:
            self._json(503, {"error": "uploads are not enabled here"})
        elif path == "/api/storage":
            try:
                course = parse_qs(urlparse(self.path).query).get("course", [""])[0]
                if course:
                    encoded = quote(course, safe="")
                    name = existing_component(encoded)
                    name = name if name and (self.library / name).is_dir() else safe_component(encoded)
                    if not name:
                        raise ValueError("Invalid course name.")
                    destination = self.library / name
                else:
                    destination = self.library
                self._json(200, self.storage.overview("library", destination))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except OSError:
                self._json(503, {"error": "Storage capacity is unavailable. Check that the library disk is mounted and writable."})
        elif path == "/api/courses":
            self._json(200, {"courses": course_names(self.library)})
        elif path == "/api/library":
            courses = library_listing(self.library)
            if self.catalog:
                titles = {row["path"]: row["title"] for row in self.catalog.rows("SELECT path,title FROM courses")}
                for course in courses:
                    course["title"] = titles.get(course["name"], course["name"])
            self._json(200, {"courses": courses})
        elif path == "/api/jobs" and self.catalog:
            self._json(200, self.catalog.status())
        elif path == "/api/catalog" and self.catalog:
            self._json(200, self.catalog.library())
        else:
            self._json(404, {"error": "no such endpoint"})
        return True

    @property
    def work(self) -> Path | None:
        return getattr(self.server, "work", None)

    def _target(self) -> tuple[Path, str, str] | None:
        """Resolve /api/library/<course>/<name>, answering the error itself."""
        path = urlparse(self.path).path
        if self.library is None:
            self._json(503, {"error": "uploads are not enabled here"}, close=True)
            return None
        parts = path[len(API_PREFIX):].split("/") if path.startswith(API_PREFIX) else []
        if len(parts) != 3 or parts[0] != "library":
            self._json(404, {"error": "no such endpoint"}, close=True)
            return None
        course, name = existing_component(parts[1]), existing_component(parts[2])
        if not course or not name:
            self._json(400, {"error": "unusable course or file name"}, close=True)
            return None
        target = self.library / course / name
        return (target, course, name) if self._contained(target) else None

    def do_DELETE(self):
        if not getattr(self.server, "uploads_enabled", True):
            self._json(403, {"error": "Library editing is disabled."}, close=True)
            return
        if self.library is None:
            self._json(503, {"error": "uploads are not enabled here"}, close=True)
            return
        with self._library_lock():
            self._delete()

    def _delete(self):
        found = self._target()
        if found is None:
            return
        target, course, name = found
        if not target.is_file():
            self._json(404, {"error": f"{course}/{name} is not there"}, close=True)
            return
        try:
            target.unlink()
            # The course folder goes too once its last lesson does, so a
            # deleted course does not linger as an empty directory.
            if not course_videos(target.parent):
                with contextlib.suppress(OSError):
                    target.parent.rmdir()
        except PermissionError:
            self._json(403, {"error": self._denied(target.parent)}, close=True)
            return
        except OSError as exc:
            self._json(500, {"error": str(exc)}, close=True)
            return
        # The stage cache is deliberately left alone: putting the same file
        # back should not mean transcribing it again.
        log(f"deleted {course}/{name}")
        if self.catalog:
            with self.catalog.connect() as db:
                relative = str(target.relative_to(self.library))
                db.execute("UPDATE builds SET state='superseded' WHERE id=(SELECT desired_build FROM lessons WHERE path=?) AND state IN ('queued','running')", (relative,))
                db.execute("UPDATE lessons SET deleted=1,desired_build=NULL WHERE path=?", (relative,))
        self._json(200, {"deleted": f"{course}/{name}"})

    def do_POST(self):
        from .worker_api import handle_worker_api

        if handle_worker_api(self):
            return
        parts = urlparse(self.path).path.strip("/").split("/")
        if not getattr(self.server, "uploads_enabled", True):
            self._json(403, {"error": "Library editing is disabled."}, close=True)
            return
        if parts[:2] == ["api", "catalog"]:
            self._edit_catalog(parts)
            return
        if len(parts) == 4 and parts[:2] == ["api", "lessons"] and self.catalog:
            try:
                if parts[3] == "retry":
                    result = {"build_id": self.catalog.retry(parts[2])}
                elif parts[3] == "cancel":
                    self.catalog.cancel(parts[2])
                    result = {"cancelled": parts[2]}
                else:
                    self._json(404, {"error": "no such action"}, close=True)
                    return
                self._json(200, result, close=True)
            except KeyError:
                self._json(404, {"error": "lesson not found"}, close=True)
            return
        if self.library is None:
            self._json(503, {"error": "uploads are not enabled here"}, close=True)
            return
        with self._library_lock():
            self._rename()

    def _edit_catalog(self, parts: list[str]):
        from .catalog import CatalogConflict
        from .publication import refresh_site

        if self.catalog is None or self.library is None:
            self._json(503, {"error": "Library editing needs the durable service."}, close=True)
            return
        if len(parts) not in (4, 5):
            self._json(404, {"error": "Unknown library action."}, close=True)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 * 1024:
                raise ValueError("Expected a JSON request smaller than 1 MB.")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            result = self.catalog.edit_library(parts[2], parts[3] if len(parts) == 5 else None, parts[-1], body)
        except CatalogConflict as exc:
            self._json(409, {"error": str(exc)}, close=True)
            return
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc)}, close=True)
            return
        except KeyError:
            self._json(404, {"error": "That item is no longer in the library."}, close=True)
            return
        warning = None
        try:
            # Title and ordering edits publish immediately, even if the media
            # worker is busy or stopped. This does not enqueue processing work.
            with self.catalog.lock():
                stored = self.catalog.rows("SELECT value FROM settings WHERE key='options'")
                options = json.loads(stored[0]["value"]) if stored else {}
                if options.get("out"):
                    refresh_site(self.catalog, Path(options["out"]), markdown=options.get("markdown", True))
        except (OSError, ValueError) as exc:
            warning = f"Saved. Pages could not refresh yet: {exc}"
        self._json(200, {"catalog": result, "warning": warning}, close=True)

    def _rename(self):
        found = self._target()
        if found is None:
            return
        source, course, name = found
        if not source.is_file():
            self._json(404, {"error": f"{course}/{name} is not there"}, close=True)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > 16384:
                raise ValueError("body too large")
            body = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected object")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "expected a JSON body"}, close=True)
            return

        to_course = safe_component(str(body["to_course"])) if body.get("to_course") else course
        to_name = safe_component(str(body["to_name"])) if body.get("to_name") else name
        if not to_course or not to_name:
            self._json(400, {"error": "unusable course or file name"}, close=True)
            return
        if Path(to_name).suffix.lower() not in VIDEO_SUFFIXES:
            self._json(415, {"error": f"{Path(to_name).suffix or 'that'} is not a video file"}, close=True)
            return
        target = self.library / to_course / to_name
        if not self._contained(target):
            return
        if target == source:
            self._json(200, {"course": course, "name": name})
            return
        if target.exists():
            self._json(409, {"error": f"{to_course}/{to_name} is already there"}, close=True)
            return

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            if not course_videos(source.parent):
                with contextlib.suppress(OSError):
                    source.parent.rmdir()
        except PermissionError:
            self._json(403, {"error": self._denied(target.parent)}, close=True)
            return
        except OSError as exc:
            self._json(500, {"error": str(exc)}, close=True)
            return

        if self.catalog:
            # Reconciliation resolves the new course; updating the path here
            # also preserves IDs when duplicate content makes a move ambiguous.
            with self.catalog.connect() as db:
                db.execute("UPDATE lessons SET path=? WHERE path=?",
                           (str(target.relative_to(self.library)), str(source.relative_to(self.library))))
        else:
            self._move_cache(course, name, to_course, to_name)
        log(f"renamed {course}/{name} to {to_course}/{to_name}")
        self._json(200, {"course": to_course, "name": to_name})

    def _move_cache(self, course: str, name: str, to_course: str, to_name: str) -> None:
        """Carry the stage cache across a rename, so reordering is free.

        Best effort by design. The builder numbers a slug that collides with
        another in the same course, and this cannot know about that, so a miss
        here costs a re-transcription rather than anything worse.
        """
        if self.work is None:
            return
        old = self.work / slugify(course) / slugify(Path(name).stem)
        new = self.work / slugify(to_course) / slugify(Path(to_name).stem)
        if not old.is_dir() or new.exists():
            return
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            old.replace(new)
            # Asset references contain the previous slug.
            (new / "assets.json").unlink(missing_ok=True)
            log(f"  carried the cache across to {new.parent.name}/{new.name}")
        except OSError as exc:
            warn(f"  could not carry the cache across, it will rebuild: {exc}")

    def do_PUT(self):
        from .worker_api import handle_worker_api

        if handle_worker_api(self):
            return
        path = urlparse(self.path).path
        if not getattr(self.server, "uploads_enabled", True):
            self._json(403, {"error": "Library editing is disabled."}, close=True)
            return
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
        existing_course = existing_component(raw_course)
        course = (existing_course if existing_course and (self.library / existing_course).is_dir()
                  else safe_component(raw_course))
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
        if not self._contained(target):
            return
        overwrite = parse_qs(urlparse(self.path).query).get("overwrite") == ["1"]
        if target.exists() and not overwrite:
            self._json(409, {"error": f"{course}/{name} is already there"}, close=True)
            return

        received = 0
        try:
            with self.storage.reserve("library", target, length) as upload:
                while received < length:
                    chunk = self.rfile.read(min(CHUNK, length - received))
                    if not chunk:
                        raise OSError("the connection closed before the file finished")
                    upload.write(chunk)
                    received += len(chunk)
                with self._library_lock():
                    if not self._contained(target):
                        return
                    if target.exists() and not overwrite:
                        self._json(409, {"error": f"{course}/{name} is already there"}, close=True)
                        return
                    upload.commit()
                    if self.catalog:
                        with self.catalog.connect() as db:
                            relative = str(target.relative_to(self.library))
                            db.execute("UPDATE builds SET state='superseded' WHERE id=(SELECT desired_build FROM lessons WHERE path=?) AND state IN ('queued','running')", (relative,))
                            db.execute("UPDATE lessons SET desired_build=NULL WHERE path=?", (relative,))
        except StorageFull as exc:
            self._json(507, {"error": str(exc), "storage": exc.status}, close=True)
            return
        except ValueError as exc:
            self._json(400, {"error": str(exc)}, close=True)
            return
        except PermissionError:
            warn(f"upload of {course}/{name} refused: {target.parent} is not writable by {service_user()}")
            self._json(403, {"error": self._denied(target.parent)}, close=True)
            return
        except OSError as exc:
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
