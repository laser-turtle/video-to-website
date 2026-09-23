"""Shared reader progress and preferences, independent of processing workflows.

One reader profile for the trusted-LAN application. Content namespaces keep old
step completion from being applied to regenerated instructions. Explicit resets
remain rows, so importing an older browser cannot resurrect cleared progress.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from urllib.parse import urlparse

from .catalog import CatalogConflict

DEFAULTS = {"rate": 1, "loop": True, "player_collapsed": True, "clip_autoplay": True, "hide_completed": False}
RATES = [0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3]
COURSE_VIEW_CHOICES = {
    "sort": {"saved", "number", "title", "shortest"}, "view": {"chapters", "flat"},
    "density": {"detailed", "compact"}, "readerDensity": {"detailed", "compact"},
}
LIBRARY_VIEW_CHOICES = {"grouping": {"chapters", "flat"}, "density": {"detailed", "compact"}}


def valid_view_field(scope, key, value, courses):
    choices = LIBRARY_VIEW_CHOICES if scope == "library" else COURSE_VIEW_CHOICES
    if key in choices:
        return isinstance(value, str) and value in choices[key]
    if type(value) is not bool:
        return False
    if scope != "library":
        return re.fullmatch(r"opened:(?:other|\d{1,3}):1", key) is not None
    if key.startswith("expanded:"):
        return key.removeprefix("expanded:") in courses
    if key.startswith("chapter:"):
        course, _, chapter = key.removeprefix("chapter:").partition("|")
        return course in courses and re.fullmatch(r"(?:other|\d{1,3}):1", chapter) is not None
    return False


class ReadingState:
    def __init__(self, catalog):
        self.catalog = catalog

    @staticmethod
    def _lessons(db):
        lessons = {}
        for row in db.execute("SELECT id,published FROM lessons WHERE deleted=0 AND published IS NOT NULL"):
            lesson = json.loads(row["published"])
            key = "v2w:" + (lesson.get("reading_key") or row["id"])
            lessons[key] = (row["id"], [f'step-{s["index"]}' for s in lesson["steps"]] or ["__lessonComplete"])
        return lessons

    def _snapshot(self, db):
        active = self._lessons(db)
        states = {row["namespace"]: {"state": json.loads(row["state"]), "revision": row["revision"]}
                  for row in db.execute("SELECT * FROM reading_state") if row["namespace"] in active}
        preference = db.execute("SELECT * FROM reader_preferences WHERE id=1").fetchone()
        courses = [row[0] for row in db.execute("SELECT id FROM courses")]
        scopes = {"library", *("course:" + course for course in courses)}
        views = {row["scope"]: {"values": json.loads(row["value"]), "revision": row["revision"]}
                 for row in db.execute("SELECT * FROM reader_views") if row["scope"] in scopes}
        return {"version": 1, "states": states, "lessons": {key: value[1] for key, value in active.items()}, "preferences": {
            "values": DEFAULTS | (json.loads(preference["value"]) if preference else {}),
            "revision": preference["revision"] if preference else 0}, "views": views, "courses": courses}

    def snapshot(self):
        with self.catalog.connect() as db:
            db.execute("BEGIN")
            return self._snapshot(db)

    def edit(self, body):
        action = body.get("action")
        if action not in ("patch", "import", "preferences", "import-preferences", "view", "import-views"):
            raise ValueError("Unknown reading-state action.")
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if action in ("preferences", "import-preferences"):
                self._preferences(db, body, importing=action == "import-preferences")
            elif action in ("view", "import-views"):
                self._views(db, body, importing=action == "import-views")
            else:
                self._progress(db, body, importing=action == "import")
            return self._snapshot(db)

    @staticmethod
    def _views(db, body, *, importing):
        entries = body.get("entries") if importing else [body]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 2000:
            raise ValueError("Expected 1–2000 view preference entries.")
        courses = {row[0] for row in db.execute("SELECT id FROM courses")}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("scope"), str):
                raise ValueError("Expected a view preference scope.")
            scope = entry["scope"]
            if scope != "library" and (not scope.startswith("course:") or scope[7:] not in courses):
                if importing:
                    continue
                raise CatalogConflict("This course is no longer available. Reload the library.")
            values = entry.get("values")
            if not isinstance(values, dict) or len(values) > 20000:
                raise ValueError("Expected a bounded view preferences object.")
            valid = {key: value for key, value in values.items() if valid_view_field(scope, key, value, courses)}
            if not importing and len(valid) != len(values):
                raise ValueError("Invalid view preference field or value.")
            row = db.execute("SELECT * FROM reader_views WHERE scope=?", (scope,)).fetchone()
            if importing and (row or not valid):
                continue
            revision = row["revision"] if row else 0
            if not importing and (type(entry.get("revision")) is not int or entry["revision"] != revision):
                raise CatalogConflict("View preferences changed on another device. Retry to apply your changes to the latest settings.")
            current = json.loads(row["value"]) if row else {}
            db.execute("INSERT OR REPLACE INTO reader_views VALUES(?,?,?)", (scope, json.dumps(current | valid), revision + 1))

    def _progress(self, db, body, *, importing):
        entries = body.get("entries")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 2000:
            raise ValueError("Expected 1–2000 lesson progress entries.")
        lessons = self._lessons(db)
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
                raise ValueError("Each entry needs a lesson key.")
            key = entry["key"]
            if key in seen:
                raise ValueError("Duplicate lesson key.")
            seen.add(key)
            if key not in lessons:
                if importing:
                    continue
                raise CatalogConflict("This lesson has changed. Reload the page before updating its progress.")
            lesson_id, units = lessons[key]
            changes = entry.get("changes")
            if not isinstance(changes, dict) or any(k not in units or type(v) is not bool for k, v in changes.items()):
                raise ValueError("Progress must contain boolean values for this lesson's steps.")
            row = db.execute("SELECT * FROM reading_state WHERE namespace=?", (key,)).fetchone()
            if importing and row:
                continue
            revision = row["revision"] if row else 0
            if not importing and (type(entry.get("revision")) is not int or entry["revision"] != revision):
                raise CatalogConflict("Progress changed on another page or device. The latest state has been loaded; try again.")
            state = (json.loads(row["state"]) if row else {}) | changes
            db.execute("""INSERT INTO reading_state VALUES(?,?,?,?,?)
                ON CONFLICT(namespace) DO UPDATE SET state=excluded.state,
                revision=excluded.revision,updated=excluded.updated""",
                (key, lesson_id, json.dumps(state), revision + 1, time.time()))

    @staticmethod
    def _preferences(db, body, *, importing):
        values = body.get("values")
        if not isinstance(values, dict) or any(k not in DEFAULTS for k in values):
            raise ValueError("Unknown reader preference.")
        for key, value in values.items():
            if key == "rate":
                if type(value) not in (int, float) or value not in RATES:
                    raise ValueError("Choose a supported playback speed.")
            elif type(value) is not bool:
                raise ValueError("Reader toggles must be boolean values.")
        row = db.execute("SELECT * FROM reader_preferences WHERE id=1").fetchone()
        if importing and row:
            return
        revision = row["revision"] if row else 0
        if not importing and (type(body.get("revision")) is not int or body["revision"] != revision):
            raise CatalogConflict("Settings changed on another device. The latest settings have been loaded; try again.")
        current = json.loads(row["value"]) if row else {}
        db.execute("INSERT OR REPLACE INTO reader_preferences VALUES(1,?,?)", (json.dumps(current | values), revision + 1))


def handle_reading_api(handler):
    if urlparse(handler.path).path != "/api/reading":
        return False
    if handler.catalog is None or not getattr(handler.server, "reading_enabled", True):
        handler._json(404, {"error": "Reading synchronization is not enabled here."}, close=True)
        return True
    store = ReadingState(handler.catalog)
    try:
        if handler.command == "GET":
            handler._json(200, store.snapshot())
        elif handler.command == "POST":
            if handler.headers.get("Transfer-Encoding") or "application/json" not in handler.headers.get("Content-Type", ""):
                raise ValueError("Expected a JSON request with Content-Length.")
            size = int(handler.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 * 1024:
                raise ValueError("Expected a JSON request smaller than 1 MB.")
            handler.connection.settimeout(30)
            raw = handler.rfile.read(size)
            if len(raw) != size:
                raise ValueError("Incomplete request body.")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            handler._json(200, store.edit(body), close=True)
        else:
            handler._json(405, {"error": "Use GET or POST."}, close=True)
    except CatalogConflict as exc:
        handler._json(409, {"error": str(exc)}, close=True)
    except (ValueError, TypeError) as exc:
        handler._json(400, {"error": str(exc)}, close=True)
    except (sqlite3.Error, OSError):
        handler._json(503, {"error": "Reading state could not be saved. Check the server connection and storage."}, close=True)
    return True
