"""Single-host DBOS workflows backed by SQLite, with immutable media revisions."""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from dbos import DBOS, SetWorkflowID

from . import render, whisper
from .catalog import PIPELINE_VERSION, Catalog
from .llm import LLMError, make_backend
from .pipeline import BuildOptions, process_video
from .util import (
    BuildCancelled,
    atomic_write,
    digest_file,
    digest_json,
    file_lock,
    log,
    process_control,
    warn,
)

QUEUE = "lessons"
_stopping = threading.Event()


class WorkerStopping(BaseException):
    """Interrupt a step without recording a terminal workflow error."""


def serialize_options(options: BuildOptions) -> dict:
    result = asdict(options)
    result["out"] = str(options.out.resolve())
    result["work"] = str((options.work or options.out / ".work").resolve())
    result["force"] = sorted(options.force)
    return result


def deserialize_options(data: dict) -> BuildOptions:
    return BuildOptions(
        **{
            **data,
            "out": Path(data["out"]),
            "work": Path(data["work"]),
            "force": set(data["force"]),
        }
    )


def check_current(catalog: Catalog, build_id: str):
    if not catalog.is_current(build_id):
        raise BuildCancelled("lesson was cancelled, removed, or replaced")


def prepare_source(spec: dict) -> dict:
    catalog = Catalog(Path(spec["catalog"]))
    check_current(catalog, spec["build_id"])
    # Resolve the current location: a rename need not invalidate a source revision.
    row = catalog.rows("SELECT path FROM lessons WHERE id=?", (spec["lesson_id"],))[0]
    video = Path(spec["library"]) / row["path"]
    if not video.resolve().is_relative_to(Path(spec["library"])):
        raise ValueError("source is outside the library")
    options = deserialize_options(spec["options"])
    snapshot = (
        options.out / "_sources" / spec["digest"] / ("source" + video.suffix.lower())
    )
    if not snapshot.exists():
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".incoming-", dir=snapshot.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            shutil.copy2(video, temporary)
            if digest_file(temporary) != spec["digest"]:
                raise ValueError("source changed while taking its processing snapshot")
            temporary.chmod(0o644)
            temporary.replace(snapshot)
        finally:
            temporary.unlink(missing_ok=True)
    # Cache paths follow identity; copying an old stage cache is a one-time import.
    work = options.work / "lessons" / spec["lesson_id"] / spec["digest"]
    legacy = options.work / spec["legacy_course"] / spec["legacy_lesson"]
    if not work.exists() and legacy.is_dir():
        shutil.copytree(legacy, work)
    model_path = whisper.resolve_model(options.model)
    backend = (
        None
        if options.llm == "heuristic"
        else make_backend(
            options.llm, options.llm_model, fallbacks=options.llm_fallbacks
        )
    )
    return {
        "video": str(snapshot),
        "work": str(work),
        "source_name": video.name,
        "model_path": str(model_path),
        "backend": backend.name if backend else "heuristic",
        "model": getattr(backend, "model", None),
    }


class LazyBackend:
    def __init__(self, name, model, fallbacks):
        self.name, self.model, self.fallbacks = name, model, fallbacks

    def complete(self, system, user):
        return make_backend(self.name, self.model, fallbacks=self.fallbacks).complete(
            system, user
        )


def refresh_site(catalog: Catalog, out: Path, *, markdown=True):
    """Materialize catalog state. Caller holds the catalog's publication lock."""
    courses = catalog.published_courses()
    render.write_site(out, courses, write_markdown=markdown)
    # Only remove pages previously owned by this catalog. Legacy pages and
    # immutable revisions are retained for open readers and manual migration.
    manifest = out / ".catalog-pages.json"
    try:
        previous = set(json.loads(manifest.read_text()))
    except (OSError, ValueError):
        previous = set()
    current = {f"{course['slug']}/index.html" for course in courses}
    current |= {
        f"{course['slug']}/{lesson['slug']}.html"
        for course in courses
        for lesson in course["lessons"]
    }
    for name in previous - current:
        target = out / name
        if target.resolve().is_relative_to(out.resolve()):
            target.unlink(missing_ok=True)
    atomic_write(manifest, json.dumps(sorted(current)))
    with catalog.connect() as db:
        previous_hash = db.execute(
            "SELECT value FROM settings WHERE key='publication_hash'"
        ).fetchone()
        current_hash = digest_json(courses)
        if previous_hash is None or previous_hash[0] != current_hash:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('publication_hash',?)",
                (current_hash,),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('publication_at',?)",
                (str(time.time()),),
            )


