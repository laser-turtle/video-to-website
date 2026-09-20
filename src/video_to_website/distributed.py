"""Coordinate optional remote native operations within existing DBOS steps."""

from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path

from .compute import OPERATIONS, operation_description
from .util import BuildCancelled, CommandError, _process_check, digest_file, digest_json
from .work_progress import report_work, track_work
from .worker_store import WorkerConflict, WorkerStore

CPU_SLOT = threading.RLock()
PREPARE_SLOT = threading.RLock()
LLM_SLOT = threading.Lock()
OFFER_SECONDS = 2.5


def check_cancelled():
    check = _process_check.get()
    if check:
        check()


@contextlib.contextmanager
def acquire(slot):
    while not slot.acquire(timeout=.2):
        check_cancelled()
    try:
        check_cancelled()
        yield
    finally:
        slot.release()


@contextlib.contextmanager
def try_cpu():
    taken = CPU_SLOT.acquire(timeout=.2)
    try:
        yield taken
    finally:
        if taken:
            CPU_SLOT.release()


class ComputeExecutor:
    def __init__(self, catalog, build_id, stage, *, digest_cache=None, source_directory=None, source_digest=None):
        self.catalog, self.build_id, self.stage = catalog, build_id, stage
        self.store = WorkerStore(catalog)
        self.digests = digest_cache if digest_cache is not None else {}
        self.source_directory = Path(source_directory).resolve() if source_directory else None
        self.source_digest = source_digest
        self._maintenance_guard = threading.Lock()
        self._next_expiry = 0
        self._next_helpers = 0
        self._media_helpers = False

    def media_window(self, maximum):
        now = time.monotonic()
        if now >= self._next_helpers:
            kinds = {"frame", "frame_hash", "activity", "clip"}
            self._media_helpers = any(kinds.intersection(json.loads(w["capabilities"])) for w in self.store.available())
            self._next_helpers = now + 1
        return maximum if self._media_helpers else 1

    def expire_due(self):
        # Sibling pollers share one maintenance cadence rather than each
        # acquiring a SQLite write transaction on every poll.
        if time.monotonic() >= self._next_expiry and self._maintenance_guard.acquire(blocking=False):
            try:
                if time.monotonic() >= self._next_expiry:
                    self._next_expiry = time.monotonic() + .5
                    self.store.expire()
            finally:
                self._maintenance_guard.release()

    def release_media_batch(self):
        self.store.release_stage(self.build_id, self.stage)

    def input(self, path):
        path = Path(path).resolve()
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        digest = self.digests.get(key)
        if digest is None:
            # prepare_source already verified this immutable snapshot. Avoid
            # reading the entire video again just to offer each native task.
            digest = self.source_digest if path.parent == self.source_directory and path.name.startswith("source.") else digest_file(path)
            self.digests[key] = digest
        return {"path": str(path), "digest": digest, "bytes": stat.st_size,
                "suffix": path.suffix.lower()[:12] or ".bin"}

    def run(self, kind, arguments, function):
        operation = OPERATIONS[kind]
        # Preparing the next helper's input must not wait behind a long local
        # transcription. Keep one separate, bounded preparation lane.
        if not operation["remote"]:
            with acquire(PREPARE_SLOT):
                return function()
        check_cancelled()
        inputs = {"source": self.input(arguments[operation["source"]])}
        params = {key: value for key, value in arguments.items()
                  if key not in (operation["source"], operation["output"], "model_path")}
        if kind == "transcribe":
            inputs["model"] = self.input(arguments["model_path"])
            # Arbitrary extra switches are only supported by local callers.
            if params.get("extra_args"):
                with acquire(CPU_SLOT):
                    return function()
        output = Path(arguments[operation["output"]]) if operation["output"] else None
        if kind == "transcribe":
            output = Path(str(output) + ".json")
        task_id = digest_json({"build": self.build_id, "stage": self.stage, "kind": kind,
                               "inputs": {key: value["digest"] for key, value in inputs.items()},
                               "params": params, "output": str(output) if output else None})
        spec = {"inputs": inputs, "params": params, "output": str(output.resolve()) if output else None,
                "description": operation_description()}
        try:
            task = self.store.ensure_task(task_id, self.build_id, self.stage, kind, spec)
        except WorkerConflict as exc:
            raise BuildCancelled(str(exc)) from exc
        offered = time.monotonic()
        last_progress = None
        while True:
            check_cancelled()
            self.expire_due()
            task = self.store.task(task_id)
            if task["state"] == "accepted":
                return self.restore(task, output, arguments)
            if task["state"] == "cancelled":
                raise BuildCancelled("Lesson was cancelled or replaced.")
            if task["state"] == "failed":
                raise CommandError(task["error"] or "The server could not accept the task result.")
            if task["state"] == "running":
                attempts = self.catalog.rows("SELECT a.*,w.name FROM compute_attempts a JOIN workers w ON w.id=a.worker_id WHERE a.id=?",
                                             (task["current_attempt"],))
                if attempts and attempts[0]["progress"] != last_progress:
                    last_progress = attempts[0]["progress"]
                    if last_progress:
                        payload = json.loads(last_progress)
                        report_work("remote-" + task["current_attempt"] + "-" + payload["phase"], payload["label"],
                                    completed=payload["completed"], total=payload["total"], unit=payload["unit"],
                                    detail=attempts[0]["name"] + ": " + payload["detail"])
                time.sleep(.2)
                continue
            if task["state"] == "pending" and time.monotonic() - offered < OFFER_SECONDS and self.store.available(kind):
                report_work("dispatch", "Assigning a processing helper", estimate=False)
                time.sleep(.1)
                continue
            report_work("local-wait-" + task_id, "Waiting for the server CPU", detail=task.get("error") or "", estimate=False)
            # Unclaimed work stays claimable while the CPU is occupied. A helper
            # arriving now can pick it up; this loop also notices its completion
            # without waiting for an unrelated local operation to finish.
            with try_cpu() as taken:
                if not taken:
                    continue
                check_cancelled()
                attempt = self.store.start_local(task_id)
                if attempt is None:
                    continue
                started = time.monotonic()
                try:
                    # Keep local progress in both the build projection and the
                    # worker view without losing a parent asset group's context.
                    from .work_progress import _reporter
                    outer = _reporter.get()

                    def progress(payload, attempt=attempt, outer=outer):
                        self.store.progress("server", attempt, payload)
                        if outer:
                            outer.report(payload["phase"], payload["label"], completed=payload["completed"],
                                         total=payload["total"], unit=payload["unit"], detail=payload["detail"])

                    with track_work(progress):
                        result = function()
                    check_cancelled()
                    value = None if isinstance(result, Path) else result
                    metrics = {"processing_seconds": time.monotonic() - started,
                               "media_seconds": self.media_seconds(kind, inputs, value), "engine": "Local native tools"}
                    self.store.complete("server", attempt, value, metrics)
                    return result
                except BaseException:
                    with contextlib.suppress(WorkerConflict):
                        self.store.fail("server", attempt, "Local processing stopped or failed.")
                    raise

    @staticmethod
    def media_seconds(kind, inputs, result):
        if kind == "transcribe":
            import wave

            try:
                with wave.open(inputs["source"]["path"], "rb") as audio:
                    return audio.getnframes() / audio.getframerate()
            except (OSError, wave.Error, EOFError):
                return 0
        return 0

    def restore(self, task, output, arguments):
        attempt = self.catalog.rows("SELECT * FROM compute_attempts WHERE id=?", (task["accepted_attempt"],))[0]
        if output and attempt["worker_id"] != "server":
            source = Path(attempt["artifact"])
            if not source.is_file() or source.stat().st_size != attempt["artifact_bytes"] or digest_file(source) != attempt["artifact_hash"]:
                raise ValueError("An accepted worker artifact is missing or damaged; retry this lesson.")
            # The admission controller accounts for the second copy even when
            # work and site live on separate filesystems.
            from .storage import DEFAULT_BUFFER, StorageLocation, StorageManager

            output.parent.mkdir(parents=True, exist_ok=True)
            configured = self.catalog.rows("SELECT value FROM settings WHERE key='min_free_bytes'")
            buffer = int(configured[0]["value"]) if configured else DEFAULT_BUFFER
            storage = StorageManager(self.catalog.directory / "storage",
                                     [StorageLocation("generated", output.parent, buffer_bytes=buffer)])
            with storage.reserve("generated", output, source.stat().st_size) as upload, source.open("rb") as stream:
                while chunk := stream.read(1024**2):
                    check_cancelled()
                    upload.write(chunk)
                upload.commit()
        if output and not output.exists():
            raise ValueError("An accepted local output is missing; retry this lesson.")
        value = json.loads(task["result"])
        if task["kind"] in ("frame", "clip"):
            return Path(arguments[OPERATIONS[task["kind"]]["output"]])
        return value
