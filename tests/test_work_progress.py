"""Measured progress, estimates, stream draining, and durable telemetry isolation."""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from video_to_website import media, steps, whisper
from video_to_website.catalog import Catalog
from video_to_website.llm import AnthropicBackend
from video_to_website.util import BuildCancelled, run
from video_to_website.work_progress import (
    WorkProgress,
    progress_snapshot,
    report_work,
    track_work,
    work_group,
)


class MeasurementTests(unittest.TestCase):
    def test_estimate_needs_real_advances_and_resets_between_operations(self):
        now = [0.0]
        recorded = []
        progress = WorkProgress(recorded.append, clock=lambda: now[0])
        progress.report("speech", "Transcribing", completed=0, total=100)
        now[0] = 2
        progress.report("speech", "Transcribing", completed=10, total=100)
        self.assertIsNone(recorded[-1]["eta_seconds"])
        now[0] = 10
        progress.report("speech", "Transcribing", completed=20, total=100)
        self.assertEqual(recorded[-1]["eta_seconds"], 40)
        now[0] = 11
        progress.report("speech", "Transcribing", completed=5, total=100)
        self.assertEqual(
            recorded[-1]["completed"],
            20,
            "out-of-order tool reports cannot move backwards",
        )
        now[0] = 20
        progress.report("scenes", "Analyzing", completed=0, total=600)
        self.assertIsNone(recorded[-1]["eta_seconds"])
        self.assertEqual(recorded[-1]["stage_started"], 0)
        self.assertEqual(recorded[-1]["phase_started"], 20)

    def test_unknown_and_nonfinite_totals_never_invent_a_percentage(self):
        progress = WorkProgress(lambda value: None)
        for completed, total in [
            (1, None),
            (1, 0),
            (1, float("inf")),
            (float("nan"), 100),
        ]:
            progress.report("model", "Waiting", completed=completed, total=total)
            self.assertIsNone(progress.latest["fraction"])
            self.assertIsNone(progress.latest["eta_seconds"])

    def test_writes_are_throttled_but_completion_is_immediate(self):
        now = [0.0]
        recorded = []
        progress = WorkProgress(recorded.append, clock=lambda: now[0])
        progress.report("speech", "Transcribing", completed=0, total=100)
        for n in range(1, 51):
            now[0] = n / 100
            progress.report("speech", "Transcribing", completed=n, total=100)
        self.assertEqual(len(recorded), 1)
        progress.report("speech", "Transcribing", completed=100, total=100)
        self.assertEqual(recorded[-1]["fraction"], 1)
        self.assertEqual(len(recorded), 2)

    def test_stale_progress_loses_its_eta(self):
        result = progress_snapshot(
            {"updated": 10, "eta_seconds": 120, "fraction": 0.4}, now=200
        )
        self.assertTrue(result["stale"])
        self.assertIsNone(result["eta_seconds"])
        self.assertEqual(result["fraction"], 0.4)

    def test_suboperation_reports_do_not_reset_media_batch_progress(self):
        with track_work(lambda value: None) as reporter:
            with work_group(
                "assets",
                "Creating media",
                completed=3,
                total=10,
                unit="steps",
                detail="Step 4 of 10",
            ):
                report_work("clip", "Encoding clip", completed=50, total=100)
                self.assertEqual(reporter.latest["fraction"], 0.3)
                self.assertIn("Encoding clip · 50%", reporter.latest["detail"])
                self.assertIn("Step 4 of 10", reporter.latest["detail"])
            report_work("assets", "Creating media", completed=4, total=10, unit="steps")
            self.assertEqual(reporter.latest["fraction"], 0.4)

    def test_progress_storage_failure_does_not_kill_processing(self):
        def broken(_):
            raise sqlite3.OperationalError("database busy")

        with patch("video_to_website.work_progress.warn") as warning:
            progress = WorkProgress(broken)
            progress.report("speech", "Transcribing", completed=0, total=100)
            progress.report("speech", "Transcribing", completed=100, total=100)
        self.assertEqual(warning.call_count, 1)

    def test_whisper_parser_only_accepts_its_actual_progress_messages(self):
        with track_work(lambda value: None) as reporter:
            whisper._report_progress("whisper_print_progress_callback: progress =  45%")
            self.assertEqual(reporter.latest["fraction"], 0.45)
            whisper._report_progress("The speaker says progress = 99%")
            whisper._report_progress("whisper_print_progress_callback: progress = 999%")
            self.assertEqual(reporter.latest["fraction"], 0.45)

    def test_ffmpeg_parser_waits_for_end_before_reporting_complete(self):
        with track_work(lambda value: None) as reporter:
            callback = media.FFmpegProgress("audio", "Extracting", 10)
            for line in [
                "out_time_us=N/A",
                "out_time_us=-9223372036854775808",
                "progress=continue",
            ]:
                callback(line)
            self.assertEqual(reporter.latest["fraction"], 0)
            callback("out_time_us=5000000")
            callback("progress=continue")
            self.assertEqual(reporter.latest["fraction"], 0.5)
            callback("out_time_us=10000000")
            callback("progress=continue")
            self.assertEqual(reporter.latest["fraction"], 0.99)
            callback("progress=end")
            self.assertEqual(reporter.latest["fraction"], 1)

    def test_streamed_scene_scores_keep_the_configured_floor(self):
        def fake_run(command, **kwargs):
            self.assertFalse(
                kwargs["capture_stdout"],
                "per-frame metadata must not accumulate in memory",
            )
            for line in (
                "frame:0 pts:0 pts_time:0",
                "lavfi.scene_score=0.005",
                "frame:1 pts:1 pts_time:0.1",
                "lavfi.scene_score=0.025",
            ):
                kwargs["on_stdout"](line)

        with (
            patch.object(media, "ffmpeg_bin", return_value="ffmpeg"),
            patch.object(media, "run", side_effect=fake_run),
        ):
            self.assertEqual(
                media.detect_scenes(Path("video.mp4"), floor=0.01, duration=1),
                [[0.1, 0.025]],
            )

    def test_model_stream_reports_activity_without_guessing_output_length(self):
        class Stream:
            text_stream = ["hello", " world"]

            def get_final_message(self):
                return "final response"

        with track_work(lambda value: None) as reporter:
            with work_group(
                "sections",
                "Writing instructions",
                completed=0,
                total=1,
                unit="sections",
            ):
                self.assertEqual(
                    AnthropicBackend._collect_stream(Stream()), "final response"
                )
                self.assertIn("11 characters received", reporter.latest["detail"])
                self.assertEqual(reporter.latest["fraction"], 0)
                self.assertIsNone(reporter.latest["eta_seconds"])

    def test_transcript_sections_count_completed_responses(self):
        class Backend:
            def complete(self, system, user):
                return json.dumps(
                    {"title": "Guide", "steps": [], "summary": "", "prerequisites": []}
                )

        with track_work(lambda value: None) as reporter:
            steps.extract_lesson(
                Backend(),
                title="Guide",
                duration=180,
                segments=[
                    {"start": t, "end": t + 5, "text": "instruction"}
                    for t in range(0, 180, 5)
                ],
                scene_times=[],
                chunk_minutes=2,
            )
            self.assertEqual(reporter.latest["completed"], 2)
            self.assertEqual(reporter.latest["total"], 2)


