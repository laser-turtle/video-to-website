"""Live measurements inside a stage, independent of DBOS checkpoints.

Tools report actual work completed. The reporter throttles persistence and only
estimates remaining time after observing forward progress over enough time.
"""

from __future__ import annotations

import contextlib
import contextvars
import math
import sqlite3
import time
from collections.abc import Callable

from .util import warn

_reporter = contextvars.ContextVar("v2w_work_reporter", default=None)
_group = contextvars.ContextVar("v2w_progress_group", default=None)


class WorkProgress:
    def __init__(
        self, sink: Callable[[dict], None], *, clock=time.time, interval: float = 1.0
    ):
        self.sink = sink
        self.clock = clock
        self.interval = interval
        self.started = clock()
        self.phase_started = self.started
        self.phase = None
        self.last_write = float("-inf")
        self.last_completed = 0.0
        self.advances = 0
        self.warning_sent = False
        self.latest = {}

    def report(
        self,
        phase: str,
        label: str,
        *,
        completed=None,
        total=None,
        unit=None,
        detail="",
        estimate=True,
    ):
        now = self.clock()
        changed = phase != self.phase
        if changed:
            self.phase = phase
            self.phase_started = now
            self.last_completed = 0.0
            self.advances = 0
        valid = (
            isinstance(completed, (int, float))
            and isinstance(total, (int, float))
            and math.isfinite(completed)
            and math.isfinite(total)
            and total > 0
        )
        fraction = None
        eta = None
        if valid:
            completed = min(total, max(self.last_completed, 0.0, completed))
            if completed > self.last_completed:
                self.advances += 1
            self.last_completed = completed
            fraction = completed / total
            elapsed = max(0, now - self.phase_started)
            if estimate and self.advances >= 2 and elapsed >= 5 and 0 < fraction < 1:
                remaining = elapsed * (1 - fraction) / fraction
                eta = max(1, math.ceil(remaining)) if math.isfinite(remaining) else None
        else:
            completed = total = None
        previous_fraction = self.latest.get("fraction")
        self.latest = {
            "phase": phase,
            "label": label,
            "detail": detail,
            "completed": completed,
            "total": total,
            "unit": unit,
            "fraction": fraction,
            "eta_seconds": eta,
            "updated": now,
            "phase_started": self.phase_started,
            "stage_started": self.started,
        }
        terminal = fraction == 1 and previous_fraction != 1
        if changed or terminal or now - self.last_write >= self.interval:
            self.last_write = now
            try:
                self.sink(dict(self.latest))
            except (OSError, sqlite3.Error) as exc:
                # Progress is telemetry; a failed progress write must not kill
                # an expensive media operation that can still finish normally.
                if not self.warning_sent:
                    warn(f"could not save live progress: {exc}")
                    self.warning_sent = True


@contextlib.contextmanager
def track_work(sink: Callable[[dict], None]):
    reporter = WorkProgress(sink)
    token = _reporter.set(reporter)
    try:
        yield reporter
    finally:
        _reporter.reset(token)


@contextlib.contextmanager
def work_group(phase, label, *, completed, total, unit, detail=""):
    """Sub-operations provide detail without resetting the batch's progress."""
    group = dict(
        phase=phase,
        label=label,
        completed=completed,
        total=total,
        unit=unit,
        detail=detail,
    )
    report_work(**group)
    token = _group.set(group)
    try:
        yield
    finally:
        _group.reset(token)


def report_work(
    phase: str,
    label: str,
    *,
    completed=None,
    total=None,
    unit=None,
    detail="",
    estimate=True,
):
    reporter = _reporter.get()
    if reporter is None:
        return
    group = _group.get()
    if group is not None:
        child = label
        if (
            isinstance(completed, (int, float))
            and isinstance(total, (int, float))
            and math.isfinite(completed)
            and math.isfinite(total)
            and total > 0
        ):
            child += f" · {min(100, max(0, int(100 * completed / total)))}%"
        message = " — ".join(part for part in (group["detail"], child, detail) if part)
        reporter.report(**{**group, "detail": message})
    else:
        reporter.report(
            phase,
            label,
            completed=completed,
            total=total,
            unit=unit,
            detail=detail,
            estimate=estimate,
        )


def progress_snapshot(payload: dict, *, now: float | None = None) -> dict:
    result = dict(payload)
    now = time.time() if now is None else now
    age = max(0, now - result["updated"])
    result["age_seconds"] = round(age)
    result["stale"] = age > 120
    if result["stale"]:
        result["eta_seconds"] = None
    return result
