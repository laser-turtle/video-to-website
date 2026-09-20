"""Bounded media fan-out within one existing DBOS stage.

The parent alone reports lesson progress and assembles results. Child threads
inherit only native execution/cancellation settings, never DBOS workflow context.
"""

from __future__ import annotations

import threading
import sqlite3
import contextvars
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable

from .compute import compute_with, current_executor
from .util import _process_check, process_control, warn
from .work_progress import report_work, track_work, work_group

MEDIA_LOOKAHEAD = 8


@dataclass(frozen=True)
class MediaJob:
    key: tuple[int, str]
    label: str
    function: Callable


class BatchAborted(BaseException):
    """A sibling failed; the parent preserves and raises the original error."""


def run_media_jobs(jobs: list[MediaJob]) -> dict:
    if not jobs:
        report_work("assets", "No lesson media requested", completed=1, total=1, estimate=False)
        return {}
    executor = current_executor()
    results = {}
    if executor is None:
        # A standalone build retains its original sequential native execution.
        for job in jobs:
            with work_group("assets", "Creating lesson media", completed=len(results), total=len(jobs),
                            unit="media jobs", detail=job.label):
                results[job.key] = job.function()
        report_work("assets", "Creating lesson media", completed=len(jobs), total=len(jobs), unit="media jobs")
        return results

    parent_check = _process_check.get()
    aborted = threading.Event()
    guard = threading.Lock()
    first_error = []
    latest = {}

    def check():
        if parent_check:
            parent_check()
        if aborted.is_set():
            raise BatchAborted()

    def invoke(job):
        def update(payload):
            with guard:
                latest[job.key] = payload

        try:
            # New threads start with an empty Context. In particular, do not
            # copy DBOS's step counter or share the parent's progress reporter.
            with process_control(check), compute_with(executor, description=job.label), track_work(update):
                check()
                return job.function()
        except BatchAborted:
            raise
        except BaseException as exc:
            with guard:
                if not first_error:
                    first_error.append(exc)
            aborted.set()
            raise

    pending = {}
    next_job = 0
    pool = ThreadPoolExecutor(max_workers=MEDIA_LOOKAHEAD, thread_name_prefix="v2w-media")
    failure = None
    try:
        while pending or next_job < len(jobs):
            with guard:
                error = first_error[0] if first_error else None
            if error is not None:
                raise error
            if parent_check:
                parent_check()
            # With no media helpers, only one job is admitted. Check again
            # while waiting so a newly connected helper can expand the window.
            limit = executor.media_window(MEDIA_LOOKAHEAD)
            while len(pending) < limit and next_job < len(jobs) and not aborted.is_set():
                job = jobs[next_job]
                pending[pool.submit(contextvars.Context().run, invoke, job)] = job
                next_job += 1
            done, _ = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
            with guard:
                error = first_error[0] if first_error else None
            if error is not None:
                raise error
            for future in done:
                job = pending.pop(future)
                results[job.key] = future.result()
                with guard:
                    latest.pop(job.key, None)
            with guard:
                updates = dict(latest)
            descriptions = []
            for job in pending.values():
                update = updates.get(job.key, {})
                detail = update.get("label", "Waiting for a processor")
                if update.get("fraction") is not None:
                    detail += f" ({round(update['fraction'] * 100)}%)"
                descriptions.append(f"{job.label}: {detail}")
            report_work("assets", "Creating lesson media", completed=len(results), total=len(jobs), unit="media jobs",
                        detail=f"{len(results)} of {len(jobs)} media jobs complete · {len(pending)} in progress or waiting"
                        + (" · " + "; ".join(descriptions[:3]) if descriptions else ""))
        return results
    except BaseException as exc:
        failure = exc
        raise
    finally:
        aborted.set()
        pool.shutdown(wait=True, cancel_futures=True)
        if isinstance(failure, (Exception, SystemExit)):
            # Completed native results survive. Stop only outstanding work on a
            # failed/cancelled stage. A coordinator shutdown (WorkerStopping,
            # a BaseException) retains live remote assignments for recovery.
            try:
                executor.release_media_batch()
            except (OSError, sqlite3.Error) as exc:
                warn(f"Could not release interrupted media assignments: {exc}")
