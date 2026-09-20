"""A single lesson supplies multiple workers without unbounded local work."""

from __future__ import annotations

import contextvars
import contextlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_workers as worker_tests
from video_to_website import media, whisper, steps
from video_to_website.catalog import Catalog
from video_to_website.compute import compute_with
from video_to_website.distributed import CPU_SLOT, ComputeExecutor
from video_to_website.durable import WorkerStopping
from video_to_website.durable import DurableWorker
from video_to_website.pipeline import BuildOptions, process_video
from video_to_website.media_batch import MEDIA_LOOKAHEAD, MediaJob, run_media_jobs
from video_to_website.util import BuildCancelled, digest_file, process_control, run
from video_to_website.work_progress import report_work, track_work
from video_to_website.worker_store import WorkerConflict
from video_to_website.worker import Client, Helper


class MediaBatchTests(worker_tests.WorkerFixture):
    def runner(self, jobs, *, cancel=None, sink=None, executor=None):
        executor = executor or ComputeExecutor(self.catalog, self.build, "frames")
        def check():
            if cancel and cancel.is_set():
                raise BuildCancelled("Cancelled test batch")
        with process_control(check), compute_with(executor), track_work(sink or (lambda payload: None)):
            return run_media_jobs(jobs)

    def until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.01)
        self.fail("Timed out waiting for batch state")

    def finish_clip(self, worker, task):
        artifact = self.store.root / task["id"] / task["attempt"] / "clip"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(f"clip {task['params']['start']}".encode())
        self.store.record_artifact(worker["id"], task["attempt"], artifact, digest_file(artifact), artifact.stat().st_size)
        self.store.complete(worker["id"], task["attempt"], None, {"processing_seconds": .1})

    def test_faster_worker_keeps_claiming_clips_from_one_lesson_while_slow_worker_is_busy(self):
        slow, fast = self.pair(["clip"]), self.pair(["clip"])
        executor = ComputeExecutor(self.catalog, self.build, "frames")
        cancel = threading.Event()
        def local():
            self.fail("The test holds the server CPU slot; a helper should do this")
        jobs = [MediaJob((n, "clip"), f"Step {n + 1} · Clip", lambda n=n: executor.run("clip", {
            "video": self.video, "out_path": self.root / "clips" / f"{n}.mp4", "start": n,
            "duration": 1, "width": 256, "fps": 30, "crf": 24, "keep": None, "encoder": "auto"}, local)) for n in range(20)]
        pool = ThreadPoolExecutor(1)
        CPU_SLOT.acquire()
        try:
            future = pool.submit(self.runner, jobs, cancel=cancel, executor=executor)
            self.until(lambda: len(self.catalog.rows("SELECT id FROM compute_tasks")) == MEDIA_LOOKAHEAD)
            slow_task = self.store.claim(slow["id"])
            self.assertIsNotNone(slow_task)
            for _ in range(len(jobs) - 1):
                task = self.until(lambda: self.store.claim(fast["id"]))
                self.finish_clip(fast, task)
                count = self.catalog.rows("SELECT COUNT(*) AS n FROM compute_tasks WHERE state IN ('pending','running','fallback')")[0]["n"]
                self.assertLessEqual(count, MEDIA_LOOKAHEAD)
            self.assertEqual(self.store.task(slow_task["id"])["state"], "running")
            self.assertFalse(future.done(), "The slow clip is still required for publication")
            self.finish_clip(slow, slow_task)
            results = future.result(timeout=5)
            self.assertEqual(len(results), 20)
            for n in range(20):
                self.assertEqual(results[n, "clip"].read_bytes(), f"clip {n}".encode())
            fast_record = next(w for w in self.store.overview()["workers"] if w["id"] == fast["id"])
            self.assertEqual(fast_record["totals"]["accepted_tasks"], 19)
            self.assertTrue(all(item["description"].startswith("Step ") for item in fast_record["recent"]))
        finally:
            cancel.set()
            CPU_SLOT.release()
            pool.shutdown(wait=True, cancel_futures=True)

    def test_local_cpu_is_bounded_and_progress_is_aggregated_on_parent_thread(self):
        self.pair(["frame_hash"])
        executor = ComputeExecutor(self.catalog, self.build, "frames")
        guard = threading.Lock()
        counts = {"active": 0, "peak": 0}
        reports, threads = [], []
        unrelated = contextvars.ContextVar("parent_workflow_context", default="absent")
        token = unrelated.set("must not reach child threads")
        def native(n):
            self.assertEqual(unrelated.get(), "absent")
            with guard:
                counts["active"] += 1
                counts["peak"] = max(counts["peak"], counts["active"])
            try:
                report_work("native", "Computing", completed=0, total=1)
                time.sleep(.02)
                return n
            finally:
                with guard: counts["active"] -= 1
        def progress(payload):
            reports.append(payload); threads.append(threading.get_ident())
        jobs = [MediaJob((n, "frames"), f"Step {n}", lambda n=n: executor.run("frame_hash", {
            "video": self.video, "timestamp": n}, lambda: native(n))) for n in range(16)]
        try:
            with patch("video_to_website.distributed.OFFER_SECONDS", 0):
                results = self.runner(jobs, executor=executor, sink=progress)
        finally:
            unrelated.reset(token)
        self.assertEqual(counts["peak"], 1)
        self.assertEqual(set(threads), {threading.get_ident()})
        self.assertEqual([p["completed"] for p in reports], sorted(p["completed"] for p in reports))
        self.assertEqual(reports[-1]["completed"], 16)
        self.assertEqual(len(results), 16)

    def test_failure_fences_siblings_and_preserves_accepted_results_for_replay(self):
        helper = self.pair(["frame_hash"])
        executor = ComputeExecutor(self.catalog, self.build, "frames")
        cancel, fail = threading.Event(), threading.Event()
        def unavailable(): self.fail("Should not run locally")
        def native(n): return executor.run("frame_hash", {"video": self.video, "timestamp": n}, unavailable)
        def broken():
            if not fail.wait(5): raise AssertionError("Failure gate was not released")
            raise ConnectionError("original media failure")
        jobs = [MediaJob((0, "frames"), "First image", lambda: native(0)),
                MediaJob((1, "frames"), "Second image", lambda: native(1)),
                MediaJob((2, "clip"), "Bad clip", broken)]
        pool = ThreadPoolExecutor(1)
        CPU_SLOT.acquire()
        try:
            future = pool.submit(self.runner, jobs, executor=executor, cancel=cancel)
            task = self.until(lambda: self.store.claim(helper["id"]))
            self.store.complete(helper["id"], task["attempt"], 42)
            slow = self.until(lambda: self.store.claim(helper["id"]))
            fail.set()
            with self.assertRaisesRegex(ConnectionError, "original media failure"):
                future.result(timeout=5)
            self.assertEqual(self.store.task(task["id"])["state"], "accepted")
            self.assertEqual(self.store.task(slow["id"])["state"], "fallback")
            with self.assertRaises(WorkerConflict):
                self.store.complete(helper["id"], slow["attempt"], 43)
            # The accepted native result remains reusable within this workflow.
            source_time = task["params"]["timestamp"]
            self.assertEqual(executor.run("frame_hash", {"video": self.video, "timestamp": source_time}, unavailable), 42)
        finally:
            fail.set(); cancel.set(); CPU_SLOT.release()
            pool.shutdown(wait=True, cancel_futures=True)

    def test_cancellation_stops_a_running_local_subprocess(self):
        cancel = threading.Event()
        entered = self.root / "entered"
        executor = ComputeExecutor(self.catalog, self.build, "frames")
        def native():
            run([sys.executable, "-c", "from pathlib import Path; import sys,time; Path(sys.argv[1]).touch(); time.sleep(30)", str(entered)])
            return 1
        jobs = [MediaJob((0, "frames"), "Local image", lambda: executor.run("frame_hash", {
            "video": self.video, "timestamp": 0}, native))]
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.runner, jobs, executor=executor, cancel=cancel)
            self.until(entered.exists)
            started = time.monotonic(); cancel.set()
            with self.assertRaises(BuildCancelled): future.result(timeout=5)
            self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(self.catalog.rows("SELECT id FROM compute_attempts WHERE state='running'"))

    def test_shutdown_preserves_live_remote_assignments_and_restart_reuses_results(self):
        helper = self.pair(["frame_hash"])
        executor = ComputeExecutor(self.catalog, self.build, "frames")
        stopping = threading.Event()
        def local(): self.fail("The helper should execute this task")
        jobs = [MediaJob((0, "frames"), "An image", lambda: executor.run("frame_hash", {
            "video": self.video, "timestamp": 0}, local))]
        def start():
            def check():
                if stopping.is_set(): raise WorkerStopping()
            with process_control(check), compute_with(executor):
                return run_media_jobs(jobs)
        pool = ThreadPoolExecutor(1); CPU_SLOT.acquire()
        try:
            future = pool.submit(start)
            task = self.until(lambda: self.store.claim(helper["id"]))
            stopping.set()
            with self.assertRaises(WorkerStopping): future.result(timeout=5)
            self.assertEqual(self.store.task(task["id"])["state"], "running")
            self.store.complete(helper["id"], task["attempt"], 42)
            stopping.clear()
            self.assertEqual(self.runner(jobs, executor=executor)[0, "frames"], 42)
        finally:
            stopping.set(); CPU_SLOT.release(); pool.shutdown(wait=True, cancel_futures=True)

    def test_media_window_expands_when_a_helper_joins(self):
        executor = ComputeExecutor(self.catalog, self.build, "frames")
        release = threading.Event()
        more = threading.Event()
        cancel = threading.Event()
        def first():
            if not release.wait(5): raise AssertionError("First media job timed out")
        jobs = [MediaJob((0, "frames"), "First job", first),
                MediaJob((1, "clip"), "Next job", more.set)]
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.runner, jobs, executor=executor, cancel=cancel)
            try:
                self.assertFalse(more.wait(.2), "Local-only processing admits one job")
                self.pair(["clip"])
                self.assertTrue(more.wait(3), "Joining helpers open the media window")
            finally:
                release.set()
            future.result(timeout=5)