def publish(spec: dict, lesson: dict, source: dict) -> str:
    catalog = Catalog(Path(spec["catalog"]))
    options = deserialize_options(spec["options"])
    staging = options.out / ".staging" / spec["build_id"]
    revision = options.out / "_revisions" / spec["build_id"]
    record = copy.deepcopy(lesson)
    prefix = f"../_revisions/{spec['build_id']}/"
    for step in record["steps"]:
        for frame in step.get("frames", []):
            frame["src"] = prefix + frame["src"]
        if step.get("frame"):
            step["frame"] = prefix + step["frame"]
        if step.get("clip"):
            step["clip"]["src"] = prefix + step["clip"]["src"]
    if record.get("poster"):
        record["poster"] = prefix + record["poster"]
    record["video_href"] = (
        ("../" + str(Path(source["video"]).relative_to(options.out)))
        if options.videos != "none"
        else None
    )
    record.update(
        id=spec["lesson_id"],
        revision=spec["build_id"],
        source_name=source["source_name"],
    )
    content = [
        {key: step.get(key) for key in ("start", "end", "title", "actions")}
        for step in record["steps"]
    ]
    record["reading_key"] = spec["lesson_id"] + ":" + digest_json(content)[:16]
    with catalog.lock():
        check_current(catalog, spec["build_id"])
        if not revision.exists():
            staging.mkdir(parents=True, exist_ok=True)
            # Fail before switching any published reference if an asset is absent.
            for step in lesson["steps"]:
                for entry in step.get("frames", []) + (
                    [step["clip"]] if step.get("clip") else []
                ):
                    target = staging / entry["src"]
                    if not target.is_file() or target.stat().st_size == 0:
                        raise ValueError(f"missing generated asset: {entry['src']}")
            revision.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(revision)
        catalog.accept(spec["build_id"], record)
        refresh_site(catalog, options.out, markdown=options.markdown)
        catalog.update_build(spec["build_id"], "ready", stage="publish")
    return spec["build_id"]


def retryable(exc: BaseException) -> bool:
    # Bad inputs and failed local tools require intervention, not an endless loop.
    return isinstance(exc, (ConnectionError, TimeoutError)) or (
        isinstance(exc, LLMError) and getattr(exc, "retryable", False)
    )


@DBOS.workflow(name="v2w.lesson.v1", max_recovery_attempts=3)
def lesson_workflow(spec: dict) -> str:
    def stage(name, function):
        def execute():
            stopping = _stopping
            catalog = Catalog(Path(spec["catalog"]))

            def check():
                if stopping.is_set():
                    raise WorkerStopping()
                check_current(catalog, spec["build_id"])

            check()
            catalog.update_build(spec["build_id"], "running", stage=name)
            try:
                with process_control(check):
                    result = function()
                    check()
                    return result
            except SystemExit as exc:
                message = "processing tool or model configuration is unavailable; see worker logs"
                catalog.update_build(
                    spec["build_id"], "running", stage=name, error=message
                )
                raise RuntimeError(message) from exc
            except Exception as exc:
                catalog.update_build(
                    spec["build_id"], "running", stage=name, error=str(exc)
                )
                raise

        return DBOS.run_step(
            {
                "name": name,
                "retries_allowed": True,
                "max_attempts": 3,
                "interval_seconds": 5,
                "backoff_rate": 2,
                "should_retry": retryable,
            },
            execute,
        )

    source = stage("prepare", lambda: prepare_source(spec))
    options = deserialize_options(spec["options"])
    options.videos = "none"  # The published player uses the immutable source snapshot.
    backend = (
        None
        if source["backend"] == "heuristic"
        else LazyBackend(source["backend"], source["model"], options.llm_fallbacks)
    )
    lesson = process_video(
        Path(source["video"]),
        work_dir=Path(source["work"]),
        course_dir=options.out / ".staging" / spec["build_id"],
        slug=spec["slug"],
        options=options,
        backend=backend,
        model_path=Path(source["model_path"]),
        run_stage=stage,
        source_name=source["source_name"],
    )
    if lesson is None:
        raise ValueError("video has no usable audio, transcript, or lesson steps")
    return stage("publish", lambda: publish(spec, lesson, source))


