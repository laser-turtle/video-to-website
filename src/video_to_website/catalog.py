"""The local library catalog. DBOS owns execution; this database owns intent.

Connections are short lived and never shared between HTTP and workflow threads.
Filesystem reconciliation preserves IDs on unambiguous content-preserving moves.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .pipeline import discover_courses
from .progress import STAGE_LABELS
from .util import digest_file, digest_json, file_lock, natural_key, slugify

SCHEMA_VERSION = 2
PIPELINE_VERSION = "1"
ACTIVE = ("queued", "running")


class CatalogConflict(ValueError):
    """The library changed after the client loaded its editable snapshot."""


class Catalog:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.path = self.directory / "catalog.sqlite"
        self.lock_path = self.directory / "catalog.lock"
        self.directory.mkdir(parents=True, exist_ok=True)
        with file_lock(self.directory / "schema.lock"), self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"catalog schema {version} is newer than this application"
                )
            if version == 0:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS courses (
                        id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                        slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS lessons (
                        id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id),
                        path TEXT NOT NULL UNIQUE, slug TEXT NOT NULL,
                        digest TEXT NOT NULL, size INTEGER NOT NULL, mtime INTEGER NOT NULL,
                        deleted INTEGER NOT NULL DEFAULT 0,
                        desired_key TEXT, desired_build TEXT, published TEXT
                    );
                    CREATE TABLE IF NOT EXISTS builds (
                        id TEXT PRIMARY KEY, lesson_id TEXT NOT NULL REFERENCES lessons(id),
                        input_key TEXT NOT NULL, spec TEXT NOT NULL,
                        state TEXT NOT NULL, stage TEXT, error TEXT,
                        created REAL NOT NULL, updated REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS builds_state ON builds(state);
                    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    PRAGMA user_version=1;
                    COMMIT;
                """)
                version = 1
            if version == 1:
                db.execute("BEGIN IMMEDIATE")
                db.execute("ALTER TABLE courses ADD COLUMN position INTEGER")
                db.execute("ALTER TABLE lessons ADD COLUMN title TEXT")
                db.execute("ALTER TABLE lessons ADD COLUMN position INTEGER")
                db.execute("PRAGMA user_version=2")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def lock(self):
        return file_lock(self.lock_path)

    @staticmethod
    def order_key(row: dict):
        return (row["position"] is None, row["position"] or 0, natural_key(row["path"]))

    def _library(self, db) -> dict:
        courses = []
        for course in sorted([dict(r) for r in db.execute("SELECT * FROM courses")], key=self.order_key):
            rows = [dict(r) for r in db.execute("""SELECT l.*,b.state FROM lessons l
                LEFT JOIN builds b ON b.id=l.desired_build WHERE l.course_id=? AND l.deleted=0""", (course["id"],))]
            videos = []
            for row in sorted(rows, key=self.order_key):
                published = json.loads(row["published"]) if row["published"] else {}
                videos.append({
                    "id": row["id"], "title": row["title"] or Path(row["path"]).stem,
                    "source_title": Path(row["path"]).stem,
                    "source_name": Path(row["path"]).name, "source_path": row["path"],
                    "custom_title": row["title"] is not None, "position": row["position"],
                    "description": published.get("title", ""), "bytes": row["size"],
                    "state": row["state"] or "queued", "duration": published.get("duration"),
                    "href": f"{course['slug']}/{row['slug']}.html" if published else None,
                })
            if videos:
                courses.append({"id": course["id"], "title": course["title"], "source_path": course["path"],
                                "position": course["position"], "href": f"{course['slug']}/index.html"
                                if any(v["href"] for v in videos) else None, "videos": videos})
        # Processing status is deliberately excluded: finishing a job does not
        # invalidate a user's edit, while moves/imports/other edits do.
        revision = digest_json([
            [c["id"], c["title"], c["source_path"], c["position"],
             [[v["id"], v["title"], v["source_path"], v["position"], v["custom_title"]] for v in c["videos"]]]
            for c in courses
        ])
        return {"revision": revision, "courses": courses}

    def library(self) -> dict:
        with self.lock(), self.connect() as db:
            return self._library(db)

    def edit_library(self, resource: str, item_id: str | None, action: str, body: dict) -> dict:
        """Apply an entire title/order edit without touching files or build intent."""
        with self.lock(), self.connect() as db:
            current = self._library(db)
            if body.get("revision") != current["revision"]:
                raise CatalogConflict("The library changed. Reload it before applying this change.")
            courses = current["courses"]
            course = next((c for c in courses if c["id"] == item_id), None)
            lesson = next((v for c in courses for v in c["videos"] if v["id"] == item_id), None)
            if action == "title":
                if resource == "courses" and course:
                    table = "courses"
                elif resource == "lessons" and lesson:
                    table = "lessons"
                else:
                    raise KeyError(item_id)
                title = body.get("title")
                if title is not None or table == "courses":
                    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 240:
                        raise ValueError("Use a title between 1 and 240 characters.")
                    title = title.strip()
                    if any(ord(char) < 32 for char in title):
                        raise ValueError("Titles must be a single line.")
                db.execute(f"UPDATE {table} SET title=? WHERE id=?", (title, item_id))
            elif action == "order":
                if resource != "courses":
                    raise ValueError("Unknown ordering action.")
                if item_id is None:
                    table, items = "courses", courses
                elif course:
                    table, items = "lessons", course["videos"]
                else:
                    raise KeyError(item_id)
                wanted = body.get("ids")
                if body.get("mode") == "source":
                    wanted = [item["id"] for item in items]
                    positions = [None] * len(wanted)
                else:
                    if (not isinstance(wanted, list) or not all(isinstance(v, str) for v in wanted)
                            or len(wanted) != len(items) or set(wanted) != {item["id"] for item in items}):
                        raise ValueError("An order must contain every item exactly once.")
                    positions = range(len(wanted))
                db.executemany(f"UPDATE {table} SET position=? WHERE id=?", zip(positions, wanted))
            else:
                raise ValueError("Unknown library action.")
            return self._library(db)

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def build(self, build_id: str) -> dict:
        rows = self.rows("SELECT * FROM builds WHERE id=?", (build_id,))
        if not rows:
            raise KeyError(build_id)
        return rows[0]

    def is_current(self, build_id: str) -> bool:
        return bool(
            self.rows(
                """SELECT 1 FROM lessons l JOIN builds b ON b.id=l.desired_build
            WHERE b.id=? AND l.deleted=0 AND b.state NOT IN ('cancelled','superseded')""",
                (build_id,),
            )
        )

    def update_build(self, build_id: str, state: str, *, stage=None, error=None):
        with self.connect() as db:
            db.execute(
                """UPDATE builds SET state=?,stage=COALESCE(?,stage),error=?,updated=?
                WHERE id=? AND state NOT IN ('cancelled','superseded','ready')""",
                (state, stage, error, time.time(), build_id),
            )

    def reconcile(self, library: Path, options: dict) -> None:
        """Import a settled filesystem snapshot, recording build intents atomically.

        A build ID is an outbox key: enqueueing it repeatedly is safe, so a crash
        between this commit and the DBOS enqueue cannot lose or duplicate work.
        """
        library = library.resolve()
        with self.lock():
            existing = {r["path"]: r for r in self.rows("SELECT * FROM lessons")}
            found = []
            for course in discover_courses([library]):
                for video in course["videos"]:
                    if not video.resolve().is_relative_to(library):
                        continue
                    relative = str(video.relative_to(library))
                    before = video.stat()
                    previous = existing.get(relative)
                    fingerprint = (before.st_size, before.st_mtime_ns)
                    if previous and fingerprint == (
                        previous["size"],
                        previous["mtime"],
                    ):
                        digest = previous["digest"]
                    else:
                        digest = digest_file(video)
                    after = video.stat()
                    if fingerprint != (after.st_size, after.st_mtime_ns):
                        raise RuntimeError(
                            f"{relative} changed during import; waiting for it to settle"
                        )
                    found.append((course, relative, fingerprint, digest))
            paths = {entry[1] for entry in found}
            moved = {}
            for previous in existing.values():
                if previous["path"] not in paths:
                    moved.setdefault(previous["digest"], []).append(previous)
            new_counts = {}
            for _, relative, _, digest in found:
                if relative not in existing:
                    new_counts[digest] = new_counts.get(digest, 0) + 1
            with self.connect() as db:
                config = json.dumps(options, sort_keys=True)
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES ('options',?)", (config,)
                )
                for course, relative, (size, mtime), digest in found:
                    course_path = str(course["root"].relative_to(library))
                    row = db.execute(
                        "SELECT * FROM courses WHERE path=?", (course_path,)
                    ).fetchone()
                    if row is None:
                        course_id = uuid.uuid4().hex
                        course_slug = (
                            f"{slugify(course['title'], max_len=40)}-{course_id[:8]}"
                        )
                        db.execute(
                            "INSERT INTO courses (id,path,slug,title) VALUES (?,?,?,?)",
                            (course_id, course_path, course_slug, course["title"]),
                        )
                    else:
                        course_id, course_slug = row["id"], row["slug"]
                    previous = existing.get(relative)
                    candidates = moved.get(digest, [])
                    if (
                        previous is None
                        and len(candidates) == 1
                        and new_counts.get(digest) == 1
                    ):
                        previous = candidates[0]
                    lesson_id = previous["id"] if previous else uuid.uuid4().hex
                    lesson_slug = (
                        previous["slug"] if previous else "lesson-" + lesson_id
                    )
                    if previous:
                        db.execute(
                            """UPDATE lessons SET course_id=?,path=?,digest=?,size=?,mtime=?,deleted=0
                            WHERE id=?""",
                            (course_id, relative, digest, size, mtime, lesson_id),
                        )
                    else:
                        db.execute(
                            """INSERT INTO lessons (id,course_id,path,slug,digest,size,mtime)
                            VALUES (?,?,?,?,?,?,?)""",
                            (
                                lesson_id,
                                course_id,
                                relative,
                                lesson_slug,
                                digest,
                                size,
                                mtime,
                            ),
                        )
                    input_key = digest_json(
                        {
                            "source": digest,
                            "options": options,
                            "pipeline": PIPELINE_VERSION,
                        }
                    )
                    if (
                        not previous
                        or previous["desired_key"] != input_key
                        or not previous["desired_build"]
                    ):
                        spec = {
                            "library": str(library),
                            "path": relative,
                            "digest": digest,
                            "lesson_id": lesson_id,
                            "slug": lesson_slug,
                            "options": options,
                            "catalog": str(self.directory),
                            "legacy_course": slugify(course["title"]),
                            "legacy_lesson": slugify(Path(relative).stem),
                        }
                        self._new_build(db, lesson_id, input_key, spec)
                for row in db.execute(
                    "SELECT id,path,desired_build FROM lessons WHERE deleted=0"
                ).fetchall():
                    if row["path"] not in paths:
                        db.execute(
                            "UPDATE lessons SET deleted=1,desired_build=NULL WHERE id=?",
                            (row["id"],),
                        )
                        db.execute(
                            "UPDATE builds SET state='superseded',updated=? WHERE id=? AND state IN ('queued','running')",
                            (time.time(), row["desired_build"]),
                        )

    def _new_build(self, db, lesson_id, input_key, spec):
        build_id = uuid.uuid4().hex
        spec = {**spec, "build_id": build_id}
        now = time.time()
        db.execute(
            "UPDATE builds SET state='superseded',updated=? WHERE lesson_id=? AND state IN ('queued','running')",
            (now, lesson_id),
        )
        db.execute(
            "INSERT INTO builds VALUES (?,?,?,?,?,?,?,?,?)",
            (
                build_id,
                lesson_id,
                input_key,
                json.dumps(spec),
                "queued",
                None,
                None,
                now,
                now,
            ),
        )
        db.execute(
            "UPDATE lessons SET desired_key=?,desired_build=? WHERE id=?",
            (input_key, build_id, lesson_id),
        )
        return build_id

    def retry(self, lesson_id: str) -> str:
        with self.lock(), self.connect() as db:
            row = db.execute(
                """SELECT b.* FROM lessons l JOIN builds b ON b.id=l.desired_build
                WHERE l.id=? AND l.deleted=0""",
                (lesson_id,),
            ).fetchone()
            if row is None:
                raise KeyError(lesson_id)
            if row["state"] in ACTIVE:
                return row["id"]
            spec = json.loads(row["spec"])
            lesson = db.execute(
                "SELECT path FROM lessons WHERE id=?", (lesson_id,)
            ).fetchone()
            spec["path"] = lesson["path"]
            return self._new_build(db, lesson_id, row["input_key"], spec)

    def cancel(self, lesson_id: str):
        with self.lock(), self.connect() as db:
            db.execute(
                """UPDATE builds SET state='cancelled',updated=? WHERE id=(
                SELECT desired_build FROM lessons WHERE id=? AND deleted=0) AND state IN ('queued','running')""",
                (time.time(), lesson_id),
            )

    def accept(self, build_id: str, lesson: dict) -> bool:
        """Called with the catalog lock held, after immutable assets are committed."""
        if not self.is_current(build_id):
            return False
        with self.connect() as db:
            db.execute(
                "UPDATE lessons SET published=? WHERE desired_build=? AND deleted=0",
                (json.dumps(lesson), build_id),
            )
        return True

    def published_courses(self) -> list[dict]:
        courses = []
        for course in sorted(self.rows("SELECT * FROM courses"), key=self.order_key):
            lessons = []
            rows = self.rows(
                "SELECT * FROM lessons WHERE course_id=? AND deleted=0 AND published IS NOT NULL",
                (course["id"],),
            )
            for row in sorted(rows, key=self.order_key):
                lesson = json.loads(row["published"])
                lesson["description"] = lesson.get("title", "")
                lesson["title"] = row["title"] or Path(row["path"]).stem
                lesson["display_title"] = lesson["title"]
                lesson["source_name"] = Path(row["path"]).name
                lessons.append(lesson)
            if lessons:
                courses.append(
                    {
                        "id": course["id"],
                        "slug": course["slug"],
                        "title": course["title"],
                        "lessons": lessons,
                    }
                )
        return courses

    def status(self) -> dict:
        rows = self.rows("""SELECT l.id,l.path,l.slug,l.title,l.published,c.title AS course,c.slug AS course_slug,
            b.id AS build_id,b.state,b.stage,b.error,b.created,b.updated
            FROM lessons l JOIN courses c ON c.id=l.course_id LEFT JOIN builds b ON b.id=l.desired_build
            WHERE l.deleted=0 ORDER BY b.created,b.id,l.id""")
        videos = []
        queue_position = 0
        stages = ["probe", "transcribe", "scenes", "steps", "frames", "publish"]
        for row in rows:
            state = {"ready": "done", "running": "working"}.get(
                row["state"], row["state"] or "queued"
            )
            if state == "queued":
                queue_position += 1
            videos.append(
                {
                    "id": row["id"],
                    "build_id": row["build_id"],
                    "course": row["course"],
                    "title": row["title"] or Path(row["path"]).stem,
                    "source_name": Path(row["path"]).name,
                    "source_path": row["path"],
                    "queue_position": queue_position if state == "queued" else None,
                    "created": row["created"],
                    "updated": row["updated"],
                    "lesson_href": f"{row['course_slug']}/{row['slug']}.html" if row["published"] else None,
                    "state": state,
                    "stage": row["stage"],
                    "label": row["error"]
                    or {
                        **STAGE_LABELS,
                        "prepare": "Preparing the video",
                        "source": "Reading the file",
                        "assemble": "Preparing the lesson",
                        "publish": "Publishing the lesson",
                    }.get(
                        row["stage"],
                        {
                            "queued": "Waiting",
                            "done": "Ready to read",
                            "cancelled": "Cancelled",
                            "failed": "Failed",
                        }.get(state, state),
                    ),
                    "step": stages.index(row["stage"]) + 1
                    if row["stage"] in stages
                    else 0,
                    "steps": len(stages),
                    "elapsed": round(time.time() - (row["updated"] or time.time()), 1),
                    "error": row["error"],
                }
            )
        ready = self.rows(
            "SELECT value AS stamp FROM settings WHERE key='publication_at'"
        )
        return {
            "updated": time.time(),
            "building": any(v["state"] == "working" for v in videos),
            "built": float(ready[0]["stamp"]) if ready else 0,
            "error": None,
            "retry_in": None,
            "videos": videos,
        }
