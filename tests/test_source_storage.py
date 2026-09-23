"""Reclamation preserves the authoritative video, library identity and retries."""

import http.client
import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from video_to_website.catalog import Catalog, CatalogConflict, SCHEMA_VERSION
from video_to_website.durable import prepare_source, publish, serialize_options
from video_to_website.ingest import IngestHandler, library_listing
from video_to_website.pipeline import BuildOptions
from video_to_website.publication import refresh_site
from video_to_website.source_storage import annotate_sources, reclaim_source


class SourceStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.library = self.root / "library"
        self.original = self.library / "Course" / "4.01 - Intro.mp4"
        self.original.parent.mkdir(parents=True); self.original.write_bytes(b"original video")
        self.catalog = Catalog(self.root / "state")
        self.options = serialize_options(BuildOptions(out=self.root / "site", work=self.root / "work", llm="heuristic"))
        self.catalog.reconcile(self.library, self.options)
        self.row = self.catalog.rows("SELECT * FROM lessons")[0]
        self.build = self.catalog.build(self.row["desired_build"])
        self.spec = json.loads(self.build["spec"])
        self.body = {"build_id": self.build["id"]}
        self.patcher = patch("video_to_website.durable.whisper.resolve_model", return_value=self.root / "model")
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        source = prepare_source(self.spec); self.snapshot = Path(source["video"])
        publish(self.spec, {"slug": self.row["slug"], "title": "An introduction", "steps": [],
                           "duration": 30, "poster": None, "summary": "", "prerequisites": [], "video_only": True}, source)

    def reclaim(self, body=None):
        return reclaim_source(self.catalog, self.library, self.row["id"], body or self.body)

    def listing(self):
        return annotate_sources(self.catalog, self.library, library_listing(self.library))

    def test_reclaim_survives_scan_restart_and_missing_course_folder(self):
        before = self.catalog.published_courses()
        result = self.reclaim()
        self.assertEqual(result["removed_bytes"], len(b"original video"))
        self.assertFalse(self.original.exists()); self.assertEqual(self.snapshot.read_bytes(), b"original video")
        self.original.parent.rmdir()
        self.catalog = Catalog(self.root / "state")
        self.catalog.reconcile(self.library, self.options)
        self.assertEqual(self.catalog.published_courses(), before)
        self.assertEqual(len(self.catalog.rows("SELECT * FROM builds")), 1)
        self.assertTrue(self.catalog.library()["courses"][0]["videos"][0]["source_reclaimed"])
        with self.catalog.lock(): refresh_site(self.catalog, Path(self.options["out"]))
        page = Path(self.options["out"]) / before[0]["slug"] / (self.row["slug"] + ".html")
        self.assertTrue(page.is_file())
        self.assertEqual((page.parent / before[0]["lessons"][0]["video_href"]).resolve(), self.snapshot)
        video = self.listing()[0]["videos"][0]
        self.assertTrue(video["source_reclaimed"]); self.assertEqual(video["bytes"], 0)
        self.assertTrue(video["href"]); self.assertFalse(video["reclaimable"])
        self.assertTrue(self.reclaim()["already_reclaimed"])

    def test_retry_and_changed_output_directory_use_retained_video(self):
        self.reclaim()
        retry = self.catalog.retry(self.row["id"])
        source = prepare_source(json.loads(self.catalog.build(retry)["spec"]))
        self.assertEqual(Path(source["video"]), self.snapshot)
        changed = {**self.options, "out": str(self.root / "new-site"), "chunk_minutes": 4}
        self.catalog.reconcile(self.library, changed)
        new_build = self.catalog.rows("SELECT desired_build FROM lessons")[0]["desired_build"]
        self.assertNotEqual(new_build, retry)
        rebuilt = prepare_source(json.loads(self.catalog.build(new_build)["spec"]))
        self.assertEqual(Path(rebuilt["video"]).read_bytes(), b"original video")
        self.assertTrue(Path(rebuilt["video"]).is_relative_to(self.root / "new-site"))
        self.assertEqual(self.catalog.build(self.build["id"])["spec"], self.build["spec"])
        self.assertFalse(self.original.exists())

    def test_duplicate_import_does_not_steal_reclaimed_identity(self):
        self.reclaim()
        self.original.with_name("4.02 - Duplicate.mp4").write_bytes(b"original video")
        self.catalog.reconcile(self.library, self.options)
        rows = self.catalog.rows("SELECT * FROM lessons WHERE deleted=0")
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(r for r in rows if r["id"] == self.row["id"])["path"], self.row["path"])

    def test_restore_same_path_clears_reclamation_and_invalidates_old_request(self):
        self.reclaim()
        # Even same size/mtime must be hashed when a reclaimed path reappears.
        self.original.write_bytes(b"different data")
        os.utime(self.original, ns=(self.row["mtime"], self.row["mtime"]))
        self.catalog.reconcile(self.library, self.options)
        after = self.catalog.rows("SELECT * FROM lessons")[0]
        self.assertEqual(after["id"], self.row["id"])
        self.assertNotEqual(after["digest"], self.row["digest"])
        self.assertFalse(self.catalog.rows("SELECT * FROM reclaimed_sources"))
        with self.assertRaises(CatalogConflict): self.reclaim()
        self.assertTrue(self.original.exists())

    def test_renamed_extension_uses_snapshot_from_publication(self):
        renamed = self.original.with_suffix(".mov"); self.original.rename(renamed)
        self.catalog.reconcile(self.library, self.options)
        self.reclaim(); self.assertFalse(renamed.exists()); self.assertTrue(self.snapshot.exists())
        retry = self.catalog.retry(self.row["id"])
        source = prepare_source(json.loads(self.catalog.build(retry)["spec"]))
        self.assertEqual(Path(source["video"]).suffix, ".mov")
        self.assertEqual(Path(source["video"]).read_bytes(), b"original video")

    def test_active_failed_unpublished_and_stale_builds_keep_original(self):
        for state in ["queued", "running", "failed", "cancelled"]:
            with self.catalog.connect() as db: db.execute("UPDATE builds SET state=?", (state,))
            with self.subTest(state=state), self.assertRaises(CatalogConflict): self.reclaim()
            self.assertTrue(self.original.exists())
        with self.catalog.connect() as db: db.execute("UPDATE builds SET state='ready'")
        with self.assertRaises(CatalogConflict): self.reclaim({"build_id": "stale"})
        with self.catalog.connect() as db: db.execute("UPDATE lessons SET published=NULL")
        with self.assertRaises(CatalogConflict): self.reclaim()
        self.assertTrue(self.original.exists()); self.assertFalse(self.catalog.rows("SELECT * FROM reclaimed_sources"))

    def test_missing_corrupt_or_linked_snapshot_keeps_original(self):
        self.snapshot.unlink()
        with self.assertRaises(CatalogConflict): self.reclaim()
        self.snapshot.write_bytes(b"corrupt video!")
        with self.assertRaises(CatalogConflict): self.reclaim()
        self.snapshot.unlink(); self.snapshot.symlink_to(self.original)
        with self.assertRaises(CatalogConflict): self.reclaim()
        self.snapshot.unlink(); os.link(self.original, self.snapshot)
        with self.assertRaises(CatalogConflict): self.reclaim()
        self.assertTrue(self.original.exists()); self.assertFalse(self.catalog.rows("SELECT * FROM reclaimed_sources"))

    def test_changed_source_and_changes_during_verification_keep_original(self):
        self.original.write_bytes(b"different data")
        os.utime(self.original, ns=(self.row["mtime"], self.row["mtime"]))
        with self.assertRaises(CatalogConflict): self.reclaim()
        self.original.write_bytes(b"original video")
        os.utime(self.original, ns=(self.row["mtime"], self.row["mtime"]))
        from video_to_website.source_storage import digest_file
        def changing(path):
            result = digest_file(path)
            if path == self.original: self.original.write_bytes(b"changed after verification")
            return result
        with patch("video_to_website.source_storage.digest_file", side_effect=changing):
            with self.assertRaises(CatalogConflict): self.reclaim()
        self.assertTrue(self.original.exists()); self.assertFalse(self.catalog.rows("SELECT * FROM reclaimed_sources"))

    def test_crashes_before_and_after_unlink_preserve_lesson(self):
        class Crash(BaseException): pass
        unlink = Path.unlink
        def crash(path, *args, **kwargs):
            if path == self.original: raise Crash("simulated interruption before unlink")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", crash):
            with self.assertRaises(Crash): self.reclaim()
        self.assertTrue(self.original.exists())
        self.assertTrue(self.catalog.rows("SELECT * FROM reclaimed_sources"))
        self.catalog.reconcile(self.library, self.options)
        self.assertFalse(self.catalog.rows("SELECT * FROM reclaimed_sources"))
        def crash_after(path, *args, **kwargs):
            result = unlink(path, *args, **kwargs)
            if path == self.original: raise Crash("simulated interruption after unlink")
            return result
        with patch.object(Path, "unlink", crash_after):
            with self.assertRaises(Crash): self.reclaim()
        self.catalog = Catalog(self.root / "state")
        self.catalog.reconcile(self.library, self.options)
        self.assertTrue(self.catalog.published_courses()); self.assertTrue(self.reclaim()["already_reclaimed"])

    def test_unlink_permission_error_does_not_claim_space_was_reclaimed(self):
        unlink = Path.unlink
        def denied(path, *args, **kwargs):
            if path == self.original: raise PermissionError("read-only course")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", denied):
            with self.assertRaises(PermissionError): self.reclaim()
        self.assertTrue(self.original.exists()); self.assertFalse(self.catalog.rows("SELECT * FROM reclaimed_sources"))
        self.assertFalse(self.catalog.library()["courses"][0]["videos"][0]["source_reclaimed"])

    def test_retry_during_verification_does_not_block_or_remove_the_original(self):
        from video_to_website.source_storage import digest_file
        retried = []
        def concurrent_retry(path):
            if not retried:
                retried.append(self.catalog.retry(self.row["id"]))
            return digest_file(path)
        with patch("video_to_website.source_storage.digest_file", side_effect=concurrent_retry):
            with self.assertRaisesRegex(CatalogConflict, "lesson changed during verification"):
                self.reclaim()
        self.assertTrue(self.original.exists()); self.assertEqual(len(retried), 1)
        self.assertFalse(self.catalog.rows("SELECT * FROM reclaimed_sources"))

    def test_ordinary_missing_file_still_removes_lesson(self):
        self.original.unlink(); self.catalog.reconcile(self.library, self.options)
        self.assertFalse(self.catalog.published_courses())

    def test_nested_source_is_listed_and_can_be_reclaimed(self):
        nested = self.original.parent / "chapter" / self.original.name
        nested.parent.mkdir(); self.original.rename(nested)
        self.catalog.reconcile(self.library, self.options)
        video = self.listing()[0]["videos"][0]
        self.assertEqual(video["name"], "chapter/" + self.original.name)
        self.assertTrue(video["reclaimable"]); self.assertFalse(video["file_actions"])
        self.reclaim(); self.catalog.reconcile(self.library, self.options)
        self.assertTrue(self.catalog.published_courses()); self.assertFalse(nested.exists())

    def test_listing_handles_api_move_before_course_reconciliation(self):
        moved = self.library / "Another course" / self.original.name
        moved.parent.mkdir(); self.original.rename(moved)
        # The existing rename endpoint updates the lesson path immediately;
        # its course_id is updated later by reconciliation.
        with self.catalog.connect() as db:
            db.execute("UPDATE lessons SET path=?", (str(moved.relative_to(self.library)),))
        course = next(c for c in self.listing() if c["name"] == "Another course")
        self.assertEqual(len(course["videos"]), 1)
        self.assertTrue(course["videos"][0]["reclaimable"])
        self.reclaim(); self.assertFalse(moved.exists())
        self.catalog.reconcile(self.library, self.options)
        self.assertEqual(self.catalog.library()["courses"][0]["source_path"], "Another course")

    def test_v6_upgrade_preserves_completed_and_running_builds(self):
        before = self.catalog.rows("SELECT * FROM builds")
        with self.catalog.connect() as db:
            db.execute("DROP TABLE reclaimed_sources"); db.execute("PRAGMA user_version=6")
            db.execute("DROP TABLE reading_state")
            db.execute("DROP TABLE reader_preferences"); db.execute("DROP TABLE reader_views")
        self.catalog = Catalog(self.root / "state")
        self.assertEqual(self.catalog.rows("PRAGMA user_version")[0]["user_version"], SCHEMA_VERSION)
        self.assertEqual(self.catalog.rows("SELECT * FROM builds"), before)
        self.reclaim(); self.assertTrue(self.snapshot.exists())

    def test_http_reclaim_replace_and_delete_remain_distinct(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), IngestHandler)
        server.library, server.catalog, server.min_free_bytes = self.library, self.catalog, 0
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        def request(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                conn.request(method, path, json.dumps(body) if isinstance(body, (dict, list)) else body)
                res = conn.getresponse(); return res.status, json.loads(res.read())
            finally: conn.close()
        endpoint = "/api/lessons/" + self.row["id"] + "/reclaim"
        from urllib.parse import quote
        source_path = "/api/library/" + quote(self.row["path"])
        self.assertTrue(request("GET", "/api/library")[1]["courses"][0]["videos"][0]["reclaimable"])
        for body in [[], {"snapshot": "/elsewhere"}, {"build_id": 3}]:
            self.assertEqual(request("POST", endpoint, body)[0], 400)
        server.uploads_enabled = False
        self.assertEqual(request("POST", endpoint, self.body)[0], 403)
        server.uploads_enabled = True
        self.assertEqual(request("POST", endpoint, self.body)[0], 200)
        self.assertEqual(request("POST", endpoint, self.body)[1]["removed_bytes"], 0)
        self.assertEqual(request("PUT", source_path, b"replacement")[0], 409)
        # Explicit deletion remains available even when its original is absent.
        self.assertEqual(request("DELETE", source_path)[0], 200)
        self.assertFalse(self.catalog.published_courses()); self.assertTrue(self.snapshot.exists())
        self.assertEqual(request("PUT", source_path, b"replacement")[0], 201)
        self.catalog.reconcile(self.library, self.options)
        self.assertFalse(self.catalog.rows("SELECT * FROM reclaimed_sources"))
        self.assertEqual(request("POST", endpoint, self.body)[0], 409)


if __name__ == "__main__": unittest.main()