class LiveSubprocessTests(unittest.TestCase):
    def test_progress_arrives_before_exit_and_both_pipes_are_drained(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = Path(tmp) / "continue"
            script = """import os, pathlib, sys, time
os.write(1, b'x' * 262144)
os.write(2, b'whisper_print_progress_call')
time.sleep(.02)
os.write(2, b'back: progress = 10%\\r')
deadline = time.monotonic() + 3
while not pathlib.Path(sys.argv[1]).exists():
    if time.monotonic() > deadline: raise RuntimeError('callback was not live')
    time.sleep(.01)
os.write(2, 'unicode: café\\n'.encode())
print('done')
"""
            lines = []

            def callback(line):
                lines.append(line)
                whisper._report_progress(line)
                if "10%" in line:
                    gate.touch()

            journal = io.StringIO()
            with (
                track_work(lambda value: None) as reporter,
                contextlib.redirect_stderr(journal),
            ):
                result = run(
                    [sys.executable, "-c", script, str(gate)],
                    on_stderr=callback,
                    capture_stderr=False,
                    tee_stderr=True,
                )
            self.assertEqual(len(result.stdout), 262144 + len("done\n"))
            self.assertIn("unicode: café", lines)
            self.assertIn(
                "progress = 10%", journal.getvalue(), "journal output is retained"
            )
            self.assertEqual(
                reporter.latest["fraction"],
                0.1,
                "callbacks inherit the workflow progress context",
            )

    def test_cancellation_during_streaming_still_reaps_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "pid"
            script = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); print('ready',file=sys.stderr,flush=True); time.sleep(30)"

            def cancel(line):
                raise BuildCancelled("cancel")

            started = time.monotonic()
            with self.assertRaises(BuildCancelled):
                run([sys.executable, "-c", script, str(pid_file)], on_stderr=cancel)
            self.assertLess(time.monotonic() - started, 5)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)


