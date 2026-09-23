"""Library edits survive imports/restarts without changing sources or build intent."""

import http.client
import http.server
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from video_to_website import render
from video_to_website.catalog import SCHEMA_VERSION, Catalog, CatalogConflict
from video_to_website.durable import serialize_options
from video_to_website.ingest import IngestHandler
from video_to_website.pipeline import BuildOptions


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / "library"
        for course in ["01 Blender", "02 Drawing"]:
            for name in ["4.02 - Start.mp4", "4.03 - Next.mp4", "4.10 - Finish.mp4"]:
                path = self.library / course / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(str(path.relative_to(self.library)).encode())
        self.catalog = Catalog(self.root / "state")
        self.options = serialize_options(
            BuildOptions(out=self.root / "site", llm="heuristic")
        )
        self.catalog.reconcile(self.library, self.options)
        for row in self.catalog.rows("SELECT * FROM lessons"):
            record = {
                "id": row["id"],
                "slug": row["slug"],
                "title": "Create a rounded object",
                "source_name": Path(row["path"]).name,
                "steps": [],
                "duration": 60,
                "poster": None,
                "video_href": None,
                "summary": "A useful technique.",
                "prerequisites": [],
                "reading_key": row["id"] + ":instructions",
            }
            with self.catalog.lock():
                self.catalog.accept(row["desired_build"], record)
            self.catalog.update_build(row["desired_build"], "ready")

    def edit(self, resource, item, action, **body):
        return self.catalog.edit_library(
            resource,
            item,
            action,
            {"revision": self.catalog.library()["revision"], **body},
        )

    def test_filenames_are_titles_and_generated_titles_are_descriptions(self):
        course = self.catalog.published_courses()[0]
        lesson = course["lessons"][0]
        self.assertEqual(lesson["title"], "4.02 - Start")
        self.assertEqual(lesson["description"], "Create a rounded object")
        html = render.render_course_page(course)
        self.assertIn('<ol class="lesson-list">', html)
        self.assertIn('<div class="t">4.02 - Start</div>', html)
        self.assertIn('<p class="lesson-description">Create a rounded object</p>', html)
        self.assertNotIn('class="cards"', html)

    def test_titles_and_orders_survive_reconciliation_without_reprocessing(self):
        courses = self.catalog.library()["courses"]
        course, other = courses
        lesson = course["videos"][0]
        original_builds = self.catalog.rows("SELECT * FROM builds ORDER BY id")
        original_files = sorted(
            str(path.relative_to(self.library)) for path in self.library.rglob("*.mp4")
        )
        original_reading_key = self.catalog.published_courses()[0]["lessons"][0][
            "reading_key"
        ]
        self.edit("courses", course["id"], "title", title="Modelling essentials")
        self.edit("lessons", lesson["id"], "title", title="4.02 - My starting point")
        self.edit("courses", None, "order", ids=[other["id"], course["id"]])
        self.edit(
            "courses",
            course["id"],
            "order",
            ids=[v["id"] for v in reversed(course["videos"])],
        )
        self.catalog = Catalog(self.root / "state")
        self.catalog.reconcile(self.library, self.options)
        published = self.catalog.published_courses()
        self.assertEqual([c["id"] for c in published], [other["id"], course["id"]])
        self.assertEqual(published[1]["title"], "Modelling essentials")
        self.assertEqual(
            published[1]["lessons"][-1]["title"], "4.02 - My starting point"
        )
        self.assertEqual(
            published[1]["lessons"][-1]["reading_key"], original_reading_key
        )
        self.assertEqual(
            self.catalog.rows("SELECT * FROM builds ORDER BY id"), original_builds
        )
        self.assertEqual(
            sorted(
                str(p.relative_to(self.library)) for p in self.library.rglob("*.mp4")
            ),
            original_files,
        )

    def test_reset_order_and_title_restore_filename_defaults(self):
        course = self.catalog.library()["courses"][0]
        lesson = course["videos"][0]
        self.edit("lessons", lesson["id"], "title", title="Custom")
        self.edit("lessons", lesson["id"], "title", title=None)
        self.edit(
            "courses",
            course["id"],
            "order",
            ids=[v["id"] for v in reversed(course["videos"])],
        )
        self.edit("courses", course["id"], "order", mode="source")
        result = self.catalog.library()["courses"][0]
        self.assertEqual(
            [v["source_name"] for v in result["videos"]],
            ["4.02 - Start.mp4", "4.03 - Next.mp4", "4.10 - Finish.mp4"],
        )
        self.assertEqual(result["videos"][0]["title"], "4.02 - Start")
        self.assertFalse(result["videos"][0]["custom_title"])

    def test_new_and_nested_lessons_append_to_a_custom_order(self):
        course = self.catalog.library()["courses"][0]
        reversed_ids = [v["id"] for v in reversed(course["videos"])]
        self.edit("courses", course["id"], "order", ids=reversed_ids)
        source = self.library / "01 Blender" / "chapter" / "4.01 - Imported.mp4"
        source.parent.mkdir()
        source.write_bytes(b"nested lesson")
        self.catalog.reconcile(self.library, self.options)
        videos = self.catalog.library()["courses"][0]["videos"]
        self.assertEqual([v["id"] for v in videos[:3]], reversed_ids)
        self.assertEqual(videos[-1]["source_name"], "4.01 - Imported.mp4")
        self.edit("lessons", videos[-1]["id"], "title", title="Nested introduction")
        self.assertEqual(
            self.catalog.library()["courses"][0]["videos"][-1]["title"],
            "Nested introduction",
        )

    def test_stale_order_is_rejected_but_processing_updates_do_not_conflict(self):
        snapshot = self.catalog.library()
        course = snapshot["courses"][0]
        lesson_row = self.catalog.rows("SELECT * FROM lessons LIMIT 1")[0]
        self.catalog.update_build(lesson_row["desired_build"], "running")
        self.assertEqual(self.catalog.library()["revision"], snapshot["revision"])
        self.edit("courses", course["id"], "title", title="Edited in another tab")
        with self.assertRaises(CatalogConflict):
            self.catalog.edit_library(
                "courses",
                None,
                "order",
                {
                    "revision": snapshot["revision"],
                    "ids": [c["id"] for c in snapshot["courses"]],
                },
            )

    def test_partial_duplicate_and_foreign_orders_are_rejected(self):
        snapshot = self.catalog.library()
        course, other = snapshot["courses"]
        ids = [v["id"] for v in course["videos"]]
        for wanted in [
            ids[:2],
            [ids[0], ids[0], ids[2]],
            [ids[0], ids[1], other["videos"][0]["id"]],
            "invalid",
        ]:
            with self.subTest(wanted=wanted), self.assertRaises(ValueError):
                self.edit("courses", course["id"], "order", ids=wanted)
        self.assertEqual(snapshot, self.catalog.library())

    def test_title_validation_and_escaping(self):
        course = self.catalog.library()["courses"][0]
        for value in [None, [], "", "line\nbreak", "x" * 241]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.edit("courses", course["id"], "title", title=value)
        self.edit("courses", course["id"], "title", title='<script>alert("x")</script>')
        page = render.render_course_page(self.catalog.published_courses()[0])
        self.assertNotIn('<script>alert("x")</script>', page)
        self.assertIn("&lt;script&gt;", page)

    def test_previous_next_follow_saved_order(self):
        course = self.catalog.library()["courses"][0]
        self.edit(
            "courses",
            course["id"],
            "order",
            ids=[v["id"] for v in reversed(course["videos"])],
        )
        course = self.catalog.published_courses()[0]
        page = render.render_lesson_page(course["lessons"][1], course)
        self.assertIn("Previous: 4.10 - Finish", page)
        self.assertIn("Next: 4.02 - Start", page)

    def test_smart_sort_handles_mixed_numbering_and_numbered_renames(self):
        course = self.catalog.library()["courses"][0]
        a, b, c = course["videos"]
        self.edit("lessons", a["id"], "title", title="10-01 - Last chapter")
        self.edit("lessons", b["id"], "title", title="Renamed middle lesson")
        self.edit("lessons", c["id"], "title", title="4_02 - First lesson")
        original_builds = self.catalog.rows("SELECT * FROM builds ORDER BY id")
        result = self.edit("courses", course["id"], "order", mode="heuristic")
        ordered = result["courses"][0]["videos"]
        self.assertEqual([v["id"] for v in ordered], [c["id"], b["id"], a["id"]])
        self.assertEqual([v["numbering"] for v in ordered], [(4, 2), (4, 3), (10, 1)])
        self.catalog.reconcile(self.library, self.options)
        self.assertEqual(self.catalog.library()["courses"][0]["videos"], ordered)
        self.assertEqual(self.catalog.rows("SELECT * FROM builds ORDER BY id"), original_builds)
        published = self.catalog.published_courses()[0]
        page = render.render_lesson_page(published["lessons"][1], published)
        self.assertIn("Previous: 4_02 - First lesson", page)
        self.assertIn("Next: 10-01 - Last chapter", page)

    def test_title_and_filename_sorts_are_natural_and_keep_equal_names_stable(self):
        course = self.catalog.library()["courses"][0]
        a, b, c = course["videos"]
        for item, title in [(a, "Lesson 10"), (b, "Lesson 2"), (c, "Lesson 2")]:
            self.edit("lessons", item["id"], "title", title=title)
        self.edit("courses", course["id"], "order", ids=[c["id"], a["id"], b["id"]])
        result = self.edit("courses", course["id"], "order", mode="title")
        self.assertEqual([v["id"] for v in result["courses"][0]["videos"]], [c["id"], b["id"], a["id"]])
        result = self.edit("courses", course["id"], "order", mode="filename")
        self.assertEqual([v["id"] for v in result["courses"][0]["videos"]], [a["id"], b["id"], c["id"]])

    def test_filename_sort_ignores_subfolder_names(self):
        course = self.catalog.library()["courses"][0]
        original = [v["id"] for v in course["videos"]]
        for folder, filename in [("aaa", "10.01 - Late.mp4"), ("zzz", "1.01 - First.mp4")]:
            source = self.library / course["source_path"] / folder / filename
            source.parent.mkdir(parents=True)
            source.write_bytes(filename.encode())
        self.catalog.reconcile(self.library, self.options)
        result = self.edit("courses", course["id"], "order", mode="filename")
        videos = result["courses"][0]["videos"]
        self.assertEqual(videos[0]["source_name"], "1.01 - First.mp4")
        self.assertEqual(videos[-1]["source_name"], "10.01 - Late.mp4")
        self.assertEqual([v["id"] for v in videos[1:-1]], original)

    def test_duration_sort_keeps_unpublished_lessons_last(self):
        course = self.catalog.library()["courses"][0]
        a, b, c = course["videos"]
        with self.catalog.connect() as db:
            for item, duration in [(a, 120), (b, 30)]:
                record = json.loads(db.execute("SELECT published FROM lessons WHERE id=?", (item["id"],)).fetchone()[0])
                record["duration"] = duration
                db.execute("UPDATE lessons SET published=? WHERE id=?", (json.dumps(record), item["id"]))
            db.execute("UPDATE lessons SET published=NULL WHERE id=?", (c["id"],))
        for mode, expected in [("shortest", [b, a, c]), ("longest", [a, b, c])]:
            result = self.edit("courses", course["id"], "order", mode=mode)
            self.assertEqual([v["id"] for v in result["courses"][0]["videos"]], [v["id"] for v in expected])

    def test_course_name_sort_and_invalid_methods(self):
        a, b = self.catalog.library()["courses"]
        self.edit("courses", a["id"], "title", title="Course 10")
        self.edit("courses", b["id"], "title", title="Course 2")
        result = self.edit("courses", None, "order", mode="title")
        self.assertEqual([c["id"] for c in result["courses"]], [b["id"], a["id"]])
        before = self.catalog.library()
        for resource_id, mode in [(None, "shortest"), (a["id"], "typo"), (a["id"], []), (a["id"], False)]:
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.edit("courses", resource_id, "order", mode=mode)
        self.assertEqual(self.catalog.library(), before)
        with self.assertRaises(CatalogConflict):
            self.catalog.edit_library("courses", a["id"], "order", {"revision": "stale", "mode": "heuristic"})

    def test_sort_includes_new_imports_and_unnumbered_lessons(self):
        course = self.catalog.library()["courses"][0]
        ids = [v["id"] for v in course["videos"]]
        self.edit("courses", course["id"], "order", ids=list(reversed(ids)))
        for name in ["4-01 - New first.mp4", "Welcome.mp4", "Bonus 10.mp4", "Bonus 2.mp4"]:
            (self.library / course["source_path"] / name).write_bytes(name.encode())
        self.catalog.reconcile(self.library, self.options)
        result = self.edit("courses", course["id"], "order", mode="heuristic")
        videos = result["courses"][0]["videos"]
        self.assertEqual([v["source_name"] for v in videos], [
            "4-01 - New first.mp4", "4.02 - Start.mp4", "4.03 - Next.mp4", "4.10 - Finish.mp4",
            "Bonus 2.mp4", "Bonus 10.mp4", "Welcome.mp4",
        ])
        self.catalog = Catalog(self.root / "state")
        self.assertEqual(self.catalog.library()["courses"][0]["videos"], videos)

    def test_http_edit_publishes_immediately_and_rejects_stale_edits(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), IngestHandler)
        server.library, server.catalog, server.work = self.library, self.catalog, None
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(method, path, body=None):
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=5
            )
            try:
                conn.request(
                    method, path, json.dumps(body) if body is not None else None
                )
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()

        status, snapshot = request("GET", "/api/catalog")
        self.assertEqual(status, 200)
        course = snapshot["courses"][0]
        status, result = request(
            "POST",
            f"/api/catalog/courses/{course['id']}/title",
            {"revision": snapshot["revision"], "title": "Edited through the browser"},
        )
        self.assertEqual(status, 200)
        self.assertIsNone(result["warning"])
        self.assertIn(
            "Edited through the browser",
            (self.root / "site" / course["href"]).read_text(),
        )
        self.assertEqual(
            request(
                "POST",
                "/api/catalog/courses/order",
                {"revision": snapshot["revision"], "ids": []},
            )[0],
            409,
        )

    def test_bulk_chapter_moves_are_durable_metadata_and_can_be_reset(self):
        course = self.catalog.library()["courses"][0]
        a, b, c = course["videos"]
        before_builds = self.catalog.rows("SELECT * FROM builds ORDER BY id")
        before_files = sorted(str(p.relative_to(self.library)) for p in self.library.rglob("*.mp4"))
        keys = {v["id"]: v["reading_key"] for v in self.catalog.published_courses()[0]["lessons"]}
        result = self.edit("courses", course["id"], "chapter", ids=[c["id"], a["id"]], chapter=7)
        videos = result["courses"][0]["videos"]
        self.assertEqual([v["id"] for v in videos], [b["id"], a["id"], c["id"]])
        self.assertEqual([v["chapter"] for v in videos], [4, 7, 7])
        self.assertEqual([v["title"] for v in videos], [b["title"], a["title"], c["title"]])
        self.catalog = Catalog(self.root / "state")
        self.catalog.reconcile(self.library, self.options)
        self.assertEqual(self.catalog.library()["courses"][0]["videos"], videos)
        published = self.catalog.published_courses()[0]
        self.assertIn('Chapter 7', render.render_course_page(published))
        self.assertEqual({v["id"]: v["reading_key"] for v in published["lessons"]}, keys)
        self.assertEqual(self.catalog.rows("SELECT * FROM builds ORDER BY id"), before_builds)
        self.assertEqual(sorted(str(p.relative_to(self.library)) for p in self.library.rglob("*.mp4")), before_files)
        self.edit("courses", course["id"], "order", mode="heuristic")
        result = self.edit("courses", course["id"], "chapter", ids=[a["id"], c["id"]], chapter=None)
        self.assertTrue(all(v["chapter"] == 4 and v["chapter_override"] is None for v in result["courses"][0]["videos"]))

    def test_bulk_assignment_appends_to_existing_chapter_and_supports_other(self):
        course = self.catalog.library()["courses"][0]
        a, b, c = course["videos"]
        self.edit("courses", course["id"], "chapter", ids=[b["id"]], chapter=0)
        result = self.edit("courses", course["id"], "chapter", ids=[a["id"], c["id"]], chapter=0)
        self.assertEqual([v["id"] for v in result["courses"][0]["videos"]], [b["id"], a["id"], c["id"]])
        result = self.edit("courses", course["id"], "chapter", ids=[a["id"], c["id"]], chapter=-1)
        self.assertEqual([v["chapter"] for v in result["courses"][0]["videos"]], [0, None, None])
        self.assertIn('Other lessons', render.render_course_page(self.catalog.published_courses()[0]))

    def test_bulk_move_preserves_selection_order_and_untouched_lessons(self):
        course = self.catalog.library()["courses"][0]
        a, b, c = course["videos"]
        result = self.edit("courses", course["id"], "move", ids=[c["id"], a["id"]], before=b["id"])
        self.assertEqual([v["id"] for v in result["courses"][0]["videos"]], [a["id"], c["id"], b["id"]])
        result = self.edit("courses", course["id"], "move", ids=[a["id"], c["id"]], before=None)
        self.assertEqual([v["id"] for v in result["courses"][0]["videos"]], [b["id"], a["id"], c["id"]])
        self.assertTrue(all(v["chapter_override"] is None for v in result["courses"][0]["videos"]))

    def test_bulk_operations_validate_complete_selection_before_writing(self):
        course, other = self.catalog.library()["courses"]
        a, b, c = course["videos"]
        before = self.catalog.library()
        for ids in [[], [a["id"], a["id"]], [a["id"], "missing"], [other["videos"][0]["id"]], [7], "bad"]:
            for action, payload in [("chapter", {"chapter": 4}), ("move", {"before": None})]:
                with self.subTest(ids=ids, action=action), self.assertRaises(ValueError):
                    self.edit("courses", course["id"], action, ids=ids, **payload)
        for chapter in [True, "4", -2, 1000, 4.5, []]:
            with self.subTest(chapter=chapter), self.assertRaises(ValueError):
                self.edit("courses", course["id"], "chapter", ids=[a["id"]], chapter=chapter)
        for target in [a["id"], "missing", [], other["videos"][0]["id"]]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.edit("courses", course["id"], "move", ids=[a["id"]], before=target)
        for action in ["chapter", "move"]:
            with self.assertRaises(ValueError):
                self.edit("courses", course["id"], action, ids=[a["id"]])
        self.assertEqual(self.catalog.library(), before)
        self.edit("courses", course["id"], "chapter", ids=[a["id"]], chapter=6)
        with self.assertRaises(CatalogConflict):
            self.catalog.edit_library("courses", course["id"], "move", {"revision": before["revision"], "ids": [b["id"], c["id"]], "before": None})

    def test_v5_migration_preserves_saved_order_and_defaults_to_automatic(self):
        before = self.catalog.library()
        with self.catalog.connect() as db:
            db.execute("ALTER TABLE lessons DROP COLUMN chapter_override")
            db.execute("DROP TABLE reclaimed_sources")
            db.execute("PRAGMA user_version=5")
            db.execute("DROP TABLE reading_state")
            db.execute("DROP TABLE reader_preferences"); db.execute("DROP TABLE reader_views")
        migrated = Catalog(self.catalog.directory)
        self.assertEqual(migrated.library(), before)
        self.assertEqual(migrated.rows("PRAGMA user_version")[0]["user_version"], SCHEMA_VERSION)


