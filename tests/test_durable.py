"""Integration boundaries: catalog, real DBOS recovery, and immutable publication."""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from video_to_website import media, whisper
from video_to_website.catalog import Catalog
from video_to_website.durable import DurableWorker, serialize_options
from video_to_website.pipeline import BuildOptions, process_video
from video_to_website.util import BuildCancelled, CommandError, process_control, run


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / "library"
        self.video = self.library / "course" / "01 first.mp4"
        self.video.parent.mkdir(parents=True)
        self.video.write_bytes(b"first source")
        self.catalog = Catalog(self.root / "state")
        self.options = serialize_options(
            BuildOptions(out=self.root / "site", llm="heuristic")
        )

    def scan(self):
        self.catalog.reconcile(self.library, self.options)
        return self.catalog.rows("SELECT * FROM lessons WHERE deleted=0")

    def test_restart_and_rename_preserve_identity_and_build(self):
        original = self.scan()[0]
        self.video.rename(self.video.with_name("02 renamed.mp4"))
        self.catalog = Catalog(self.root / "state")
        renamed = self.scan()[0]
        self.assertEqual(original["id"], renamed["id"])
        self.assertEqual(original["desired_build"], renamed["desired_build"])
        self.assertEqual(len(self.catalog.rows("SELECT * FROM builds")), 1)

    def test_source_replacement_supersedes_running_work(self):
        original = self.scan()[0]
        self.catalog.update_build(original["desired_build"], "running")
        self.video.write_bytes(b"replacement with different bytes")
        replaced = self.scan()[0]
        self.assertEqual(original["id"], replaced["id"])
        self.assertNotEqual(original["desired_build"], replaced["desired_build"])
        self.assertFalse(self.catalog.is_current(original["desired_build"]))

    def test_settings_change_creates_a_revision(self):
        before = self.scan()[0]
        self.options["language"] = "auto"
        self.assertNotEqual(before["desired_build"], self.scan()[0]["desired_build"])

    def test_final_deletion_reconciles_empty_catalog(self):
        before = self.scan()[0]
        self.video.unlink()
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.catalog.published_courses(), [])
        self.assertFalse(self.catalog.is_current(before["desired_build"]))

    def test_failure_requires_explicit_retry_and_cancel_survives_scan(self):
        lesson = self.scan()[0]
        self.catalog.update_build(lesson["desired_build"], "failed", error="bad input")
        self.assertEqual(self.scan()[0]["desired_build"], lesson["desired_build"])
        retry = self.catalog.retry(lesson["id"])
        self.assertNotEqual(retry, lesson["desired_build"])
        self.catalog.cancel(lesson["id"])
        self.scan()
        self.assertFalse(self.catalog.is_current(retry))

    def test_duplicate_imports_get_distinct_ids(self):
        self.video.with_name("02 copy.mp4").write_bytes(self.video.read_bytes())
        lessons = self.scan()
        self.assertEqual(len({lesson["id"] for lesson in lessons}), 2)

    def test_newer_schema_is_not_silently_downgraded(self):
        with self.catalog.connect() as db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(RuntimeError, "newer"):
            Catalog(self.root / "state")

    def test_queue_status_includes_order_source_and_published_link(self):
        for name in ("02 next.mp4", "03 last.mp4"):
            self.video.with_name(name).write_bytes(name.encode())
        lessons = self.scan()
        status = self.catalog.status()["videos"]
        self.assertEqual([v["queue_position"] for v in status], [1, 2, 3])
        self.assertEqual(status[0]["source_path"], "course/01 first.mp4")
        self.assertEqual(status[0]["source_name"], "01 first.mp4")
        self.assertIsNone(status[0]["lesson_href"])
        self.catalog.update_build(lessons[0]["desired_build"], "running", stage="transcribe")
        status = self.catalog.status()["videos"]
        self.assertEqual([v["queue_position"] for v in status], [None, 1, 2])
        self.catalog.cancel(lessons[1]["id"])
        with self.catalog.lock():
            self.catalog.accept(lessons[0]["desired_build"], {"title": "Published"})
        status = self.catalog.status()["videos"]
        self.assertEqual([v["queue_position"] for v in status], [None, None, 1])
        self.assertTrue(status[0]["lesson_href"].endswith(lessons[0]["slug"] + ".html"))
        self.assertEqual(status[0]["build_id"], lessons[0]["desired_build"])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / "library"
        self.video = self.library / "course" / "first.mp4"
        self.video.parent.mkdir(parents=True)
        self.video.write_bytes(b"original video")
        self.options = BuildOptions(
            out=self.root / "site",
            work=self.root / "work",
            llm="heuristic",
            clips="none",
        )
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        model = self.root / "model.bin"
        model.touch()
        self.stack.enter_context(
            patch.object(whisper, "resolve_model", return_value=model)
        )
        self.transcribe = self.stack.enter_context(
            patch.object(
                whisper,
                "transcribe",
                return_value=[
                    {"start": 0.0, "end": 10.0, "text": "Create the first object."}
                ],
            )
        )
        self.probe = self.stack.enter_context(
            patch.object(
                media,
                "probe",
                return_value={
                    "duration": 10.0,
                    "width": 100,
                    "height": 100,
                    "has_audio": True,
                },
            )
        )
        self.stack.enter_context(patch.object(media, "extract_audio"))
        self.stack.enter_context(patch.object(media, "detect_scenes", return_value=[]))
        self.stack.enter_context(patch.object(media, "frame_hash", return_value=None))
        self.frames = self.stack.enter_context(
            patch.object(media, "extract_frame", side_effect=self.image)
        )

    @staticmethod
    def image(video, timestamp, target, **kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test image")

    def wait(self, worker, condition):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            worker.tick()
            if condition():
                return
            time.sleep(0.05)
        self.fail(f"timed out: {worker.catalog.rows('SELECT state,error FROM builds')}")

    def test_rename_then_failed_replacement_preserves_published_revision(self):
        with DurableWorker(self.library, self.options, self.root / "state") as worker:
            self.wait(worker, lambda: bool(worker.catalog.published_courses()))
            course = worker.catalog.published_courses()[0]
            lesson = course["lessons"][0]
            page = self.options.out / course["slug"] / (lesson["slug"] + ".html")
            asset = (page.parent / lesson["poster"]).resolve()
            original_source = (page.parent / lesson["video_href"]).resolve()
            self.assertTrue(asset.exists())
            markdown = page.parent / "md" / (lesson["slug"] + ".md")
            self.assertIn("(../" + lesson["poster"] + ")", markdown.read_text())
            self.assertEqual(original_source.read_bytes(), b"original video")
            renamed = self.video.with_name("renamed.mp4")
            self.video.rename(renamed)
            worker.tick()
            worker.tick()
            changed = worker.catalog.published_courses()[0]["lessons"][0]
            self.assertEqual(changed["id"], lesson["id"])
            self.assertEqual(changed["reading_key"], lesson["reading_key"])
            self.assertTrue(asset.exists())
            self.assertEqual(self.transcribe.call_count, 1)
            renamed.write_bytes(b"new source data")
            self.frames.side_effect = CommandError("simulated encoding failure")
            self.wait(
                worker,
                lambda: bool(
                    worker.catalog.rows("SELECT 1 FROM builds WHERE state='failed'")
                ),
            )
            retained = worker.catalog.published_courses()[0]["lessons"][0]
            self.assertEqual(retained["revision"], lesson["revision"])
            self.assertEqual(original_source.read_bytes(), b"original video")
            self.assertTrue(page.exists())
            self.assertTrue(asset.exists())

    def test_bad_lesson_does_not_block_next_lesson(self):
        self.video.with_name("second.mp4").write_bytes(b"second video")
        self.probe.side_effect = [
            CommandError("corrupt video"),
            self.probe.return_value,
        ]
        with DurableWorker(self.library, self.options, self.root / "state") as worker:
            self.wait(
                worker,
                lambda: (
                    {
                        r["state"]
                        for r in worker.catalog.rows("SELECT state FROM builds")
                    }
                    == {"ready", "failed"}
                ),
            )
            self.assertEqual(len(worker.catalog.published_courses()[0]["lessons"]), 1)

    def test_reclaimed_upload_reprocesses_after_worker_restart(self):
        from video_to_website.source_storage import reclaim_source

        with DurableWorker(self.library, self.options, self.root / "state") as worker:
            self.wait(worker, lambda: bool(worker.catalog.rows("SELECT * FROM builds WHERE state='ready'")))
            row = worker.catalog.rows("SELECT * FROM lessons")[0]
            before = worker.catalog.published_courses()[0]["lessons"][0]
            reclaim_source(worker.catalog, self.library, row["id"], {"build_id": row["desired_build"]})
        self.assertFalse(self.video.exists())
        with DurableWorker(self.library, self.options, self.root / "state") as worker:
            worker.tick(); worker.tick()
            self.assertEqual(worker.catalog.published_courses()[0]["lessons"][0]["revision"], before["revision"])
            new_id = worker.catalog.configure_lesson(row["id"], {"build_id": row["desired_build"], "chunk_minutes": 5})
            self.wait(worker, lambda: worker.catalog.build(new_id)["state"] == "ready")
            after = worker.catalog.published_courses()[0]["lessons"][0]
            self.assertEqual(after["video_href"], before["video_href"])
            self.assertEqual(after["reading_key"], before["reading_key"])
            self.assertNotEqual(after["revision"], before["revision"])
            self.assertEqual(self.transcribe.call_count, 1)
            self.assertFalse(self.video.exists())

    def test_live_transcription_progress_is_visible_before_stage_finishes(self):
        entered = threading.Event()
        release = threading.Event()
        segments = self.transcribe.return_value

        def slow_transcribe(*args, **kwargs):
            whisper._report_progress("whisper_print_progress_callback: progress = 25%")
            entered.set()
            if not release.wait(10):
                raise RuntimeError("progress test gate timed out")
            return segments

        self.transcribe.side_effect = slow_transcribe
        with DurableWorker(self.library, self.options, self.root / "state") as worker:
            try:
                self.wait(worker, entered.is_set)
                status = worker.catalog.status()["videos"][0]
                self.assertEqual(status["state"], "working")
                self.assertEqual(status["progress"]["fraction"], .25)
                self.assertEqual(status["progress"]["phase"], "speech")
            finally:
                release.set()
            self.wait(worker, lambda: bool(worker.catalog.published_courses()))

    def test_stopping_an_active_media_step_leaves_it_recoverable(self):
        entered = threading.Event()

        def slow_probe(*args):
            entered.set()
            run([sys.executable, "-c", "import time; time.sleep(30)"])
            return self.probe.return_value

        self.probe.side_effect = slow_probe
        started = time.monotonic()
        with DurableWorker(self.library, self.options, self.root / "state") as worker:
            self.wait(worker, entered.is_set)
        self.assertLess(time.monotonic() - started, 10)
        self.probe.side_effect = None
        with DurableWorker(self.library, self.options, self.root / "state") as worker:
            self.wait(worker, lambda: bool(worker.catalog.published_courses()))
            self.assertEqual(len(worker.catalog.rows("SELECT * FROM builds")), 1)

    def test_transcript_changes_invalidate_generated_instructions(self):
        arguments = dict(
            work_dir=self.root / "cache",
            course_dir=self.root / "output",
            slug="lesson",
            options=self.options,
            backend=None,
            model_path=self.root / "model.bin",
        )
        first = process_video(self.video, **arguments)
        self.transcribe.return_value = [
            {"start": 0.0, "end": 10.0, "text": "Create a different object."}
        ]
        self.options.force = {"transcribe"}
        second = process_video(self.video, **arguments)
        self.assertNotEqual(first["steps"][0]["actions"], second["steps"][0]["actions"])
        self.assertEqual(self.transcribe.call_count, 2)

    def test_final_deletion_removes_current_page_and_navigation(self):
        with DurableWorker(self.library, self.options, self.root / "state") as worker:
            self.wait(worker, lambda: bool(worker.catalog.published_courses()))
            course = worker.catalog.published_courses()[0]
            page = (
                self.options.out
                / course["slug"]
                / (course["lessons"][0]["slug"] + ".html")
            )
            self.video.unlink()
            worker.tick()
            worker.tick()
            self.assertEqual(
                json.loads((self.options.out / "site.json").read_text()), []
            )
            self.assertFalse(page.exists())


class RecoveryTests(unittest.TestCase):
    def test_killed_worker_recovers_real_dbos_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = root / "library" / "course"
            course.mkdir(parents=True)
            (course / "lesson.mp4").write_bytes(b"video")
            command = [
                sys.executable,
                str(Path(__file__).with_name("durable_fixture.py")),
                str(root),
            ]
            with (root / "worker.log").open("w+") as output:
                process = subprocess.Popen(command, stdout=output, stderr=output)
                try:
                    deadline = time.monotonic() + 20
                    while (
                        not (root / "entered-scenes").exists()
                        and time.monotonic() < deadline
                    ):
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                    self.assertTrue(
                        (root / "entered-scenes").exists(),
                        "worker did not reach the crash point",
                    )
                finally:
                    process.kill()
                    process.wait(timeout=5)
                # Prove that recovery comes from DBOS history, not just file caches.
                for cache in (root / "work").rglob("transcript.json"):
                    cache.unlink()
                (root / "release-scenes").touch()
                result = subprocess.run(
                    command, stdout=output, stderr=output, timeout=25
                )
                output.seek(0)
                self.assertEqual(result.returncode, 0, output.read())
            self.assertEqual(
                (root / "transcriptions").read_text().splitlines(), ["transcribe"]
            )
            catalog = Catalog(root / "state")
            self.assertEqual(len(catalog.rows("SELECT * FROM builds")), 1)
            self.assertEqual(
                catalog.rows("SELECT state FROM builds")[0]["state"], "ready"
            )

    def test_subprocess_cancellation_reaps_the_process(self):
        started = time.monotonic()

        def check():
            if time.monotonic() - started > 0.2:
                raise BuildCancelled("cancelled")

        with process_control(check), self.assertRaises(BuildCancelled):
            run([sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
