"""The local library catalog. DBOS owns execution; this database owns intent.

Connections are short lived and never shared between HTTP and workflow threads.
Filesystem reconciliation preserves IDs on unambiguous content-preserving moves.
"""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path

from .chapters import chapter_groups, lesson_chapter, lesson_numbering
from .pipeline import discover_courses
from .progress import STAGE_LABELS
from .util import digest_file, digest_json, file_fingerprint, file_lock, natural_key, read_stage, slugify
from .work_progress import progress_snapshot

SCHEMA_VERSION = 8
PIPELINE_VERSION = "1"
ACTIVE = ("queued", "running")
LESSON_OPTIONS_PREFIX = "lesson-options:"


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
                version = 2
            if version == 2:
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                db.execute("""CREATE TABLE build_progress (
                    build_id TEXT PRIMARY KEY REFERENCES builds(id),
                    stage TEXT NOT NULL, started REAL NOT NULL, payload TEXT NOT NULL)""")
                db.execute("PRAGMA user_version=3")
                version = 3
            if version == 3:
                from .worker_store import SCHEMA

                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("PRAGMA user_version=4")
                version = 4
            if version == 4:
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                db.execute("ALTER TABLE workers ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
                db.execute("PRAGMA user_version=5")
                version = 5
            if version == 5:
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                db.execute("ALTER TABLE lessons ADD COLUMN chapter_override INTEGER CHECK(chapter_override BETWEEN -1 AND 999)")
                db.execute("PRAGMA user_version=6")
                version = 6
            if version == 6:
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                db.execute("""CREATE TABLE reclaimed_sources (
                    lesson_id TEXT PRIMARY KEY REFERENCES lessons(id),
                    digest TEXT NOT NULL, snapshot TEXT NOT NULL,
                    build_id TEXT NOT NULL, created REAL NOT NULL)""")
                db.execute("PRAGMA user_version=7")
                version = 7
            if version == 7:
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                db.execute("""CREATE TABLE reading_state (
                    namespace TEXT PRIMARY KEY, lesson_id TEXT NOT NULL REFERENCES lessons(id),
                    state TEXT NOT NULL, revision INTEGER NOT NULL, updated REAL NOT NULL)""")
                db.execute("""CREATE TABLE reader_preferences (
                    id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL,
                    revision INTEGER NOT NULL)""")
                db.execute("PRAGMA user_version=8")

    @contextlib.contextmanager
    def connect(self, *, timeout: float = 30):
        db = sqlite3.connect(self.path, timeout=timeout)
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
        from .provider_pause import PAUSE_PREFIX, WAIT_PREFIX

        paused = {row["key"][len(PAUSE_PREFIX):] for row in db.execute("SELECT key FROM settings WHERE key LIKE ?", (PAUSE_PREFIX + "%",))}
        waiting = {row["key"][len(WAIT_PREFIX):]: json.loads(row["value"])["provider"]
                   for row in db.execute("SELECT key,value FROM settings WHERE key LIKE ?", (WAIT_PREFIX + "%",))}
        courses = []
        for course in sorted(
            [dict(r) for r in db.execute("SELECT * FROM courses")], key=self.order_key
        ):
            rows = [
                dict(r)
                for r in db.execute(
                    """SELECT l.*,b.state,r.snapshot FROM lessons l
                LEFT JOIN builds b ON b.id=l.desired_build
                LEFT JOIN reclaimed_sources r ON r.lesson_id=l.id
                WHERE l.course_id=? AND l.deleted=0""",
                    (course["id"],),
                )
            ]
            videos = []
            for row in sorted(rows, key=self.order_key):
                published = json.loads(row["published"]) if row["published"] else {}
                videos.append(
                    {
                        "id": row["id"],
                        "title": row["title"] or Path(row["path"]).stem,
                        "display_title": row["title"] or Path(row["path"]).stem,
                        "chapter_override": row["chapter_override"],
                        "chapter": lesson_chapter({"display_title": row["title"],
                            "source_name": Path(row["path"]).name, "chapter_override": row["chapter_override"]}),
                        "source_title": Path(row["path"]).stem,
                        "source_name": Path(row["path"]).name,
                        "source_path": row["path"],
                        "custom_title": row["title"] is not None,
                        "position": row["position"],
                        "description": published.get("title", ""),
                        "bytes": row["size"],
                        "source_reclaimed": row["snapshot"] is not None,
                        "state": "blocked" if row["state"] in ACTIVE and waiting.get(row["desired_build"]) in paused else row["state"] or "queued",
                        "duration": published.get("duration"),
                        "video_only": bool(published.get("video_only")),
                        "numbering": lesson_numbering({
                            "display_title": row["title"], "source_name": Path(row["path"]).name,
                        }),
                        "href": f"{course['slug']}/{row['slug']}.html"
                        if published
                        else None,
                    }
                )
            if videos:
                courses.append(
                    {
                        "id": course["id"],
                        "title": course["title"],
                        "source_path": course["path"],
                        "position": course["position"],
                        "href": f"{course['slug']}/index.html"
                        if any(v["href"] for v in videos)
                        else None,
                        "videos": videos,
                    }
                )
        # Processing status is deliberately excluded: finishing a job does not
        # invalidate a user's edit, while moves/imports/other edits do.
        revision = digest_json(
            [
                [
                    c["id"],
                    c["title"],
                    c["source_path"],
                    c["position"],
                    [
                        [
                            v["id"],
                            v["title"],
                            v["source_path"],
                            v["position"],
                            v["custom_title"],
                            v["chapter_override"],
                        ]
                        for v in c["videos"]
                    ],
                ]
                for c in courses
            ]
        )
        return {"revision": revision, "courses": courses}

    def library(self) -> dict:
        with self.lock(), self.connect() as db:
            return self._library(db)

    def edit_library(
        self, resource: str, item_id: str | None, action: str, body: dict
    ) -> dict:
        """Apply an entire title/order edit without touching files or build intent."""
        with self.lock(), self.connect() as db:
            current = self._library(db)
            if body.get("revision") != current["revision"]:
                raise CatalogConflict(
                    "The library changed. Reload it before applying this change."
                )
            courses = current["courses"]
            course = next((c for c in courses if c["id"] == item_id), None)
            lesson = next(
                (v for c in courses for v in c["videos"] if v["id"] == item_id), None
            )
            if action == "title":
                if resource == "courses" and course:
                    table = "courses"
                elif resource == "lessons" and lesson:
                    table = "lessons"
                else:
                    raise KeyError(item_id)
                title = body.get("title")
                if title is not None or table == "courses":
                    if (
                        not isinstance(title, str)
                        or not title.strip()
                        or len(title.strip()) > 240
                    ):
                        raise ValueError("Use a title between 1 and 240 characters.")
                    title = title.strip()
                    if any(ord(char) < 32 for char in title):
                        raise ValueError("Titles must be a single line.")
                db.execute(f"UPDATE {table} SET title=? WHERE id=?", (title, item_id))
            elif action in {"move", "chapter"}:
                if resource != "courses" or course is None:
                    raise ValueError("Choose a course for this change.")
                ids = body.get("ids")
                items = course["videos"]
                if (not isinstance(ids, list) or not ids or not all(isinstance(v, str) for v in ids)
                        or len(set(ids)) != len(ids) or not set(ids) <= {v["id"] for v in items}):
                    raise ValueError("Select lessons from this course exactly once.")
                selected = set(ids)
                moving = [item for item in items if item["id"] in selected]
                remaining = [item for item in items if item["id"] not in selected]
                if action == "move":
                    before = body.get("before")
                    if "before" not in body or (before is not None and (not isinstance(before, str) or before not in {v["id"] for v in remaining})):
                        raise ValueError("Choose an unselected lesson to move before, or the end of the course.")
                    index = next((i for i, v in enumerate(remaining) if v["id"] == before), len(remaining))
                    ordered = remaining[:index] + moving + remaining[index:]
                else:
                    number = body.get("chapter")
                    if "chapter" not in body or (number is not None and (type(number) is not int or not -1 <= number <= 999)):
                        raise ValueError("Use a chapter number from 0 to 999, Other lessons, or Automatic.")
                    for item in moving:
                        item["chapter_override"] = number
                        item["chapter"] = lesson_chapter(item)
                    if number is None:
                        ordered = items
                    else:
                        target = None if number == -1 else number
                        index = max((i + 1 for i, v in enumerate(remaining) if v["chapter"] == target), default=len(remaining))
                        ordered = remaining[:index] + moving + remaining[index:]
                    ordered = [v for group in chapter_groups(ordered) for v in group["lessons"]]
                    db.executemany("UPDATE lessons SET chapter_override=? WHERE id=?", [(number, v["id"]) for v in moving])
                db.executemany("UPDATE lessons SET position=? WHERE id=?", [(i, v["id"]) for i, v in enumerate(ordered)])
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
                mode = body.get("mode")
                if mode == "source":
                    wanted = [item["id"] for item in items]
                    positions = [None] * len(wanted)
                else:
                    if mode is not None:
                        allowed = {"title", "heuristic", "filename", "shortest", "longest"} if course else {"title"}
                        if not isinstance(mode, str) or mode not in allowed:
                            raise ValueError("Unknown sorting method.")

                        def sort_key(item):
                            title = natural_key(item["title"])
                            if mode == "heuristic":
                                numbering = item["numbering"]
                                return (item["chapter"] is None, item["chapter"] or 0, numbering[1] if numbering else 0, title)
                            if mode == "filename":
                                return natural_key(item["source_name"])
                            if mode in {"shortest", "longest"}:
                                duration = item["duration"]
                                return (duration is None, (duration or 0) * (-1 if mode == "longest" else 1))
                            return title

                        # Stable ties keep their current relative order. Compute
                        # against this locked snapshot, including unpublished
                        # lessons; the client never sends a filtered subset.
                        wanted = [item["id"] for item in sorted(items, key=sort_key)]
                    if (
                        not isinstance(wanted, list)
                        or not all(isinstance(v, str) for v in wanted)
                        or len(wanted) != len(items)
                        or set(wanted) != {item["id"] for item in items}
                    ):
                        raise ValueError(
                            "An order must contain every item exactly once."
                        )
                    positions = range(len(wanted))
                db.executemany(
                    f"UPDATE {table} SET position=? WHERE id=?", zip(positions, wanted)
                )
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

    def update_progress(self, build_id: str, stage: str, payload: dict):
        with self.connect(timeout=.1) as db:
            db.execute("""INSERT INTO build_progress (build_id,stage,started,payload)
                SELECT ?,?,?,? WHERE EXISTS (
                    SELECT 1 FROM builds WHERE id=? AND state='running' AND stage=?)
                ON CONFLICT(build_id) DO UPDATE SET stage=excluded.stage,
                    started=excluded.started,payload=excluded.payload""",
                (build_id, stage, payload["stage_started"], json.dumps(payload), build_id, stage))

    def reconcile(self, library: Path, options: dict) -> None:
        """Import a settled filesystem snapshot, recording build intents atomically.

        A build ID is an outbox key: enqueueing it repeatedly is safe, so a crash
        between this commit and the DBOS enqueue cannot lose or duplicate work.
        """
        library = library.resolve()
        with self.lock():
            existing = {r["path"]: r for r in self.rows("SELECT * FROM lessons")}
            reclaimed = {r["lesson_id"]: r for r in self.rows("SELECT * FROM reclaimed_sources")}
            found = []
            for course in discover_courses([library]):
                for video in course["videos"]:
                    if not video.resolve().is_relative_to(library):
                        continue
                    relative = str(video.relative_to(library))
                    before = video.stat()
                    previous = existing.get(relative)
                    fingerprint = (before.st_size, before.st_mtime_ns)
                    if previous and previous["id"] not in reclaimed and fingerprint == (
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
            # Reclamation removes a duplicate, not the logical library entry.
            # Include snapshot-backed lessons in normal settings reconciliation,
            # even when their entire course folder has since been removed.
            courses = {r["id"]: r for r in self.rows("SELECT * FROM courses")}
            retained_paths = set()
            for previous in existing.values():
                retained = reclaimed.get(previous["id"])
                if retained and not previous["deleted"] and previous["path"] not in paths:
                    course = courses[previous["course_id"]]
                    course_path, course_title = Path(course["path"]), course["title"]
                    if not Path(previous["path"]).is_relative_to(course_path):
                        # An API file move updates path before its course is
                        # reconciled; cleanup may happen between those events.
                        course_path = Path(previous["path"]).parent
                        course_title = course_path.name
                    retained_paths.add(previous["path"])
                    found.append(({"root": library / course_path, "title": course_title},
                                  previous["path"], (previous["size"], previous["mtime"]), previous["digest"]))
            paths |= retained_paths
            moved = {}
            for previous in existing.values():
                if previous["path"] not in paths and previous["id"] not in reclaimed:
                    moved.setdefault(previous["digest"], []).append(previous)
            new_counts = {}
            for _, relative, _, digest in found:
                if relative not in existing:
                    new_counts[digest] = new_counts.get(digest, 0) + 1
            with self.connect() as db:
                overrides = {row["key"][len(LESSON_OPTIONS_PREFIX):]: json.loads(row["value"])
                             for row in db.execute("SELECT key,value FROM settings WHERE key LIKE ?", (LESSON_OPTIONS_PREFIX + "%",))}
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
                    if relative not in retained_paths:
                        # A deliberate re-upload or external restore owns the
                        # source path again; never delete it on an old request.
                        db.execute("DELETE FROM reclaimed_sources WHERE lesson_id=?", (lesson_id,))
                    if previous:
                        db.execute(
                            """UPDATE lessons SET course_id=?,path=?,digest=?,size=?,mtime=?,deleted=0,position=?
                            WHERE id=?""",
                            (
                                course_id,
                                relative,
                                digest,
                                size,
                                mtime,
                                previous["position"]
                                if previous["course_id"] == course_id
                                else None,
                                lesson_id,
                            ),
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
                    lesson_options = {**options, **overrides.get(lesson_id, {})}
                    input_key = digest_json(
                        {
                            "source": digest,
                            "options": lesson_options,
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
                            "options": lesson_options,
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

    def configure_lesson(self, lesson_id: str, body: dict) -> str:
        """Save instruction chunking and enqueue a new attempt atomically.

        The running workflow's immutable spec/history is never patched. A build
        ID acts as a compare-and-swap token for concurrent retries or imports.
        """
        if not isinstance(body, dict) or set(body) != {"build_id", "chunk_minutes"}:
            raise ValueError("Expected build_id and chunk_minutes.")
        minutes = body["chunk_minutes"]
        if minutes is not None and (type(minutes) not in (int, float) or not math.isfinite(minutes) or not 1 <= minutes <= 120):
            raise ValueError("Section length must be between 1 and 120 minutes, or null for the server default.")
        with self.lock(), self.connect() as db:
            row = db.execute("""SELECT l.path,l.digest,b.* FROM lessons l JOIN builds b ON b.id=l.desired_build
                             WHERE l.id=? AND l.deleted=0""", (lesson_id,)).fetchone()
            if row is None:
                raise KeyError(lesson_id)
            if row["id"] != body["build_id"]:
                raise CatalogConflict("This lesson changed or was retried elsewhere. Reload the latest settings before saving.")
            if row["state"] in ACTIVE:
                raise CatalogConflict("This lesson is queued or processing. Cancel it before changing processing settings.")
            spec = json.loads(row["spec"])
            saved = db.execute("SELECT value FROM settings WHERE key='options'").fetchone()
            defaults = json.loads(saved[0]) if saved else spec["options"]
            override = {"chunk_minutes": float(minutes)} if minutes is not None else {}
            options = {**defaults, **override}
            key = LESSON_OPTIONS_PREFIX + lesson_id
            if override:
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, json.dumps(override)))
            else:
                db.execute("DELETE FROM settings WHERE key=?", (key,))
            spec.update(path=row["path"], digest=row["digest"], options=options)
            input_key = digest_json({"source": row["digest"], "options": options, "pipeline": PIPELINE_VERSION})
            return self._new_build(db, lesson_id, input_key, spec)

    def lesson_processing(self, lesson_id: str) -> dict:
        with self.lock(), self.connect() as db:
            row = db.execute("""SELECT l.path,b.* FROM lessons l JOIN builds b ON b.id=l.desired_build
                             WHERE l.id=? AND l.deleted=0""", (lesson_id,)).fetchone()
            if row is None:
                raise KeyError(lesson_id)
            spec = json.loads(row["spec"])
            options = spec["options"]
            saved = db.execute("SELECT value FROM settings WHERE key='options'").fetchone()
            defaults = json.loads(saved[0]) if saved else options
            override = db.execute("SELECT value FROM settings WHERE key=?", (LESSON_OPTIONS_PREFIX + lesson_id,)).fetchone()
            override = json.loads(override[0]) if override else {}
        duration = None
        try:
            snapshot = Path(options["out"]) / "_sources" / spec["digest"] / ("source" + Path(row["path"]).suffix.lower())
            cache = Path(options["work"]) / "lessons" / lesson_id / spec["digest"] / "probe.json"
            info = read_stage(cache, file_fingerprint(snapshot), {"stage": "probe"})
            value = info.get("duration") if isinstance(info, dict) else None
            if type(value) in (int, float) and math.isfinite(value) and value > 0:
                duration = value
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass  # The editor also works before probing or after cache cleanup.
        return {"id": lesson_id, "build_id": row["id"], "state": row["state"], "updated": row["updated"],
                "chunk_minutes": options.get("chunk_minutes", 25.0),
                "default_chunk_minutes": defaults.get("chunk_minutes", options.get("chunk_minutes", 25.0)),
                "custom_chunk_minutes": override.get("chunk_minutes"), "duration": duration}

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
                lesson["chapter_override"] = row["chapter_override"]
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
        from .provider_pause import ProviderPauses

        pauses = ProviderPauses(self)
        paused, waiting = pauses.active(), pauses.waiting()
        overrides = {row["key"][len(LESSON_OPTIONS_PREFIX):]: json.loads(row["value"])
                     for row in self.rows("SELECT key,value FROM settings WHERE key LIKE ?", (LESSON_OPTIONS_PREFIX + "%",))}
        defaults_row = self.rows("SELECT value FROM settings WHERE key='options'")
        defaults = json.loads(defaults_row[0]["value"]) if defaults_row else {}
        rows = self.rows("""SELECT l.id,l.course_id,l.path,l.slug,l.title,l.published,c.title AS course,c.slug AS course_slug,
            b.id AS build_id,b.state,b.stage,b.error,b.created,b.updated,b.spec,
            p.stage AS progress_stage,p.started AS stage_started,p.payload AS progress_payload
            FROM lessons l JOIN courses c ON c.id=l.course_id LEFT JOIN builds b ON b.id=l.desired_build
            LEFT JOIN build_progress p ON p.build_id=b.id
            WHERE l.deleted=0 ORDER BY b.created,b.id,l.id""")
        videos = []
        queue_position = 0
        stages = ["probe", "transcribe", "scenes", "steps", "frames", "publish"]
        for row in rows:
            options = json.loads(row["spec"]).get("options", {}) if row["spec"] else {}
            state = {"ready": "done", "running": "working"}.get(
                row["state"], row["state"] or "queued"
            )
            wait = waiting.get(row["build_id"])
            blocked = paused.get(wait["provider"]) if wait and state in {"queued", "working"} else None
            if state == "queued" and row["spec"]:
                provider = options.get("llm")
                blocked = blocked or paused.get(provider)
            if blocked:
                state = "blocked"
            if state == "queued":
                queue_position += 1
            measured = None
            if state == "working" and row["progress_stage"] == row["stage"] and row["progress_payload"]:
                measured = progress_snapshot(json.loads(row["progress_payload"]))
            stage_started = row["stage_started"] if measured else row["updated"]
            execution = None
            executions = []
            if state == "working":
                query = """SELECT t.id AS task_id,t.kind,t.state,t.error,t.spec,a.id AS attempt_id,
                    a.worker_id,w.name AS worker_name,a.progress
                    FROM compute_tasks t LEFT JOIN compute_attempts a ON a.id=t.current_attempt
                    LEFT JOIN workers w ON w.id=a.worker_id
                    WHERE t.build_id=? AND t.stage=? """
                activity = self.rows(query + """AND t.state IN ('running','pending','fallback','failed')
                    ORDER BY t.state='running' DESC,t.created,t.id""", (row["build_id"], row["stage"]))
                for entry in activity:
                    detail = {key: entry[key] for key in ("task_id", "kind", "state", "error", "attempt_id", "worker_id", "worker_name")}
                    detail["description"] = json.loads(entry["spec"]).get("description", "")
                    executions.append(detail)
                execution = {"worker_id": "server", "worker_name": "Library server", "state": "running"}
                if executions:
                    execution = executions[0]
                else:
                    recent = self.rows(query + "ORDER BY t.updated DESC,t.created DESC,t.id DESC LIMIT 1", (row["build_id"], row["stage"]))
                    if recent:
                        execution = {key: recent[0][key] for key in ("task_id", "kind", "state", "error", "attempt_id", "worker_id", "worker_name")}
            videos.append(
                {
                    "id": row["id"],
                    "build_id": row["build_id"],
                    "course": row["course"],
                    "course_id": row["course_id"],
                    "course_slug": row["course_slug"],
                    "title": row["title"] or Path(row["path"]).stem,
                    "source_name": Path(row["path"]).name,
                    "source_path": row["path"],
                    "queue_position": queue_position if state == "queued" else None,
                    "created": row["created"],
                    "updated": row["updated"],
                    "lesson_href": f"{row['course_slug']}/{row['slug']}.html"
                    if row["published"]
                    else None,
                    "state": state,
                    "blocked": blocked,
                    "processing": {
                        "chunk_minutes": options.get("chunk_minutes", 25.0),
                        "default_chunk_minutes": defaults.get("chunk_minutes", options.get("chunk_minutes", 25.0)),
                        "custom_chunk_minutes": overrides.get(row["id"], {}).get("chunk_minutes"),
                    },
                    "stage": row["stage"],
                    "label": ("Waiting for API credits or billing" if blocked else row["error"])
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
                    "stage_started": stage_started,
                    "progress": measured,
                    "execution": execution,
                    "executions": executions,
                    "elapsed": round(time.time() - (stage_started or time.time()), 1),
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
            "provider_pauses": list(paused.values()),
        }
