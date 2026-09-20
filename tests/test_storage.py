"""Capacity is per filesystem, including active uploads and crash recovery."""

import collections
import http.client
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from video_to_website.cli import _build_parser
from video_to_website.ingest import IngestHandler
from video_to_website.storage import GIB, StorageFull, StorageLocation, StorageManager

DiskUsage = collections.namedtuple("DiskUsage", "total used free")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a, self.b = self.root / "a", self.root / "b"
        self.a.mkdir()
        self.b.mkdir()
        self.devices = {self.a: "shared", self.b: "shared"}
        self.capacity = {"shared": 500, "second": 300}
        self.manager = StorageManager(
            self.root / "state",
            [
                StorageLocation("main", self.a, "Main", 100),
                StorageLocation("archive", self.b, "Archive", 100),
            ],
        )

        def device(path):
            return next(
                value
                for root, value in self.devices.items()
                if Path(path).resolve().is_relative_to(root.resolve())
            )

        def usage(path):
            wanted = device(path)
            used = sum(
                file.stat().st_size
                for root, value in self.devices.items()
                if value == wanted
                for file in root.rglob("*")
                if file.is_file()
            )
            return DiskUsage(self.capacity[wanted], used, self.capacity[wanted] - used)

        self.addCleanup(patch.stopall)
        patch("video_to_website.storage.filesystem_id", side_effect=device).start()
        patch("video_to_website.storage.shutil.disk_usage", side_effect=usage).start()

    def test_stats_keep_free_buffer_and_show_available_capacity(self):
        (self.a / "existing.mp4").write_bytes(b"x" * 125)
        status = self.manager.status("main")
        self.assertEqual(status["free_bytes"], 375)
        self.assertEqual(status["available_bytes"], 275)
        self.assertEqual(status["buffer_bytes"], 100)

    def test_same_disk_reservations_are_shared_and_written_bytes_not_double_counted(
        self,
    ):
        with self.manager.reserve("main", self.a / "one.mp4", 250) as first:
            self.assertEqual(self.manager.status("archive")["available_bytes"], 150)
            first.write(b"a" * 100)
            status = self.manager.status("archive")
            self.assertEqual(status["reserved_bytes"], 150)
            self.assertEqual(status["available_bytes"], 150)
            with self.assertRaises(StorageFull):
                with self.manager.reserve("archive", self.b / "too-large.mp4", 151):
                    pass
            with self.manager.reserve("archive", self.b / "two.mp4", 150) as second:
                second.write(b"b" * 150)
                second.commit()
                self.assertEqual(self.manager.status("main")["reserved_bytes"], 150)
                first.write(b"a" * 150)
                first.commit()
        self.assertEqual(self.manager.status("main")["free_bytes"], 100)
        self.assertFalse(self.manager.status("main")["accepting_uploads"])
        self.assertEqual((self.a / "one.mp4").stat().st_size, 250)

    def test_separate_disks_have_independent_capacity(self):
        self.devices[self.b] = "second"
        with self.manager.reserve("main", self.a / "one.mp4", 350):
            self.assertEqual(self.manager.status("main")["available_bytes"], 50)
            self.assertEqual(self.manager.status("archive")["available_bytes"], 200)
            with self.manager.reserve("archive", self.b / "two.mp4", 200) as second:
                second.write(b"b" * 200)
                second.commit()
        self.assertEqual(self.manager.status("main")["available_bytes"], 400)

    def test_shared_disk_uses_the_larger_configured_buffer(self):
        manager = StorageManager(
            self.root / "state",
            [
                StorageLocation("main", self.a, buffer_bytes=100),
                StorageLocation("archive", self.b, buffer_bytes=200),
            ],
        )
        self.assertEqual(manager.status("main")["available_bytes"], 300)
        self.assertEqual(manager.status("archive")["buffer_bytes"], 200)

    def test_external_disk_growth_stops_upload_and_cleans_partial_file(self):
        with self.assertRaises(StorageFull):
            with self.manager.reserve("main", self.a / "new.mp4", 200) as upload:
                upload.write(b"a" * 50)
                (self.b / "other-process.dat").write_bytes(b"x" * 250)
                upload.write(b"a" * 150)
        self.assertFalse((self.a / "new.mp4").exists())
        self.assertEqual(list(self.a.glob(".incoming-*")), [])
        self.assertEqual(self.manager.status("main")["reserved_bytes"], 0)

    def test_replacements_need_room_for_the_new_file_before_removing_the_old_one(self):
        target = self.a / "original.mp4"
        target.write_bytes(b"o" * 300)
        with self.assertRaises(StorageFull):
            with self.manager.reserve("main", target, 150):
                pass
        self.assertEqual(target.read_bytes(), b"o" * 300)

    def test_disconnect_releases_capacity_and_removes_partial_file(self):
        with self.assertRaisesRegex(OSError, "disconnected"):
            with self.manager.reserve("main", self.a / "new.mp4", 300) as upload:
                upload.write(b"x" * 70)
                raise OSError("disconnected")
        self.assertEqual(self.manager.status("main")["available_bytes"], 400)
        self.assertEqual(list(self.a.iterdir()), [])

    def test_destination_cannot_escape_its_registered_root(self):
        with self.assertRaises(ValueError):
            self.manager.status("main", self.b)

    def test_api_reports_capacity_and_rejects_before_reading_a_body(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), IngestHandler)
        server.library = self.a
        server.storage = StorageManager(
            self.root / "state", [StorageLocation("library", self.a, buffer_bytes=100)]
        )
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/storage")
        response = conn.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["destination"]["available_bytes"], 400)
        self.assertEqual(payload["locations"][0]["id"], "library")
        conn.putrequest("PUT", "/api/library/course/too-large.mp4")
        conn.putheader("Content-Length", "401")
        conn.endheaders()
        # No request body is sent. Rejection must happen at admission.
        response = conn.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 507)
        self.assertIn("free-space buffer", payload["error"])
        self.assertFalse((self.a / "course" / "too-large.mp4").exists())

    def test_buffer_cli_options_apply_to_each_upload_server(self):
        parser = _build_parser()
        for command in (
            ["api", "library", "--state", "state"],
            ["serve"],
            ["watch", "library"],
        ):
            args = parser.parse_args([*command, "--min-free-gib", "2.5"])
            self.assertEqual(args.min_free_bytes, int(2.5 * GIB))


class CrashRecoveryTests(unittest.TestCase):
    def test_dead_uploader_releases_reservation_and_abandoned_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "library"
            library.mkdir()
            script = """import sys,time
from pathlib import Path
from video_to_website.storage import StorageLocation,StorageManager
root=Path(sys.argv[1]);library=root/'library'
manager=StorageManager(root/'state',[StorageLocation('library',library,buffer_bytes=0)])
with manager.reserve('library',library/'upload.mp4',100) as upload:
    upload.write(b'x'*20)
    (root/'ready').touch()
    time.sleep(30)
"""
            process = subprocess.Popen([sys.executable, "-c", script, str(root)])
            try:
                deadline = time.monotonic() + 5
                while not (root / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((root / "ready").exists())
                manager = StorageManager(
                    root / "state",
                    [StorageLocation("library", library, buffer_bytes=0)],
                )
                self.assertEqual(manager.status("library")["reserved_bytes"], 80)
            finally:
                process.kill()
                process.wait(timeout=5)
            self.assertEqual(manager.status("library")["reserved_bytes"], 0)
            self.assertEqual(list(library.iterdir()), [])