class StandaloneBatchTests(unittest.TestCase):
    def test_killed_media_stage_reuses_completed_clip_on_real_dbos_recovery(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "library" / "course" / "video.mp4"
            source.parent.mkdir(parents=True); source.write_bytes(b"fixture source")
            command = [sys.executable, str(Path(__file__).with_name("media_batch_fixture.py")), str(root)]
            with (root / "worker.log").open("w+") as log:
                process = subprocess.Popen(command, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 15
                    while not (root / "entered-second-clip").exists() and time.monotonic() < deadline and process.poll() is None:
                        time.sleep(.05)
                    log.flush(); log.seek(0)
                    self.assertTrue((root / "entered-second-clip").exists(), log.read())
                finally:
                    process.kill(); process.wait(timeout=5)
                self.assertFalse(list((root / "work").rglob("assets.json")), "The media stage must still be incomplete")
                (root / "release-second-clip").touch()
                recovered = subprocess.run(command, stdout=log, stderr=log, timeout=20)
                log.seek(0); self.assertEqual(recovered.returncode, 0, log.read())
            calls = (root / "clip-calls").read_text().splitlines()
            self.assertEqual(calls.count("step-001.mp4"), 1, "Accepted clips must not be regenerated on replay")
            self.assertEqual(calls.count("step-002.mp4"), 2, "The interrupted clip restarts")
            self.assertEqual(Catalog(root / "state").rows("SELECT state FROM builds"), [{"state": "ready"}])

    def test_standalone_jobs_run_sequentially(self):
        calls = []
        jobs = [MediaJob((n, "frames"), f"Job {n}", lambda n=n: calls.append(n) or n) for n in range(5)]
        with compute_with(None):
            results = run_media_jobs(jobs)
        self.assertEqual(calls, list(range(5)))
        self.assertEqual([results[n, "frames"] for n in range(5)], calls)


class PipelineBatchTests(worker_tests.WorkerFixture):
    def test_parallel_completion_preserves_step_order_screenshot_dedup_and_manifest(self):
        class Executor:
            def media_window(self, maximum): return maximum
            def release_media_batch(self): raise AssertionError("Successful batch should not be released")

        completed = []
        lock = threading.Lock()
        def frame(video, timestamp, output, **kwargs):
            time.sleep(.07 if timestamp < 1 else .001)
            output.parent.mkdir(parents=True, exist_ok=True); output.write_bytes(b"frame")
            return output
        def clip(video, start, output, **kwargs):
            index = int(re.search(r"step-(\d+)", output.name)[1])
            time.sleep(.2 if index == 1 else .001)
            output.parent.mkdir(parents=True, exist_ok=True); output.write_bytes(b"clip")
            with lock: completed.append(index)
            return output
        lesson = {"title": "Tutorial", "summary": "Summary", "prerequisites": [], "steps": [
            {"start": n, "end": n + 1, "title": f"Step {n + 1}", "actions": ["Create a shape"], "motion": True}
            for n in range(4)]}
        options = BuildOptions(out=self.root / "site", llm="heuristic", clips="all", clip_max_still=0, shots_with_clip="all", frames_per_step=3)
        with contextlib.ExitStack() as stack:
            for target, name, setting in [
                (media, "probe", {"return_value": {"duration": 4, "width": 128, "height": 96, "has_audio": True}}),
                (media, "extract_audio", {}),
                (whisper, "transcribe", {"return_value": [{"start": 0, "end": 4, "text": "Create a shape"}]}),
                (media, "detect_scenes", {"return_value": []}),
                (steps, "heuristic_lesson", {"return_value": lesson}),
                (steps, "frames_for_step", {"side_effect": lambda step, *a, **kw: [step["start"] + t for t in (.1, .2, .3)]}),
                (media, "frame_hash", {"side_effect": lambda video, t: (2**256 - 1) if round(t * 10) % 10 == 3 else 0}),
                (media, "extract_frame", {"side_effect": frame}),
                (media, "extract_clip", {"side_effect": clip}),
                (media, "duration_of", {"return_value": .5}),
            ]:
                stack.enter_context(patch.object(target, name, **setting))
            stack.enter_context(compute_with(Executor()))
            result = process_video(self.video, work_dir=self.root / "work", course_dir=self.root / "output",
                                   slug="lesson", options=options, backend=None, model_path=self.root / "model.bin",
                                   run_stage=lambda name, fn: fn())
        self.assertNotEqual(completed[0], 1, "The test must exercise out-of-order completion")
        self.assertEqual([step["index"] for step in result["steps"]], [1, 2, 3, 4])
        for n, step in enumerate(result["steps"]):
            self.assertEqual([frame["time"] for frame in step["frames"]], [n + .1, n + .3])
            self.assertTrue(step["clip"]["src"].endswith(f"step-{n + 1:03d}.mp4"))
        manifest = json.loads((self.root / "work" / "assets.json").read_text())["data"]
        self.assertEqual([r["clip"]["src"] for r in manifest], [s["clip"]["src"] for s in result["steps"]])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is unavailable")
class NativeBatchTests(worker_tests.WorkerHTTPFixture):
    def test_two_helpers_process_multiple_clips_within_one_dbos_lesson(self):
        worker_tests.NativeWorkerTests.make_video(self)
        model = self.root / "model.bin"; model.write_bytes(b"fixture model")
        executable = self.root / "whisper-test"
        executable.write_text("#!" + sys.executable + "\n" + '''import json,sys
from pathlib import Path
prefix=sys.argv[sys.argv.index('--output-file')+1]
Path(prefix+'.json').write_text(json.dumps({'transcription':[{'offsets':{'from':0,'to':2000},'text':'Create a cube and rotate it.'}]}))
''')
        executable.chmod(0o755)
        fast = self.pair(["clip"])
        helpers, threads = [], []
        slow_started = threading.Event()
        class SlowHelper(Helper):
            def execute(self, task):
                if task["kind"] == "clip":
                    slow_started.set()
                    time.sleep(.8)
                return super().execute(task)
        class FastHelper(Helper):
            def execute(self, task):
                if task["kind"] == "clip" and not slow_started.wait(3):
                    raise AssertionError("Another clip should be available to the slow helper")
                return super().execute(task)
        options = BuildOptions(out=self.root / "site", work=self.root / "work", llm="heuristic", clips="all", frame_width=64, clip_width=64)
        lesson = {"title": "Tutorial", "summary": "Summary", "prerequisites": [], "steps": [
            {"start": n / 2, "end": (n + 1) / 2, "title": f"Part {n + 1}", "actions": ["Rotate the cube"], "motion": True}
            for n in range(4)]}
        with patch.dict("os.environ", {"V2W_WHISPER_BIN": str(executable)}), patch.object(whisper, "resolve_model", return_value=model), \
             patch.object(steps, "heuristic_lesson", return_value=lesson):
            CPU_SLOT.acquire()
            try:
                for number, (kind, client) in enumerate(((SlowHelper, self.client), (FastHelper, Client(self.client.base, fast["token"])))):
                    helper = kind(client, self.root / f"helper-{number}", min_free_gib=.00001)
                    if number == 0: helper.capabilities = ["clip"]
                    helper.heartbeat()
                    thread = threading.Thread(target=helper.run, daemon=True); thread.start()
                    helpers.append(helper); threads.append(thread)
                with DurableWorker(self.root / "library", options, self.root / "state") as coordinator:
                    deadline = time.monotonic() + 30
                    multiple = False
                    while time.monotonic() < deadline:
                        coordinator.tick()
                        status = self.catalog.status()["videos"][0]
                        if len(status.get("executions", [])) > 1: multiple = True
                        if status["state"] == "done": break
                        if status["state"] == "failed": self.fail(str(status))
                        time.sleep(.03)
                    else: self.fail(str(self.catalog.status()))
                self.assertTrue(multiple, "The queue must expose several tasks for one lesson")
                published = self.catalog.published_courses()[0]["lessons"][0]
                self.assertEqual([s["index"] for s in published["steps"]], [1, 2, 3, 4])
                self.assertEqual(sum(bool(s.get("clip")) for s in published["steps"]), 4)
                for identity in (self.credentials["id"], fast["id"]):
                    self.assertTrue(self.catalog.rows("""SELECT a.id FROM compute_attempts a JOIN compute_tasks t ON t.id=a.task_id
                        WHERE a.worker_id=? AND a.state='accepted' AND t.kind='clip'""", (identity,)))
                for step in published["steps"]:
                    path = (self.root / "site" / "course-placeholder" / step["clip"]["src"]).resolve()
                    self.assertEqual(media.probe(path)["video_codec"], "h264")
            finally:
                CPU_SLOT.release()
                for helper in helpers: helper.stopped.set()
                for thread in threads: thread.join(timeout=12)
