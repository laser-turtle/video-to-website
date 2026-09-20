"""A downloaded helper must work outside a checkout and without site packages."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from video_to_website import worker
from video_to_website.worker_bundle import MODULES, bundle, bundle_info
from video_to_website.worker_setup import check_tools, tool_paths


class BundleTests(unittest.TestCase):
    def test_bundle_is_reproducible_and_contains_only_helper_code(self):
        first, digest = bundle()
        bundle.cache_clear()
        self.assertEqual(bundle(), (first, digest))
        self.assertEqual(hashlib.sha256(first).hexdigest(), digest)
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertEqual(set(archive.namelist()), {"__main__.py"} | {"video_to_website/" + name for name in MODULES})
            self.assertNotIn("worker_store.py", " ".join(archive.namelist()))
            self.assertNotIn("catalog", " ".join(archive.namelist()))

    def test_download_runs_with_isolated_python_and_no_packages_or_tools(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / bundle_info()["filename"]
            archive.write_bytes(bundle()[0])
            env = {**os.environ, "PATH": "", "PYTHONPATH": "/missing", "V2W_WHISPER_BIN": "whisper-cli"}
            result = subprocess.run([sys.executable, "-I", "-S", str(archive), "--help"], cwd=root,
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--pairing-code", result.stdout)
            self.assertIn("--check", result.stdout)
            state = root / "not-created"
            result = subprocess.run([sys.executable, "-I", "-S", str(archive), "--check", "--state", str(state)],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("FFmpeg: missing", result.stdout)
            self.assertIn("Whisper: missing", result.stdout)
            self.assertFalse(state.exists(), "A preflight must not pair or create state")

    def test_check_accepts_one_working_tool_and_explains_broken_binaries(self):
        with patch("video_to_website.worker_setup.shutil.which", side_effect=["/ffmpeg", "/whisper"]), \
             patch("video_to_website.worker_setup.run", side_effect=[subprocess.CompletedProcess([], 0), OSError("missing runtime")]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check_tools(), 0)
            self.assertIn("Ready to connect for visual processing", output.getvalue())
            self.assertIn("missing runtime", output.getvalue())

    def test_portable_tools_do_not_change_system_path_permanently(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "tools" / "bin").mkdir(parents=True)
            explicit = root / "explicit"; explicit.mkdir()
            nested = root / "tools" / "ffmpeg-release" / "bin"; nested.mkdir(parents=True)
            (nested / "ffmpeg.exe").touch()
            whisper_dir = root / "tools" / "Release"; whisper_dir.mkdir()
            (whisper_dir / "whisper-cli.exe").touch()
            old = os.environ.get("PATH")
            with patch.object(sys, "argv", [str(root / "worker.pyz")]), tool_paths([explicit]):
                paths = os.environ["PATH"].split(os.pathsep)
                self.assertEqual(paths[:3], [str(explicit.resolve()), str((root / "tools").resolve()), str((root / "tools" / "bin").resolve())])
                self.assertIn(str(nested.resolve()), paths)
                self.assertIn(str(whisper_dir.resolve()), paths)
            self.assertEqual(os.environ.get("PATH"), old)
            with self.assertRaises(ValueError), tool_paths([root / "missing"]):
                pass

    def test_custom_tool_folders_are_saved_and_reused_for_checks(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(worker.Client, "post", return_value={}) as pair, patch.object(worker, "Helper") as helper:
            helper.return_value.capabilities = ["transcribe"]
            tools = Path(raw) / "native"; tools.mkdir()
            state = Path(raw) / "state"
            old = os.environ.get("PATH")
            pair.side_effect = OSError("interrupted pairing")
            self.assertEqual(worker.main(["--server", "http://lessons", "--pairing-code", "test-code", "--state", str(state), "--tools", str(tools)]), 1)
            pair.side_effect = None
            self.assertEqual(worker.main(["--state", str(state)]), 0, "Tool folders survive an interrupted initial pairing")
            self.assertEqual(set(pair.call_args.args[1]), {"id", "token", "name", "code"})
            self.assertEqual(os.environ.get("PATH"), old)
            def inspect_path():
                self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], str(tools.resolve()))
                return 0
            with patch.object(worker, "check_tools", side_effect=inspect_path):
                self.assertEqual(worker.main(["--check", "--state", str(state)]), 0)
            self.assertEqual(os.environ.get("PATH"), old)