class DurableWorker:
    """Owns one local executor. Polling discovers imports; DBOS executes jobs."""

    def __init__(self, library: Path, options: BuildOptions, state: Path):
        if options.stop_after:
            raise ValueError(
                "--stop-after is only supported by the standalone build command"
            )
        self.library = library.resolve()
        if options.out.resolve().is_relative_to(self.library):
            raise ValueError("the site directory must be outside the watched library")
        self.options = options
        self.catalog = Catalog(state)
        self.serialized = serialize_options(options)
        self.seen = self.reconciled = None
        self.last_publication = None

    def __enter__(self):
        global _stopping
        self.library.mkdir(parents=True, exist_ok=True)
        self.lock = file_lock(self.catalog.directory / "worker.lock", blocking=False)
        self.lock.__enter__()
        self.stopping = _stopping = threading.Event()
        try:
            DBOS(
                config={
                    "name": "video-to-website",
                    "application_version": PIPELINE_VERSION,
                    "system_database_url": "sqlite:///"
                    + str(self.catalog.directory / "workflows.sqlite"),
                    "executor_id": "local",
                    "enable_otlp": False,
                    "log_level": "WARNING",
                }
            )
            DBOS.launch()
            DBOS.register_queue(QUEUE, worker_concurrency=1, polling_interval_sec=0.2)
            # Repair a crash between the catalog commit and writing public pages.
            if self.catalog.rows("SELECT 1 FROM lessons LIMIT 1"):
                with self.catalog.lock():
                    refresh_site(
                        self.catalog, self.options.out, markdown=self.options.markdown
                    )
            elif not (self.options.out / "index.html").exists():
                render.write_placeholder(
                    self.options.out, "Add videos to start your library."
                )
            return self
        except BaseException:
            self.lock.__exit__(None, None, None)
            raise

    def tick(self, *, scan=True):
        from .cli import _library_state

        if scan:
            state = _library_state(self.library)
            if state == self.seen and state != self.reconciled:
                self.catalog.reconcile(self.library, self.serialized)
                self.reconciled = state
            self.seen = state
        for build in self.catalog.rows(
            "SELECT * FROM builds WHERE state IN ('queued','running','cancelled','superseded') ORDER BY created,id"
        ):
            status = DBOS.get_workflow_status(build["id"])
            if build["state"] in ("cancelled", "superseded"):
                if status and status.status in ("ENQUEUED", "PENDING"):
                    DBOS.cancel_workflow(build["id"])
                continue
            if status is None:
                with SetWorkflowID(build["id"]):
                    DBOS.enqueue_workflow(
                        QUEUE, lesson_workflow, json.loads(build["spec"])
                    )
            elif status.status in ("ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"):
                detail = build["error"] or str(
                    status.error or "processing failed; retry this lesson"
                )
                self.catalog.update_build(build["id"], "failed", error=detail)
            elif status.app_version != PIPELINE_VERSION:
                self.catalog.update_build(
                    build["id"],
                    "failed",
                    error="workflow belongs to an older pipeline version; retry to rebuild with this version",
                )
            elif status.status == "CANCELLED":
                self.catalog.update_build(build["id"], "cancelled")
        # Metadata changes and deletions are published even when there are no jobs.
        with self.catalog.lock():
            courses = self.catalog.published_courses()
            publication = digest_json(courses)
            if publication != self.last_publication and self.reconciled is not None:
                refresh_site(
                    self.catalog, self.options.out, markdown=self.options.markdown
                )
                self.last_publication = publication
            atomic_write(
                self.options.out / "status.json", json.dumps(self.catalog.status())
            )

    def __exit__(self, *exc):
        self.stopping.set()
        try:
            DBOS.destroy(workflow_completion_timeout_sec=5)
        finally:
            self.lock.__exit__(*exc)


def watch(
    library: Path,
    options: BuildOptions,
    state: Path,
    *,
    interval: float,
    api_port=None,
    api_bind="127.0.0.1",
):
    with DurableWorker(library, options, state) as worker:
        if api_port:
            from .cli import _start_api

            _start_api(
                library.resolve(), api_bind, api_port, options.work, worker.catalog
            )
        log(f"watching {library}; SQLite state in {state}; DBOS lesson worker ready")
        next_scan = 0
        while True:
            try:
                scan = time.monotonic() >= next_scan
                worker.tick(scan=scan)
                if scan:
                    next_scan = time.monotonic() + interval
            except Exception as exc:
                warn(f"library reconciliation failed: {exc}")
            time.sleep(min(1.0, interval))