class DurableProgressTests(unittest.TestCase):
    def test_busy_progress_database_does_not_block_the_media_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "library"
            library.mkdir()
            (library / "source.mp4").write_bytes(b"source")
            catalog = Catalog(root / "state")
            catalog.reconcile(library, {})
            job = catalog.rows("SELECT * FROM builds")[0]
            catalog.update_build(job["id"], "running", stage="transcribe")
            with (
                catalog.connect() as locked,
                patch("video_to_website.work_progress.warn"),
            ):
                locked.execute("BEGIN IMMEDIATE")
                started = time.monotonic()
                with track_work(
                    lambda value: catalog.update_progress(
                        job["id"], "transcribe", value
                    )
                ):
                    report_work("speech", "Transcribing", completed=25, total=100)
                self.assertLess(time.monotonic() - started, 2)

    def test_measurements_survive_reopen_and_do_not_change_stage_elapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "library"
            library.mkdir()
            (library / "source.mp4").write_bytes(b"source")
            catalog = Catalog(root / "state")
            catalog.reconcile(library, {})
            job = catalog.rows("SELECT * FROM builds")[0]
            catalog.update_build(job["id"], "running", stage="transcribe")
            now = [time.time() - 10]
            progress = WorkProgress(
                lambda value: catalog.update_progress(job["id"], "transcribe", value),
                clock=lambda: now[0],
            )
            progress.report("speech", "Transcribing", completed=0, total=100)
            now[0] += 5
            progress.report("speech", "Transcribing", completed=10, total=100)
            now[0] += 5
            progress.report("speech", "Transcribing", completed=20, total=100)
            status = Catalog(root / "state").status()["videos"][0]
            self.assertEqual(status["progress"]["fraction"], 0.2)
            self.assertEqual(status["progress"]["eta_seconds"], 40)
            self.assertGreaterEqual(status["elapsed"], 10)
            catalog.update_build(job["id"], "running", stage="scenes")
            self.assertIsNone(
                catalog.status()["videos"][0]["progress"],
                "the next stage must not inherit the old percentage",
            )
            catalog.cancel(job["lesson_id"])
            catalog.update_progress(job["id"], "transcribe", progress.latest)
            self.assertIsNone(catalog.status()["videos"][0]["progress"])

    def test_schema_two_migrates_progress_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp))
            with catalog.connect() as db:
                db.execute("DROP TABLE build_progress")
                for table in ("compute_attempts", "compute_tasks", "worker_pairing", "workers"):
                    db.execute("DROP TABLE " + table)
                db.execute("ALTER TABLE lessons DROP COLUMN chapter_override")
                db.execute("DROP TABLE reclaimed_sources")
                db.execute("PRAGMA user_version=2")
                db.execute("DROP TABLE reading_state")
                db.execute("DROP TABLE reader_preferences")
            reopened = Catalog(Path(tmp))
            self.assertEqual(reopened.rows("SELECT * FROM build_progress"), [])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is unavailable")
class RealMediaProgressTests(unittest.TestCase):
    def test_native_ffmpeg_progress_preserves_media_and_scene_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=128x72:rate=10",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=16000",
                    "-t",
                    "1.5",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    str(video),
                ]
            )
            with track_work(lambda value: None) as reporter:
                media.extract_audio(video, root / "audio.wav", duration=1.5)
                self.assertEqual(reporter.latest["fraction"], 1)
                self.assertGreater((root / "audio.wav").stat().st_size, 44)
                pairs = media.detect_scenes(video, floor=0, duration=1.5)
                self.assertGreater(
                    len(pairs), 0, "progress must not consume ffmpeg's scene metadata"
                )
                self.assertEqual(reporter.latest["fraction"], 1)
                media.extract_clip(video, 0, root / "clip.mp4", duration=1.5, width=128)
                self.assertEqual(reporter.latest["fraction"], 1)
                self.assertGreater(media.duration_of(root / "clip.mp4"), 1)
