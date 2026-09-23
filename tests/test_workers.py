"""Ownership races, interrupted helpers, replay and real HTTP task transfers."""

from __future__ import annotations

import hashlib
import http.server
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from video_to_website import media
from video_to_website.catalog import Catalog
from video_to_website.compute import OPERATIONS
from video_to_website.distributed import ComputeExecutor
from video_to_website.durable import DurableWorker, serialize_options
from video_to_website.ingest import IngestHandler
from video_to_website.pipeline import BuildOptions
from video_to_website.util import digest_file
from video_to_website.worker import APIError, Client, Helper, InputCache
from video_to_website.worker_store import LEASE_SECONDS, PROTOCOL, WorkerConflict, WorkerStore, WorkerUnauthorized


class WorkerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.video = self.root / "library" / "Course" / "4.02 - Lesson.mp4"
        self.video.parent.mkdir(parents=True)
        self.video.write_bytes(b"source bytes")
        self.catalog = Catalog(self.root / "state")
        self.catalog.reconcile(self.root / "library", serialize_options(BuildOptions(out=self.root / "site", llm="heuristic")))
        self.lesson = self.catalog.rows("SELECT * FROM lessons")[0]
        self.build = self.lesson["desired_build"]
        self.catalog.update_build(self.build, "running", stage="frames")
        self.store = WorkerStore(self.catalog)
        self.store.server_heartbeat()
        with self.catalog.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('min_free_bytes','0')")

    def pair(self, kinds=None):
        body = {"id": uuid.uuid4().hex, "token": uuid.uuid4().hex, "name": "Test helper", "code": self.store.pairing()["code"]}
        self.store.pair(body)
        self.store.heartbeat(body["id"], {"protocol": PROTOCOL, "capabilities": kinds or ["frame_hash"], "platform": "Test"})
        return body

    def task(self, kind="frame_hash", *, output=None):
        task_id = uuid.uuid4().hex
        spec = {"inputs": {"source": {"path": str(self.video), "bytes": self.video.stat().st_size,
            "digest": digest_file(self.video), "suffix": ".mp4"}}, "params": {"timestamp": 1.0}, "output": output}
        self.store.ensure_task(task_id, self.build, "frames", kind, spec)
        return task_id