class MigrationTests(unittest.TestCase):
    def test_v1_catalog_migrates_without_losing_existing_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            with sqlite3.connect(path / "catalog.sqlite") as db:
                db.executescript("""CREATE TABLE courses (id TEXT PRIMARY KEY,path TEXT,slug TEXT,title TEXT);
                    CREATE TABLE lessons (id TEXT PRIMARY KEY,course_id TEXT,path TEXT,slug TEXT,digest TEXT,size INTEGER,mtime INTEGER,deleted INTEGER DEFAULT 0,desired_key TEXT,desired_build TEXT,published TEXT);
                    CREATE TABLE builds (id TEXT PRIMARY KEY,lesson_id TEXT,input_key TEXT,spec TEXT,state TEXT,stage TEXT,error TEXT,created REAL,updated REAL);
                    CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT);
                    INSERT INTO courses VALUES ('course','folder','course','Course title');
                    INSERT INTO lessons VALUES ('lesson','course','folder/4.02 - Something.mp4','lesson','hash',1,2,0,'key','job',NULL);
                    PRAGMA user_version=1;""")
            migrated = Catalog(path)
            course = migrated.library()["courses"][0]
            self.assertEqual(course["id"], "course")
            self.assertEqual(course["videos"][0]["id"], "lesson")
            self.assertEqual(course["videos"][0]["title"], "4.02 - Something")
            self.assertEqual(migrated.rows("PRAGMA user_version")[0]["user_version"], SCHEMA_VERSION)
            Catalog(path)  # Migration is idempotent across API/worker restarts.
