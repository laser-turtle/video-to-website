"""HTTP boundary for helper pairing, task leases and bounded artifact transfers."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .storage import DEFAULT_BUFFER, StorageFull, StorageLocation, StorageManager
from .worker_store import WorkerConflict, WorkerStore, WorkerUnauthorized


def handle_worker_api(handler):
    path = urlparse(handler.path).path
    if not (path == "/api/workers" or path.startswith(("/api/workers/", "/api/worker/"))):
        return False
    if not getattr(handler.server, "workers_enabled", True):
        handler._json(503, {"error": "Remote helpers are disabled on this server."}, close=True)
        return True
    if handler.catalog is None:
        handler._json(503, {"error": "Workers require the durable service."}, close=True)
        return True
    api = WorkerAPI(handler, WorkerStore(handler.catalog))
    def fail(status, body):
        if api.streaming:
            # A partial binary response can only end with a closed connection;
            # appending a JSON error would corrupt its declared byte stream.
            handler.close_connection = True
        else:
            handler._json(status, body, close=True)
    try:
        api.handle(path)
    except WorkerUnauthorized as exc:
        fail(401, {"error": str(exc)})
    except WorkerConflict as exc:
        fail(409, {"error": str(exc)})
    except KeyError:
        fail(404, {"error": "Worker or task not found."})
    except StorageFull as exc:
        fail(507, {"error": str(exc), "storage": exc.status})
    except (ValueError, TypeError) as exc:
        fail(400, {"error": str(exc)})
    except (BrokenPipeError, ConnectionResetError):
        pass
    except OSError:
        fail(503, {"error": "Worker transfer could not finish. Check the connection and server storage."})
    return True


class WorkerAPI:
    def __init__(self, handler, store):
        self.h, self.store = handler, store
        self.streaming = False

    def body(self, limit=32768):
        if self.h.headers.get("Transfer-Encoding"):
            raise ValueError("A Content-Length is required.")
        size = int(self.h.headers.get("Content-Length", "0"))
        if not 0 < size <= limit:
            raise ValueError("Missing or oversized request body.")
        self.h.connection.settimeout(30)
        raw = self.h.rfile.read(size)
        if len(raw) != size:
            raise ValueError("Incomplete request body.")
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object.")
        return body

    def handle(self, path):
        method = self.h.command
        parts = path.strip("/").split("/")
        if parts[1] == "workers":
            # These controls share the existing trusted-LAN management boundary.
            if method == "GET" and len(parts) == 2:
                self.store.expire()
                self.h._json(200, self.store.overview())
            elif method in ("GET", "HEAD") and parts[2:] in (["download"], ["download-info"]):
                self.helper_download(info=parts[2] == "download-info")
            elif method == "POST" and parts[2:] == ["pairing"]:
                self.h._json(201, self.store.pairing(), close=True)
            elif method == "POST" and len(parts) == 3:
                self.store.manage(parts[2], self.body())
                self.h._json(200, {"saved": True}, close=True)
            else:
                self.h._json(404, {"error": "Unknown worker management endpoint."}, close=True)
            return
        if method == "POST" and parts[2:] == ["pair"]:
            self.h._json(200, self.store.pair(self.body()), close=True)
            return
        authorization = self.h.headers.get("Authorization", "")
        worker_id = self.store.authenticate(authorization[7:] if authorization.startswith("Bearer ") else "")
        if method == "POST" and parts[2:] == ["heartbeat"]:
            self.h._json(200, self.store.heartbeat(worker_id, self.body()), close=True)
        elif method == "POST" and parts[2:] == ["claim"]:
            self.store.expire()
            self.h._json(200, {"task": self.store.claim(worker_id)}, close=True)
        elif len(parts) >= 4 and parts[2] == "attempts":
            attempt_id = parts[3]
            if not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
                raise KeyError(attempt_id)
            action = parts[4:]
            if method == "POST" and action == ["progress"]:
                self.store.progress(worker_id, attempt_id, self.body())
                self.h._json(200, {"saved": True}, close=True)
            elif method == "POST" and action == ["complete"]:
                body = self.body(16 * 1024**2)
                self.h._json(200, self.store.complete(worker_id, attempt_id, body.get("value"), body.get("metrics")), close=True)
            elif method == "POST" and action == ["fail"]:
                body = self.body()
                self.store.fail(worker_id, attempt_id, body.get("error", "Helper processing failed."), category=body.get("category", "worker"))
                self.h._json(200, {"saved": True}, close=True)
            elif method == "GET" and len(action) == 2 and action[0] == "inputs":
                self.download(worker_id, attempt_id, action[1])
            elif method == "PUT" and action == ["artifact"]:
                self.upload(worker_id, attempt_id)
            else:
                self.h._json(404, {"error": "Unknown task endpoint."}, close=True)
        else:
            self.h._json(404, {"error": "Unknown helper endpoint."}, close=True)

    def helper_download(self, *, info=False):
        from .worker_bundle import bundle, bundle_info

        metadata = bundle_info()
        payload, digest = bundle()
        expected = parse_qs(urlparse(self.h.path).query).get("sha256", [digest])[0]
        if expected != digest:
            raise WorkerConflict("The server's helper was updated. Reopen Connect a computer for the latest download and command.")
        if info:
            payload = json.dumps(metadata).encode("utf-8")
        self.h.send_response(200)
        self.h.send_header("Content-Type", "application/json" if info else "application/octet-stream")
        if not info:
            self.h.send_header("Content-Disposition", 'attachment; filename="' + metadata["filename"] + '"')
            self.h.send_header("X-Content-SHA256", digest)
        self.h.send_header("Content-Length", str(len(payload)))
        self.h.send_header("X-Content-Type-Options", "nosniff")
        self.h.send_header("Cache-Control", "no-store")
        self.h.end_headers()
        self.streaming = True
        if self.h.command != "HEAD":
            self.h.wfile.write(payload)

    def download(self, worker_id, attempt_id, name):
        _, task = self.store.owned(worker_id, attempt_id)
        item = json.loads(task["spec"])["inputs"].get(name)
        if not item:
            raise KeyError(name)
        path = Path(item["path"])
        if path.stat().st_size != item["bytes"]:
            raise WorkerConflict("Task input changed; returning work to the server.")
        with path.open("rb") as stream:
            self.h.send_response(200)
            self.h.send_header("Content-Type", "application/octet-stream")
            self.h.send_header("Content-Length", str(item["bytes"]))
            self.h.send_header("X-Content-SHA256", item["digest"])
            self.h.send_header("Cache-Control", "no-store")
            self.h.send_header("Connection", "close")
            self.h.end_headers()
            self.streaming = True
            self.h.close_connection = True
            self.h.connection.settimeout(30)
            while chunk := stream.read(1024**2):
                self.store.owned(worker_id, attempt_id)
                self.h.wfile.write(chunk)

    def upload(self, worker_id, attempt_id):
        _, task = self.store.owned(worker_id, attempt_id)
        if not json.loads(task["spec"]).get("output"):
            raise ValueError("This task has no artifact output.")
        if self.h.headers.get("Transfer-Encoding"):
            raise ValueError("A Content-Length is required.")
        size = int(self.h.headers.get("Content-Length", "0"))
        digest = self.h.headers.get("X-Content-SHA256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Output checksum is required.")
        self.store.root.mkdir(parents=True, exist_ok=True)
        # Each retry uploads to a distinct file. A stale upload can never replace
        # an artifact that a completed attempt has already referenced.
        target = self.store.root / task["id"] / attempt_id / uuid.uuid4().hex
        storage = StorageManager(self.store.catalog.directory / "storage", [StorageLocation(
            "worker-results", self.store.root,
            buffer_bytes=getattr(self.h.server, "min_free_bytes", DEFAULT_BUFFER))])
        self.h.connection.settimeout(30)
        accepted = False
        try:
            with storage.reserve("worker-results", target, size) as upload:
                hasher = hashlib.sha256()
                remaining = size
                while remaining:
                    chunk = self.h.rfile.read(min(1024**2, remaining))
                    if not chunk:
                        raise OSError("Incomplete artifact upload.")
                    self.store.owned(worker_id, attempt_id)
                    hasher.update(chunk)
                    upload.write(chunk)
                    remaining -= len(chunk)
                if hasher.hexdigest() != digest:
                    raise ValueError("Output checksum does not match its contents.")
                validate_artifact(task["kind"], upload.partial)
                self.store.owned(worker_id, attempt_id)
                upload.commit()
                self.store.record_artifact(worker_id, attempt_id, target, digest, size)
                accepted = True
            self.h._json(200, {"stored": True}, close=True)
        finally:
            if not accepted:
                target.unlink(missing_ok=True)


def validate_artifact(kind, path):
    with path.open("rb") as stream:
        head = stream.read(32)
    if kind == "frame" and not head.startswith(b"\xff\xd8\xff"):
        raise ValueError("Expected a JPEG screenshot.")
    if kind == "clip" and head[4:8] != b"ftyp":
        raise ValueError("Expected an MP4 clip.")
    if kind == "transcribe":
        if path.stat().st_size > 16 * 1024**2:
            raise ValueError("Raw transcript exceeds 16 MiB.")
        blob = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(blob, dict) or not isinstance(blob.get("transcription"), list):
            raise ValueError("Expected Whisper transcript JSON.")