class WorkerTests(WorkerFixture):
    def test_fresh_pairing_code_reuses_identity_history_and_display_name(self):
        worker = self.pair()
        self.task(); attempt = self.store.claim(worker["id"])
        self.store.complete(worker["id"], attempt["attempt"], 42, {"processing_seconds": 7})
        self.store.manage(worker["id"], {"action": "rename", "name": "Gaming PC"})
        self.store.manage(worker["id"], {"action": "pause"})
        fresh = {**worker, "name": "Hostname", "code": self.store.pairing()["code"]}
        with self.assertRaises(WorkerUnauthorized):
            self.store.pair({**fresh, "token": uuid.uuid4().hex})
        self.store.pair(fresh)
        self.assertEqual(len(self.catalog.rows("SELECT id FROM workers")), 2)
        record = next(w for w in self.store.overview()["workers"] if w["id"] == worker["id"])
        self.assertEqual(record["name"], "Gaming PC")
        self.assertTrue(record["paused"])
        self.assertEqual(record["totals"]["accepted_tasks"], 1)

    def test_archive_fences_work_and_restore_keeps_access_revoked(self):
        worker = self.pair()
        task_id = self.task(); attempt = self.store.claim(worker["id"])
        self.store.manage(worker["id"], {"action": "archive"})
        self.assertEqual(self.store.task(task_id)["state"], "fallback")
        with self.assertRaises(WorkerUnauthorized): self.store.authenticate(worker["token"])
        with self.assertRaises(WorkerConflict): self.store.complete(worker["id"], attempt["attempt"], 99)
        record = next(w for w in self.store.overview()["workers"] if w["id"] == worker["id"])
        self.assertEqual(record["status"], "archived")
        self.assertEqual(record["totals"]["interrupted_tasks"], 1)
        self.store.manage(worker["id"], {"action": "restore"})
        with self.assertRaises(WorkerUnauthorized): self.store.authenticate(worker["token"])
        with self.assertRaises(WorkerUnauthorized): self.store.pair(worker)
        self.store.pair({**worker, "code": self.store.pairing()["code"]})
        self.assertEqual(self.store.authenticate(worker["token"]), worker["id"])
        record = next(w for w in self.store.overview()["workers"] if w["id"] == worker["id"])
        self.assertFalse(record["archived"])
        self.assertFalse(record["paused"])

    def test_delete_protects_results_needed_by_unfinished_or_failed_lessons(self):
        worker = self.pair()
        task_id = self.task(); attempt = self.store.claim(worker["id"])
        with self.assertRaisesRegex(WorkerConflict, "unfinished lesson"):
            self.store.manage(worker["id"], {"action": "delete"})
        self.store.complete(worker["id"], attempt["attempt"], 42)
        self.store.manage(worker["id"], {"action": "archive"})
        for state in ("running", "failed"):
            self.catalog.update_build(self.build, state)
            with self.assertRaisesRegex(WorkerConflict, "unfinished lesson"):
                self.store.manage(worker["id"], {"action": "delete"})
            self.assertEqual(self.store.task(task_id)["accepted_attempt"], attempt["attempt"])
        # An explicit retry creates a different build, releasing the old history.
        self.catalog.retry(self.lesson["id"])
        self.store.manage(worker["id"], {"action": "delete"})
        self.assertFalse(self.catalog.rows("SELECT id FROM workers WHERE id=?", (worker["id"],)))

    def test_delete_removes_worker_contributions_and_transfer_copy_but_keeps_lessons(self):
        worker = self.pair(["frame"])
        task_id = self.task("frame", output=str(self.root / "frame.jpg")); attempt = self.store.claim(worker["id"])
        artifact = self.store.root / task_id / attempt["attempt"] / "output"
        artifact.parent.mkdir(parents=True); artifact.write_bytes(b"image")
        self.store.record_artifact(worker["id"], attempt["attempt"], artifact, digest_file(artifact), 5)
        self.store.complete(worker["id"], attempt["attempt"], None)
        self.catalog.update_build(self.build, "ready")
        published = self.root / "site" / "_revisions" / "published.jpg"
        published.parent.mkdir(parents=True); published.write_bytes(b"published image")
        before = self.catalog.rows("SELECT * FROM lessons")
        self.store.manage(worker["id"], {"action": "delete"})
        self.assertFalse(self.catalog.rows("SELECT * FROM workers WHERE id=?", (worker["id"],)))
        self.assertFalse(self.catalog.rows("SELECT * FROM compute_attempts WHERE worker_id=?", (worker["id"],)))
        self.assertFalse(self.catalog.rows("SELECT * FROM worker_pairing WHERE worker_id=?", (worker["id"],)))
        self.assertFalse(artifact.exists())
        self.assertEqual(self.catalog.rows("SELECT * FROM lessons"), before)
        self.assertEqual(published.read_bytes(), b"published image")
        self.assertEqual(self.video.read_bytes(), b"source bytes")
        self.assertFalse(self.catalog.rows("PRAGMA foreign_key_check"))
        with self.assertRaises(WorkerUnauthorized): self.store.authenticate(worker["token"])
        with self.assertRaises(WorkerUnauthorized): self.store.pair(worker)

    def test_deleting_lost_worker_keeps_task_available_for_local_fallback(self):
        worker = self.pair()
        task_id = self.task(); self.store.claim(worker["id"])
        self.store.manage(worker["id"], {"action": "archive"})
        self.store.manage(worker["id"], {"action": "delete"})
        local = self.store.start_local(task_id)
        self.store.complete("server", local, 42)
        self.assertEqual(json.loads(self.store.task(task_id)["result"]), 42)
        for action in ("archive", "restore", "delete"):
            with self.assertRaises(ValueError): self.store.manage("server", {"action": action})

    def test_schema_four_preserves_existing_workers_during_archive_migration(self):
        worker = self.pair()
        with self.catalog.connect() as db:
            db.execute("ALTER TABLE workers DROP COLUMN archived")
            db.execute("ALTER TABLE lessons DROP COLUMN chapter_override")
            db.execute("DROP TABLE reclaimed_sources")
            db.execute("PRAGMA user_version=4")
            db.execute("DROP TABLE reading_state")
            db.execute("DROP TABLE reader_preferences"); db.execute("DROP TABLE reader_views")
        reopened = WorkerStore(Catalog(self.root / "state"))
        self.assertEqual(reopened.authenticate(worker["token"]), worker["id"])
        record = next(w for w in reopened.overview()["workers"] if w["id"] == worker["id"])
        self.assertFalse(record["archived"])

    def test_default_helper_support_does_not_requeue_existing_lessons(self):
        options = BuildOptions(out=self.root / "site", llm="heuristic")
        legacy = serialize_options(options)
        legacy.pop("clip_encoder", None)
        self.catalog.reconcile(self.root / "library", legacy)
        original = self.catalog.rows("SELECT desired_build FROM lessons")[0]["desired_build"]
        self.catalog.reconcile(self.root / "library", serialize_options(options))
        self.assertEqual(self.catalog.rows("SELECT desired_build FROM lessons")[0]["desired_build"], original)

    def test_pairing_replay_expiration_and_revocation(self):
        body = self.pair()
        self.assertEqual(self.store.pair(body)["id"], body["id"])
        self.assertEqual(self.store.authenticate(body["token"]), body["id"])
        with self.assertRaises(WorkerUnauthorized):
            self.store.pair({**body, "id": uuid.uuid4().hex})
        self.assertNotIn(body["token"], json.dumps(self.store.overview()))
        self.store.manage(body["id"], {"action": "revoke"})
        with self.assertRaises(WorkerUnauthorized):
            self.store.authenticate(body["token"])
        code = self.store.pairing()["code"]
        with patch.object(self.store, "clock", return_value=time.time() + 601), self.assertRaises(WorkerUnauthorized):
            self.store.pair({**body, "id": uuid.uuid4().hex, "code": code})

    def test_two_helpers_cannot_claim_the_same_task(self):
        workers = [self.pair(), self.pair()]
        self.task()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda worker: self.store.claim(worker["id"]), workers))
        self.assertEqual(sum(result is not None for result in results), 1)
        claimed = next(result for result in results if result)
        self.assertNotIn("path", claimed["inputs"]["source"])
        self.assertNotIn(str(self.root), json.dumps(claimed))

    def test_only_one_task_per_helper_and_pause_drains(self):
        worker = self.pair()
        self.task(); self.task()
        task = self.store.claim(worker["id"])
        self.assertIsNone(self.store.claim(worker["id"]))
        self.store.manage(worker["id"], {"action": "pause"})
        response = self.store.heartbeat(worker["id"], {"protocol": PROTOCOL, "capabilities": ["frame_hash"], "attempt": task["attempt"]})
        self.assertTrue(response["continue"])
        self.store.complete(worker["id"], task["attempt"], 12)
        self.assertIsNone(self.store.claim(worker["id"]))
        self.store.manage(worker["id"], {"action": "resume"})
        self.assertIsNotNone(self.store.claim(worker["id"]))

    def test_lease_expiration_fences_late_progress_completion_and_heartbeat(self):
        worker = self.pair()
        task_id = self.task()
        task = self.store.claim(worker["id"])
        with patch.object(self.store, "clock", return_value=time.time() + LEASE_SECONDS + 1):
            with self.assertRaises(WorkerConflict):
                self.store.complete(worker["id"], task["attempt"], 99)
            with self.assertRaises(WorkerConflict):
                self.store.progress(worker["id"], task["attempt"], {})
            response = self.store.heartbeat(worker["id"], {"protocol": PROTOCOL, "capabilities": ["frame_hash"], "attempt": task["attempt"]})
            self.assertFalse(response["continue"])
            self.store.expire()
        local = self.store.start_local(task_id)
        self.store.complete("server", local, 42)
        with self.assertRaises(WorkerConflict):
            self.store.complete(worker["id"], task["attempt"], 99)
        self.assertEqual(json.loads(self.store.task(task_id)["result"]), 42)

    def test_cancel_and_stop_invalidate_active_assignments(self):
        worker = self.pair()
        task_id = self.task()
        task = self.store.claim(worker["id"])
        self.store.manage(worker["id"], {"action": "stop"})
        self.assertEqual(self.store.task(task_id)["state"], "fallback")
        with self.assertRaises(WorkerConflict):
            self.store.complete(worker["id"], task["attempt"], 4)
        self.catalog.cancel(self.lesson["id"])
        self.store.expire()
        self.assertEqual(self.store.task(task_id)["state"], "cancelled")
        with self.assertRaises(WorkerConflict):
            self.store.start_local(task_id)

    def test_repeated_completion_counts_contribution_once(self):
        worker = self.pair()
        self.task()
        task = self.store.claim(worker["id"])
        for _ in range(3):
            self.store.complete(worker["id"], task["attempt"], 42, {"processing_seconds": 10})
        reopened = WorkerStore(Catalog(self.root / "state"))
        record = next(w for w in reopened.overview()["workers"] if w["id"] == worker["id"])
        self.assertEqual(record["totals"]["accepted_tasks"], 1)
        self.assertEqual(record["totals"]["processing_seconds"], 10)
        with self.assertRaises(WorkerConflict):
            self.store.complete(worker["id"], task["attempt"], 43)

    def test_no_helper_runs_locally_and_replay_reuses_result(self):
        function = Mock(return_value=42)
        arguments = {"video": self.video, "timestamp": 1.0}
        first = ComputeExecutor(self.catalog, self.build, "frames")
        self.assertEqual(first.run("frame_hash", arguments, function), 42)
        second = ComputeExecutor(Catalog(self.root / "state"), self.build, "frames")
        self.assertEqual(second.run("frame_hash", arguments, function), 42)
        function.assert_called_once()

    def test_disconnected_helper_automatically_falls_back_during_execution(self):
        worker = self.pair()
        function = Mock(return_value=42)
        executor = ComputeExecutor(self.catalog, self.build, "frames")
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(executor.run, "frame_hash", {"video": self.video, "timestamp": 1.0}, function)
            deadline = time.monotonic() + 2
            task = None
            while task is None and time.monotonic() < deadline:
                task = self.store.claim(worker["id"])
                time.sleep(.02)
            self.assertIsNotNone(task)
            with self.catalog.connect() as db:
                db.execute("UPDATE compute_attempts SET lease_until=0 WHERE id=?", (task["attempt"],))
            self.assertEqual(future.result(timeout=3), 42)
        function.assert_called_once()
        self.assertEqual(self.catalog.status()["videos"][0]["execution"]["worker_id"], "server")

    def test_coordinator_restart_retains_live_remote_but_releases_local_attempt(self):
        worker = self.pair()
        remote_id = self.task()
        self.store.claim(worker["id"])
        local_id = self.task()
        self.store.start_local(local_id)
        self.store.expire(restart=True)
        self.assertEqual(self.store.task(remote_id)["state"], "running")
        self.assertEqual(self.store.task(local_id)["state"], "fallback")

    def test_startup_cleanup_cannot_invalidate_a_task_started_by_recovery(self):
        task_id = self.task()
        old_attempt = self.store.start_local(task_id)
        self.store.fail("server", old_attempt, "Previous process stopped.")
        resumed = []

        def recover():
            resumed.append(self.store.start_local(task_id))

        with patch("video_to_website.durable.DBOS") as dbos:
            dbos.launch.side_effect = recover
            with DurableWorker(self.root / "library", BuildOptions(out=self.root / "site", llm="heuristic"), self.root / "state"):
                self.assertIsNotNone(resumed[0])
                task = self.store.task(task_id)
                self.assertEqual(task["state"], "running")
                self.assertEqual(task["current_attempt"], resumed[0])
                self.store.complete("server", resumed[0], 42)

    def test_full_server_storage_does_not_repeat_expensive_processing(self):
        from video_to_website.util import CommandError

        worker = self.pair()
        function = Mock(side_effect=AssertionError("Storage failure should not rerun compute"))
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(ComputeExecutor(self.catalog, self.build, "frames").run,
                                 "frame_hash", {"video": self.video, "timestamp": 2.0}, function)
            task = None
            deadline = time.monotonic() + 2
            while task is None and time.monotonic() < deadline:
                task = self.store.claim(worker["id"])
                time.sleep(.02)
            self.assertIsNotNone(task)
            self.store.fail(worker["id"], task["attempt"], "Server disk is full.", category="server_storage")
            with self.assertRaisesRegex(CommandError, "disk is full"):
                future.result(timeout=3)
        function.assert_not_called()

    def test_protocol_and_result_validation(self):
        worker = self.pair()
        with self.assertRaises(WorkerConflict):
            self.store.heartbeat(worker["id"], {"protocol": 999})
        self.task()
        task = self.store.claim(worker["id"])
        for value in (-1, 2**256, "bad"):
            with self.assertRaises(ValueError):
                self.store.complete(worker["id"], task["attempt"], value)

    def test_helper_can_pick_up_work_waiting_for_busy_local_cpu(self):
        from video_to_website.distributed import CPU_SLOT

        function = Mock(side_effect=AssertionError("Should run remotely"))
        with ThreadPoolExecutor(1) as pool:
            CPU_SLOT.acquire()
            try:
                future = pool.submit(ComputeExecutor(self.catalog, self.build, "frames").run,
                                     "frame_hash", {"video": self.video, "timestamp": 1.0}, function)
                deadline = time.monotonic() + 2
                while not self.catalog.rows("SELECT id FROM compute_tasks") and time.monotonic() < deadline:
                    time.sleep(.02)
                worker = self.pair()
                task = self.store.claim(worker["id"])
                self.assertIsNotNone(task)
                self.store.complete(worker["id"], task["attempt"], 88)
                self.assertEqual(future.result(timeout=3), 88)
            finally:
                CPU_SLOT.release()
        function.assert_not_called()

    def test_input_preparation_can_continue_while_local_compute_is_busy(self):
        from video_to_website.distributed import CPU_SLOT

        with ThreadPoolExecutor(1) as pool:
            CPU_SLOT.acquire()
            try:
                future = pool.submit(ComputeExecutor(self.catalog, self.build, "probe").run,
                                     "probe", {"video": self.video}, lambda: {"duration": 2})
                self.assertEqual(future.result(timeout=1), {"duration": 2})
            finally:
                CPU_SLOT.release()


