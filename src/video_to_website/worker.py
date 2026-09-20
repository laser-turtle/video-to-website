"""Optional native helper. No database access, inbound listener, or LLM keys."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
from urllib.parse import urlparse

from . import media, whisper
from .compute import OPERATIONS
from .util import BuildCancelled, digest_file, file_lock, log, process_control, run, warn
from .work_progress import report_work, track_work
from .worker_protocol import HEARTBEAT_SECONDS, KINDS, LEASE_SECONDS, PROTOCOL
from .worker_setup import check_tools, tool_paths

GIB = 1024**3


class APIError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise APIError(400, "Configure the helper with the server's final URL; redirects are disabled.")


class Client:
    def __init__(self, base, token="", *, timeout=10):
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use an HTTP(S) server URL without credentials, a query or fragment.")
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout

    def open(self, method, path, data=None, headers=None):
        headers = {**(headers or {}), "Authorization": "Bearer " + self.token}
        request = urllib.request.Request(self.base + "/api/worker/" + path, data=data, headers=headers, method=method)
        try:
            return urllib.request.build_opener(NoRedirect()).open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            with exc:
                try:
                    detail = json.loads(exc.read(32768)).get("error", str(exc))
                except (ValueError, AttributeError):
                    detail = str(exc)
            raise APIError(exc.code, detail) from exc

    def post(self, path, body=None):
        raw = json.dumps(body or {}, allow_nan=False).encode()
        with self.open("POST", path, raw, {"Content-Type": "application/json"}) as response:
            return json.loads(response.read(16 * 1024**2))


class InputCache:
    def __init__(self, root, *, limit_bytes=20 * GIB, min_free_bytes=2 * GIB):
        self.root, self.limit, self.buffer = Path(root), limit_bytes, min_free_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self.verified = {}

    def target(self, item):
        if not re.fullmatch(r"[a-f0-9]{64}", item.get("digest", "")) or not re.fullmatch(r"\.[a-z0-9]{1,10}", item.get("suffix", "")):
            raise ValueError("Invalid task input identity.")
        if not isinstance(item.get("bytes"), int) or not 0 < item["bytes"] <= self.limit:
            raise ValueError("Task input exceeds this helper's cache limit.")
        return self.root / (item["digest"] + item["suffix"])

    def make_room(self, needed, pinned):
        files = sorted((p for p in self.root.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
        used = sum(p.stat().st_size for p in files)
        for path in files:
            if used + needed <= self.limit and shutil.disk_usage(self.root).free >= needed + self.buffer:
                break
            if path.name in pinned:
                continue
            used -= path.stat().st_size
            path.unlink()
            self.verified.pop(str(path), None)
        if used + needed > self.limit or shutil.disk_usage(self.root).free < needed + self.buffer:
            raise OSError("Not enough helper cache or free disk space for this task.")

    def get(self, client, attempt, name, item, pinned, check):
        target = self.target(item)
        if target.exists():
            stat = target.stat()
            stamp = (stat.st_size, stat.st_mtime_ns)
            if stat.st_size == item["bytes"] and (self.verified.get(str(target)) == stamp or digest_file(target) == item["digest"]):
                target.touch()
                stat = target.stat()
                self.verified[str(target)] = (stat.st_size, stat.st_mtime_ns)
                return target, 0
            target.unlink()
        self.make_room(item["bytes"], pinned)
        partial = target.with_name(".incoming-" + uuid.uuid4().hex)
        try:
            received, digest = 0, hashlib.sha256()
            report_work("download-" + name, "Downloading " + ("model weights" if name == "model" else "task input"),
                        completed=0, total=item["bytes"], unit="bytes")
            with client.open("GET", f"attempts/{attempt}/inputs/{name}") as response, partial.open("xb") as output:
                if int(response.headers.get("Content-Length", "0")) != item["bytes"]:
                    raise ValueError("Task input size does not match its manifest.")
                while chunk := response.read(1024**2):
                    check()
                    received += len(chunk)
                    if received > item["bytes"]:
                        raise ValueError("Task input exceeds its declared size.")
                    if shutil.disk_usage(self.root).free < len(chunk) + self.buffer:
                        raise OSError("Helper disk free-space buffer reached.")
                    digest.update(chunk)
                    output.write(chunk)
                    report_work("download-" + name, "Downloading " + ("model weights" if name == "model" else "task input"),
                                completed=received, total=item["bytes"], unit="bytes")
                output.flush()
                os.fsync(output.fileno())
            if received != item["bytes"] or digest.hexdigest() != item["digest"]:
                raise ValueError("Task input checksum does not match its manifest.")
            partial.replace(target)
            stat = target.stat()
            self.verified[str(target)] = (stat.st_size, stat.st_mtime_ns)
            return target, received
        finally:
            partial.unlink(missing_ok=True)


class UploadStream:
    def __init__(self, stream, size, check):
        self.stream, self.size, self.check = stream, size, check
        self.sent = 0

    def read(self, size=-1):
        self.check()
        data = self.stream.read(size)
        self.sent += len(data)
        report_work("upload", "Returning result to the server", completed=self.sent, total=self.size, unit="bytes")
        return data


class Helper:
    def __init__(self, client, state, *, cache_gib=20, min_free_gib=2, device="auto", clip_encoder="libx264", threads=None):
        self.client, self.state = client, Path(state)
        self.cache = InputCache(self.state / "cache", limit_bytes=int(cache_gib * GIB), min_free_bytes=int(min_free_gib * GIB))
        self.scratch = self.state / "tasks"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.device, self.clip_encoder, self.threads = device, clip_encoder, threads
        self.stopped, self.cancelled = threading.Event(), threading.Event()
        self.guard = threading.Lock()
        self.attempt = None
        self.last_lease = time.monotonic()
        self.capabilities = []
        if shutil.which(os.environ.get("V2W_WHISPER_BIN", "whisper-cli")):
            whisper.whisper_bin()
            self.capabilities.append("transcribe")
        if shutil.which("ffmpeg"):
            self.capabilities += sorted(KINDS - {"transcribe"})
        if not self.capabilities:
            raise ValueError("Install whisper-cli and/or ffmpeg before starting a helper.")
        self.hardware = platform.processor() or platform.machine()
        try:
            if shutil.which("nvidia-smi"):
                result = run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=3, check=False)
                if result.returncode == 0:
                    self.hardware = (result.stdout or "").strip()[:240]
            elif platform.system() == "Darwin":
                result = run(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=3, check=False)
                if result.returncode == 0:
                    self.hardware = (result.stdout or "").strip()[:240]
        except (OSError, RuntimeError):
            pass

    def details(self):
        disk = shutil.disk_usage(self.state)
        return {"hardware": self.hardware, "whisper": "CPU" if self.device == "cpu" else "Automatic native acceleration",
                "clip_encoder": self.clip_encoder, "cache_limit_bytes": self.cache.limit,
                "cache_used_bytes": sum(p.stat().st_size for p in self.cache.root.iterdir() if p.is_file()),
                "free_bytes": disk.free, "buffer_bytes": self.cache.buffer}

    def heartbeat(self):
        with self.guard:
            attempt = self.attempt
        details = self.details()
        body = self.client.post("heartbeat", {"protocol": PROTOCOL, "platform": platform.system() + " / " + platform.machine(),
            "capabilities": self.capabilities if details["free_bytes"] > self.cache.buffer else [],
            "details": details, "attempt": attempt})
        with self.guard:
            if attempt == self.attempt:
                if attempt and not body["continue"]:
                    self.cancelled.set()
                else:
                    self.last_lease = time.monotonic()
        return body

    def heartbeats(self):
        while not self.stopped.wait(HEARTBEAT_SECONDS):
            try:
                self.heartbeat()
            except APIError as exc:
                if exc.status in (401, 409):
                    warn(str(exc))
                    self.stopped.set()
                    self.cancelled.set()
            except (OSError, ValueError, urllib.error.URLError):
                pass

    def check(self):
        if self.stopped.is_set() or self.cancelled.is_set() or time.monotonic() - self.last_lease > LEASE_SECONDS - 5:
            raise BuildCancelled("Task lease ended or the server requested cancellation.")
        if shutil.disk_usage(self.state).free < self.cache.buffer:
            raise OSError("Helper disk free-space buffer reached.")

    def report(self, attempt, payload):
        try:
            self.client.post(f"attempts/{attempt}/progress", payload)
        except APIError as exc:
            if exc.status in (401, 409):
                self.cancelled.set()
                raise BuildCancelled(str(exc)) from exc
            if exc.status < 500:
                raise
            # Telemetry loss must not stop healthy native work. Heartbeats and
            # check() independently enforce the lease if the API stays down.

    def execute(self, task):
        attempt, kind = task["attempt"], task["kind"]
        if not re.fullmatch(r"[a-f0-9]{32}", attempt):
            raise ValueError("Invalid task attempt identity.")
        if kind not in self.capabilities:
            raise ValueError("This helper does not support that operation.")
        with self.guard:
            self.attempt = attempt
            self.last_lease = time.monotonic()
            self.cancelled.clear()
        operation = OPERATIONS[kind]
        transferred = 0
        try:
            with tempfile.TemporaryDirectory(prefix=attempt + "-", dir=self.scratch) as raw, process_control(self.check), track_work(
                lambda payload: self.report(attempt, payload)
            ):
                paths = {}
                pinned = {self.cache.target(item).name for item in task["inputs"].values()}
                for name, item in task["inputs"].items():
                    paths[name], count = self.cache.get(self.client, attempt, name, item, pinned, self.check)
                    transferred += count
                parameters = dict(task["params"])
                parameters[operation["source"]] = paths["source"]
                output = None
                if operation["output"]:
                    extension = {"frame": ".jpg", "clip": ".mp4", "transcribe": ""}[kind]
                    output = Path(raw) / ("output" + extension)
                    parameters[operation["output"]] = output
                if kind == "transcribe":
                    parameters["model_path"] = paths["model"]
                    parameters["threads"] = self.threads
                    parameters["extra_args"] = ["--no-gpu"] if self.device == "cpu" else []
                if kind == "clip" and parameters.get("encoder") == "auto":
                    parameters["encoder"] = self.clip_encoder
                # The operation registry rejects unexpected parameters; no code,
                # shell commands or filesystem paths are supplied by the server.
                operation["signature"].bind(**parameters)
                self.check()
                started = time.monotonic()
                report_work("processing", "Processing on this helper", estimate=False)
                value = operation["function"](**parameters)
                elapsed = time.monotonic() - started
                self.check()
                media_seconds = 0
                if kind == "transcribe":
                    with wave.open(str(paths["source"]), "rb") as audio:
                        media_seconds = audio.getnframes() / audio.getframerate()
                    output = Path(str(output) + ".json")
                if output:
                    size = output.stat().st_size
                    with output.open("rb") as stream, self.client.open("PUT", f"attempts/{attempt}/artifact",
                        UploadStream(stream, size, self.check), {"Content-Length": str(size),
                        "Content-Type": "application/octet-stream", "X-Content-SHA256": digest_file(output)}) as response:
                        response.read(32768)
                    transferred += size
                metrics = {"processing_seconds": elapsed, "transfer_bytes": transferred,
                           "media_seconds": media_seconds,
                           "engine": parameters.get("encoder", "whisper.cpp (" + self.device + ")" if kind == "transcribe" else "ffmpeg CPU")}
                result = {"value": None if isinstance(value, Path) else value, "metrics": metrics}
                # Retrying a lost completion acknowledgement does not rerun the
                # expensive operation or increment contribution counts again.
                for retry in range(3):
                    try:
                        self.client.post(f"attempts/{attempt}/complete", result)
                        break
                    except (OSError, urllib.error.URLError):
                        if retry == 2:
                            raise
                        time.sleep(.5)
                    except APIError as exc:
                        if exc.status not in (500, 502, 503, 504) or retry == 2:
                            raise
                        time.sleep(.5)
                log(f"completed {kind} in {elapsed:.1f}s")
        finally:
            with self.guard:
                self.attempt = None

    def run(self):
        self.heartbeat()
        thread = threading.Thread(target=self.heartbeats, daemon=True)
        thread.start()
        try:
            while not self.stopped.is_set():
                try:
                    task = self.client.post("claim")["task"]
                    if task is None:
                        self.stopped.wait(.25)
                        continue
                    try:
                        self.execute(task)
                    except (Exception, SystemExit) as exc:
                        warn(f"{task['kind']}: {exc}")
                        with contextlib.suppress(Exception):
                            self.client.post(f"attempts/{task['attempt']}/fail", {"error": str(exc)[:1000],
                                "category": "server_storage" if isinstance(exc, APIError) and exc.status == 507 else "worker"})
                except APIError as exc:
                    if exc.status in (401, 409):
                        raise
                    warn(str(exc))
                    self.stopped.wait(5)
                except (OSError, urllib.error.URLError) as exc:
                    warn(f"Server unavailable: {exc}")
                    self.stopped.wait(5)
        finally:
            self.stopped.set()
            thread.join(timeout=11)


def write_credentials(path, data):
    fd, name = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def cleanup_scratch(state):
    """Called with the helper process lock held, before accepting new work."""
    for path in (state / "tasks").glob("*"):
        if re.fullmatch(r"[a-f0-9]{32}-[a-z0-9_]{8}", path.name) and path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
    for path in (state / "cache").glob(".incoming-*"):
        if re.fullmatch(r"\.incoming-[a-f0-9]{32}", path.name) and path.is_file() and not path.is_symlink():
            path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="v2w-worker", description="Contribute native processing to your video-to-website server.")
    parser.add_argument("--server", help="server URL, e.g. https://lessons.example")
    parser.add_argument("--pairing-code", help="single-use code from the Workers page")
    parser.add_argument("--name", default=socket.gethostname())
    parser.add_argument("--state", type=Path, default=Path.home() / ".v2w-worker")
    parser.add_argument("--cache-gib", type=float, help="input cache limit (default: 20 GiB; saved for reconnects)")
    parser.add_argument("--min-free-gib", type=float, help="helper free-space buffer (default: 2 GiB; saved)")
    parser.add_argument("--device", choices=["auto", "cpu"], help="Whisper acceleration (default: auto; saved)")
    parser.add_argument("--clip-encoder", choices=["libx264", "h264_nvenc"], help="helper clip encoder (default: libx264; saved)")
    parser.add_argument("--threads", type=int)
    parser.add_argument("--check", action="store_true", help="check native tools and show setup instructions without connecting")
    parser.add_argument("--tools", action="append", type=Path, help="folder containing native executables (repeatable; saved for reconnects)")
    args = parser.parse_args(argv)
    state = args.state.expanduser().resolve()
    if args.check:
        try:
            saved = json.loads((state / "credentials.json").read_text()) if (state / "credentials.json").is_file() else {}
            with tool_paths(args.tools if args.tools is not None else saved.get("tool_paths", [])):
                return check_tools()
        except (OSError, ValueError) as exc:
            warn(str(exc))
            return 1
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with file_lock(state / "helper.lock", blocking=False), contextlib.ExitStack() as setup:
            cleanup_scratch(state)
            path = state / "credentials.json"
            pairing_path = state / "pairing.json"
            existing = json.loads(path.read_text()) if path.is_file() else None
            candidate = json.loads(pairing_path.read_text()) if pairing_path.is_file() else None
            if args.pairing_code:
                if not args.server:
                    parser.error("Pairing requires --server.")
                pairing_digest = hashlib.sha256(args.pairing_code.encode()).hexdigest()
                credentials = next((dict(item) for item in (candidate, existing) if item and
                    item.get("pairing_digest") == pairing_digest and item.get("server", "").rstrip("/") == args.server.rstrip("/")), None)
                if credentials is None:
                    # A new code authorizes the same installation again; it must
                    # not create another computer or discard its saved settings.
                    credentials = next((dict(item) for item in (existing, candidate) if item and
                        item.get("server", "").rstrip("/") == args.server.rstrip("/")), None)
                if credentials is None:
                    credentials = {"id": uuid.uuid4().hex, "token": secrets.token_urlsafe(32), "server": args.server}
                # Preserve a working identity until a new exchange succeeds.
                # Retrying the same command reuses the candidate after a lost ACK.
                credentials.update(code=args.pairing_code, name=args.name, pairing_digest=pairing_digest)
                write_credentials(pairing_path, credentials)
            elif existing or candidate:
                credentials = existing or candidate
            else:
                parser.error("Create a pairing code on the Workers page, then supply --server and --pairing-code.")
            if args.server and args.server.rstrip("/") != credentials["server"].rstrip("/"):
                parser.error("Pair with the new server before changing the server URL.")
            paths = args.tools if args.tools is not None else credentials.get("tool_paths", [])
            credentials["tool_paths"] = [str(Path(path).expanduser().resolve()) for path in paths]
            setup.enter_context(tool_paths(credentials["tool_paths"]))
            defaults = {"cache_gib": 20, "min_free_gib": 2, "device": "auto", "clip_encoder": "libx264", "threads": None}
            options = {**defaults, **credentials.get("options", {})}
            for key in defaults:
                if getattr(args, key) is not None:
                    options[key] = getattr(args, key)
            if (not all(math.isfinite(options[k]) and options[k] > 0 for k in ("cache_gib", "min_free_gib"))
                or (options["threads"] is not None and options["threads"] < 1)):
                parser.error("Cache size, free-space buffer and thread count must be positive.")
            if options["device"] not in ("auto", "cpu") or options["clip_encoder"] not in ("libx264", "h264_nvenc"):
                parser.error("Invalid saved acceleration settings; override them on the command line.")
            credentials["options"] = options
            client = Client(credentials["server"], credentials["token"])
            if credentials.get("code"):
                write_credentials(pairing_path, credentials)
            # Validate native tools before registering. A failed setup check
            # leaves a retryable local candidate, not a dead server-side worker.
            if options["clip_encoder"] != "libx264":
                run([media.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=s=256x256:r=1",
                     "-frames:v", "1", *media.clip_encoding_args(options["clip_encoder"], 24), "-f", "null", "-"], timeout=30)
            helper = Helper(client, state, **options)
            if credentials.get("code"):
                client.post("pair", {key: credentials[key] for key in ("id", "token", "name", "code")})
                credentials.pop("code")
            write_credentials(path, credentials)
            if args.pairing_code or (candidate and credentials["id"] == candidate["id"]):
                pairing_path.unlink(missing_ok=True)
            log(f"helper connected to {client.base}; {', '.join(helper.capabilities)}; Ctrl-C to stop")
            helper.run()
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        warn(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