class WorkerHTTPFixture(WorkerFixture):
    def setUp(self):
        super().setUp()
        server = self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), IngestHandler)
        server.library, server.catalog, server.min_free_bytes = self.root / "library", self.catalog, 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        credentials = self.credentials = self.pair(["frame_hash", "frame"])
        self.client = Client(f"http://127.0.0.1:{server.server_port}", credentials["token"])


class WorkerHTTPTests(WorkerHTTPFixture):
    def test_worker_archive_restore_and_delete_through_management_api(self):
        url = self.client.base + "/api/workers/" + self.credentials["id"]
        for action in ("archive", "restore", "delete"):
            request = urllib.request.Request(url, data=json.dumps({"action": action}).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
        self.assertFalse(self.catalog.rows("SELECT id FROM workers WHERE id=?", (self.credentials["id"],)))
        with self.assertRaises(APIError) as error: self.client.post("claim")
        self.assertEqual(error.exception.status, 401)

    def test_helper_download_is_public_versioned_and_has_no_pairing_credentials(self):
        base = self.client.base + "/api/workers/"
        with urllib.request.urlopen(base + "download-info", timeout=3) as response:
            info = json.load(response)
        with urllib.request.urlopen(base + "download?sha256=" + info["sha256"], timeout=3) as response:
            payload = response.read()
            self.assertIn(info["filename"], response.headers["Content-Disposition"])
            self.assertEqual(response.headers["X-Content-SHA256"], info["sha256"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(hashlib.sha256(payload).hexdigest(), info["sha256"])
        self.assertEqual(len(payload), info["bytes"])
        self.assertEqual(self.catalog.rows("SELECT COUNT(*) AS n FROM worker_pairing")[0]["n"], 1,
                         "Downloading does not create a pairing code")
        request = urllib.request.Request(base + "download", method="HEAD")
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.read(), b"")
            self.assertEqual(int(response.headers["Content-Length"]), info["bytes"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "download?sha256=" + "0" * 64, timeout=3)
        self.assertEqual(error.exception.code, 409); error.exception.close()

    def test_disabled_workers_do_not_expose_helper_downloads(self):
        self.server.workers_enabled = False
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.client.base + "/api/workers/download", timeout=3)
        self.assertEqual(error.exception.code, 503); error.exception.close()


    def test_http_helper_and_cached_input_without_local_execution(self):
        with patch("video_to_website.worker.shutil.which", return_value="/fake/bin"):
            helper = Helper(self.client, self.root / "helper", min_free_gib=.00001)
        helper.heartbeat()
        operation = {**OPERATIONS["frame_hash"], "function": lambda **kw: 77}
        with patch.dict(OPERATIONS, frame_hash=operation), ThreadPoolExecutor(1) as pool:
            for timestamp in (1.0, 2.0):
                function = Mock(side_effect=AssertionError("Should have executed on the helper"))
                executor = ComputeExecutor(self.catalog, self.build, "frames")
                future = pool.submit(executor.run, "frame_hash", {"video": self.video, "timestamp": timestamp}, function)
                task = None
                deadline = time.monotonic() + 2
                while task is None and time.monotonic() < deadline:
                    task = self.client.post("claim")["task"]
                    time.sleep(.02)
                self.assertIsNotNone(task)
                helper.execute(task)
                self.assertEqual(future.result(timeout=3), 77)
                function.assert_not_called()
        accepted = self.catalog.rows("SELECT metrics FROM compute_attempts WHERE worker_id=? AND state='accepted' ORDER BY started", (self.credentials["id"],))
        self.assertEqual(json.loads(accepted[0]["metrics"])["transfer_bytes"], self.video.stat().st_size)
        self.assertEqual(json.loads(accepted[1]["metrics"])["transfer_bytes"], 0)

    def test_artifact_integrity_and_foreign_assignment(self):
        output = str(self.root / "result.jpg")
        self.task("frame", output=output)
        task = self.client.post("claim")["task"]
        data = b"\xff\xd8\xff" + b"screenshot"
        headers = {"Content-Length": str(len(data)), "X-Content-SHA256": "0" * 64}
        with self.assertRaises(APIError) as error:
            self.client.open("PUT", f"attempts/{task['attempt']}/artifact", data, headers)
        self.assertEqual(error.exception.status, 400)
        other = self.pair()
        foreign = Client(self.client.base, other["token"])
        with self.assertRaises(APIError) as error:
            foreign.open("GET", f"attempts/{task['attempt']}/inputs/source")
        self.assertEqual(error.exception.status, 409)
        headers["X-Content-SHA256"] = hashlib.sha256(data).hexdigest()
        with self.client.open("PUT", f"attempts/{task['attempt']}/artifact", data, headers) as response:
            self.assertEqual(response.status, 200)
        self.client.post(f"attempts/{task['attempt']}/complete", {"value": None})
        record = self.store.task(task["id"])
        ComputeExecutor(self.catalog, self.build, "frames").restore(record, Path(output), {"out_path": Path(output)})
        self.assertEqual(Path(output).read_bytes(), data)

    def test_unauthenticated_helper_cannot_read_inputs(self):
        with self.assertRaises(APIError) as error:
            Client(self.client.base).post("claim")
        self.assertEqual(error.exception.status, 401)

    def test_workers_work_when_browser_uploads_are_disabled(self):
        self.server.uploads_enabled = False
        self.assertIn("task", self.client.post("claim"))
        import urllib.error
        import urllib.request

        request = urllib.request.Request(self.client.base + "/api/library/Course/new.mp4", data=b"abc", method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is unavailable")
class NativeWorkerTests(WorkerHTTPFixture):
    def test_downloaded_helper_joins_and_processes_without_installed_python_packages(self):
        import signal
        import subprocess

        self.make_video()
        with urllib.request.urlopen(self.client.base + "/api/workers/download", timeout=3) as response:
            archive = self.root / "worker.pyz"; archive.write_bytes(response.read())
        task_id = self.task()
        code = self.store.pairing()["code"]
        with (self.root / "downloaded-helper.log").open("w+") as log:
            process = subprocess.Popen([sys.executable, "-I", "-S", str(archive), "--server", self.client.base,
                "--pairing-code", code, "--name", "Downloaded helper", "--state", str(self.root / "downloaded-state"),
                "--min-free-gib", "0.00001"], cwd=self.root, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    task = self.store.task(task_id)
                    if task["state"] == "accepted":
                        break
                    if process.poll() is not None:
                        log.seek(0); self.fail(log.read())
                    time.sleep(.05)
                else:
                    log.seek(0); self.fail(log.read())
                self.assertIsInstance(json.loads(task["result"]), int)
                helper = next(w for w in self.store.overview()["workers"] if w["name"] == "Downloaded helper")
                self.assertEqual(helper["totals"]["accepted_tasks"], 1)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT) if sys.platform != "win32" else process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()

    def make_video(self):
        from video_to_website.util import run

        run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(self.video)], timeout=10)

    def test_native_clip_transfers_and_validates_end_to_end(self):
        self.make_video()
        helper = Helper(self.client, self.root / "helper", min_free_gib=.00001)
        helper.heartbeat()
        output = self.root / "out.mp4"
        binding = OPERATIONS["clip"]["signature"].bind(self.video, 0, output, duration=1, width=64)
        binding.apply_defaults(); arguments = binding.arguments
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(ComputeExecutor(self.catalog, self.build, "frames").run, "clip", arguments,
                                 Mock(side_effect=AssertionError("Expected remote encoding")))
            task = None
            deadline = time.monotonic() + 2
            while task is None and time.monotonic() < deadline:
                task = self.client.post("claim")["task"]
                time.sleep(.02)
            self.assertIsNotNone(task)
            helper.execute(task)
            self.assertEqual(future.result(timeout=5), output)
        facts = media.probe(output)
        self.assertEqual(facts["video_codec"], "h264")
        self.assertEqual(facts["width"], 64)
        self.assertGreater(facts["duration"], 0)

    def test_dbos_pipeline_with_two_helpers_and_real_ffmpeg(self):
        self.make_video()
        self.video.with_name("4.03 - Next.mp4").write_bytes(self.video.read_bytes())
        model = self.root / "model.bin"; model.write_bytes(b"test model")
        native = self.root / "whisper-test"
        native.write_text("#!" + sys.executable + "\n" + '''import json,sys,time
from pathlib import Path
time.sleep(1)
args=sys.argv
prefix=args[args.index('--output-file')+1]
print('whisper_print_progress_callback: progress = 100%',file=sys.stderr,flush=True)
Path(prefix+'.json').write_text(json.dumps({'transcription':[{'offsets':{'from':0,'to':2000},'text':'Create a cube and rotate it.'}]}))
''')
        native.chmod(0o755)
        second = self.pair(["transcribe", "clip", "scenes", "frame_hash", "frame", "activity"])
        clients = [self.client, Client(self.client.base, second["token"])]
        options = BuildOptions(out=self.root / "site", work=self.root / "work", llm="heuristic", clips="all", frame_width=64, clip_width=64)
        helpers, threads = [], []
        with patch.dict("os.environ", {"V2W_WHISPER_BIN": str(native)}), patch("video_to_website.whisper.resolve_model", return_value=model):
            try:
                for number, client in enumerate(clients):
                    helper = Helper(client, self.root / ("helper-" + str(number)), min_free_gib=.00001)
                    helper.heartbeat()
                    thread = threading.Thread(target=helper.run, daemon=True); thread.start()
                    helpers.append(helper); threads.append(thread)
                with DurableWorker(self.root / "library", options, self.root / "state") as coordinator:
                    deadline = time.monotonic() + 35
                    while time.monotonic() < deadline:
                        coordinator.tick()
                        builds = self.catalog.rows("SELECT b.state,b.error FROM builds b JOIN lessons l ON l.desired_build=b.id")
                        if len(builds) == 2 and all(b["state"] == "ready" for b in builds):
                            break
                        if any(b["state"] == "failed" for b in builds):
                            self.fail(str(builds))
                        time.sleep(.05)
                    else:
                        self.fail(str(self.catalog.status()))
                records = self.store.overview()["workers"]
                for credentials in (self.credentials, second):
                    worker = next(w for w in records if w["id"] == credentials["id"])
                    self.assertGreater(worker["totals"]["accepted_tasks"], 0)
                self.assertEqual(len(self.catalog.published_courses()[0]["lessons"]), 2)
                self.assertTrue((self.root / "site" / "workers.html").is_file())
            finally:
                for helper in helpers: helper.stopped.set()
                for thread in threads: thread.join(timeout=12)


class CacheTests(unittest.TestCase):
    def test_eviction_preserves_active_inputs_and_free_space_buffer(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = InputCache(root, limit_bytes=10, min_free_bytes=0)
            (root / "old").write_bytes(b"12345")
            (root / "active").write_bytes(b"12345")
            cache.make_room(5, {"active"})
            self.assertFalse((root / "old").exists())
            self.assertTrue((root / "active").exists())
            with self.assertRaises(OSError):
                cache.make_room(6, {"active"})

    def test_cache_rejects_paths_and_oversized_inputs(self):
        with tempfile.TemporaryDirectory() as raw:
            cache = InputCache(Path(raw), limit_bytes=10)
            for item in ({"digest": "../escape", "bytes": 1, "suffix": ".mp4"},
                         {"digest": "0" * 64, "bytes": 11, "suffix": ".mp4"},
                         {"digest": "0" * 64, "bytes": 1, "suffix": "/escape"}):
                with self.assertRaises(ValueError):
                    cache.target(item)

    def test_encoding_profiles_keep_cpu_fallback(self):
        self.assertEqual(media.clip_encoding_args("auto", 24), media.clip_encoding_args("libx264", 24))
        self.assertIn("h264_nvenc", media.clip_encoding_args("h264_nvenc", 24))
        with self.assertRaises(ValueError):
            media.clip_encoding_args("not-an-encoder", 24)


class HelperProcessTests(unittest.TestCase):
    def test_nvenc_smoke_test_is_256_pixels_and_runs_before_pairing(self):
        from video_to_website import worker

        events = []
        def encode(command, **kwargs):
            source = command[command.index("-i") + 1]
            self.assertEqual(source, "color=s=256x256:r=1")
            self.assertIn("h264_nvenc", command)
            events.append("encode")
        with tempfile.TemporaryDirectory() as raw, patch.object(worker, "run", side_effect=encode), \
             patch.object(worker.media, "ffmpeg_bin", return_value="ffmpeg"), \
             patch.object(worker.Client, "post", side_effect=lambda *args: events.append("pair") or {}), \
             patch.object(worker, "Helper") as helper:
            helper.return_value.capabilities = ["clip"]
            self.assertEqual(worker.main(["--server", "http://lessons", "--pairing-code", "code", "--state", raw,
                                          "--clip-encoder", "h264_nvenc"]), 0)
        self.assertEqual(events, ["encode", "pair"])

    def test_failed_encoder_setup_never_registers_a_worker_and_preserves_candidate(self):
        from video_to_website import worker
        from video_to_website.util import CommandError

        with tempfile.TemporaryDirectory() as raw, patch.object(worker, "run", side_effect=CommandError("encoder unavailable")) as encode, \
             patch.object(worker.media, "ffmpeg_bin", return_value="ffmpeg"), patch.object(worker.Client, "post", return_value={}) as pair, \
             patch.object(worker, "Helper") as helper:
            helper.return_value.capabilities = ["clip"]
            args = ["--server", "http://lessons", "--state", raw, "--clip-encoder", "h264_nvenc", "--pairing-code", "first-code"]
            self.assertEqual(worker.main(args), 1)
            pair.assert_not_called()
            saved = json.loads((Path(raw) / "pairing.json").read_text())
            self.assertFalse((Path(raw) / "credentials.json").exists())
            args[-1] = "fresh-code"
            encode.side_effect = None
            self.assertEqual(worker.main(args), 0)
            self.assertEqual(pair.call_args.args[1]["id"], saved["id"])

    def test_new_codes_for_same_server_reuse_identity_and_settings(self):
        from video_to_website import worker

        with tempfile.TemporaryDirectory() as raw, patch.object(worker.Client, "post", return_value={}) as pair, patch.object(worker, "Helper") as helper:
            helper.return_value.capabilities = ["transcribe"]
            self.assertEqual(worker.main(["--state", raw, "--server", "http://lessons/", "--pairing-code", "one", "--cache-gib", "3"]), 0)
            original = json.loads((Path(raw) / "credentials.json").read_text())
            self.assertEqual(worker.main(["--state", raw, "--server", "http://lessons", "--pairing-code", "two"]), 0)
            saved = json.loads((Path(raw) / "credentials.json").read_text())
            self.assertEqual((saved["id"], saved["token"]), (original["id"], original["token"]))
            self.assertEqual(saved["options"]["cache_gib"], 3)
            self.assertEqual(worker.main(["--state", raw, "--server", "http://different-server", "--pairing-code", "three"]), 0)
            self.assertNotEqual(pair.call_args.args[1]["id"], original["id"])
            self.assertNotEqual(pair.call_args.args[1]["token"], original["token"])

    def test_progress_outage_does_not_stop_work_but_revoked_lease_does(self):
        from video_to_website.util import BuildCancelled

        with tempfile.TemporaryDirectory() as raw, patch("video_to_website.worker.shutil.which", return_value="/fake/bin"):
            client = Mock()
            helper = Helper(client, Path(raw), min_free_gib=.00001)
            client.post.side_effect = APIError(503, "temporarily unavailable")
            helper.report("attempt", {})
            self.assertFalse(helper.cancelled.is_set())
            client.post.side_effect = APIError(409, "assignment revoked")
            with self.assertRaises(BuildCancelled):
                helper.report("attempt", {})
            self.assertTrue(helper.cancelled.is_set())

    def test_failed_pairing_preserves_working_identity_and_retries_candidate(self):
        from video_to_website import worker

        with tempfile.TemporaryDirectory() as raw, patch.object(worker.Client, "post", return_value={}) as post, patch.object(worker, "Helper") as factory:
            factory.return_value.capabilities = ["transcribe"]
            arguments = ["--server", "http://lessons", "--state", raw, "--pairing-code", "first-code"]
            self.assertEqual(worker.main(arguments), 0)
            saved = json.loads((Path(raw) / "credentials.json").read_text())
            post.side_effect = OSError("lost pairing response")
            arguments[-1] = "second-code"
            self.assertEqual(worker.main(arguments), 1)
            self.assertEqual(json.loads((Path(raw) / "credentials.json").read_text())["token"], saved["token"])
            pending = json.loads((Path(raw) / "pairing.json").read_text())
            post.side_effect = None
            self.assertEqual(worker.main(arguments), 0)
            self.assertEqual(json.loads((Path(raw) / "credentials.json").read_text())["id"], pending["id"])
            self.assertFalse((Path(raw) / "pairing.json").exists())

    def test_settings_survive_reconnection_and_credentials_are_private(self):
        from video_to_website import worker

        with tempfile.TemporaryDirectory() as raw, patch.object(worker.Client, "post", return_value={}), patch.object(worker, "Helper") as factory:
            factory.return_value.capabilities = ["transcribe"]
            self.assertEqual(worker.main(["--server", "http://lessons", "--pairing-code", "test-code", "--state", raw,
                                          "--device", "cpu", "--cache-gib", "3", "--min-free-gib", "1"]), 0)
            first = json.loads((Path(raw) / "credentials.json").read_text())
            self.assertNotIn("code", first)
            self.assertEqual(worker.main(["--state", raw]), 0)
            self.assertEqual(factory.call_args.kwargs["device"], "cpu")
            self.assertEqual(factory.call_args.kwargs["cache_gib"], 3)
            self.assertEqual(json.loads((Path(raw) / "credentials.json").read_text())["token"], first["token"])
            if sys.platform != "win32":
                self.assertEqual((Path(raw) / "credentials.json").stat().st_mode & 0o777, 0o600)

    def test_windows_pipe_reader_preserves_streaming_callbacks(self):
        from video_to_website.util import _communicate_windows, run

        reports, owners = [], []
        def report(line):
            reports.append(line); owners.append(threading.get_ident())
        with patch("video_to_website.util._communicate_live", side_effect=_communicate_windows):
            result = run([sys.executable, "-c", "import os; os.write(2, 'héllo\\r25%\\n'.encode()); os.write(1,b'ok\\n')"], on_stderr=report)
        self.assertEqual(reports, ["héllo", "25%"])
        self.assertEqual(set(owners), {threading.get_ident()})
        self.assertEqual(result.stdout, "ok\n")
