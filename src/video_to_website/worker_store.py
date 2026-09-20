"""Server-side task ownership. All transitions use short SQLite transactions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import secrets
import time
import uuid
from pathlib import Path

from .worker_protocol import HEARTBEAT_SECONDS, KINDS, LEASE_SECONDS, OFFLINE_SECONDS, PROTOCOL, TASK_TIMEOUT

SCHEMA = """
CREATE TABLE workers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT,
    platform TEXT NOT NULL DEFAULT '', capabilities TEXT NOT NULL DEFAULT '[]',
    details TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL, last_seen REAL,
    paused INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0,
    cooldown_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE worker_pairing (
    code_hash TEXT PRIMARY KEY, expires REAL NOT NULL, worker_id TEXT
);
CREATE TABLE compute_tasks (
    id TEXT PRIMARY KEY, build_id TEXT NOT NULL REFERENCES builds(id),
    stage TEXT NOT NULL, kind TEXT NOT NULL, spec TEXT NOT NULL,
    state TEXT NOT NULL, current_attempt TEXT, accepted_attempt TEXT,
    result TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX compute_tasks_build ON compute_tasks(build_id, updated);
CREATE INDEX compute_tasks_state ON compute_tasks(state, created);
CREATE TABLE compute_attempts (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES compute_tasks(id),
    worker_id TEXT NOT NULL REFERENCES workers(id), state TEXT NOT NULL,
    started REAL NOT NULL, updated REAL NOT NULL, finished REAL,
    lease_until REAL, progress TEXT, error TEXT, metrics TEXT NOT NULL DEFAULT '{}',
    artifact TEXT, artifact_hash TEXT, artifact_bytes INTEGER
);
CREATE INDEX compute_attempts_worker ON compute_attempts(worker_id, started);
CREATE INDEX compute_attempts_task ON compute_attempts(task_id);
"""


class WorkerConflict(ValueError):
    """The task no longer belongs to this attempt."""


class WorkerUnauthorized(ValueError):
    pass


def secret_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def clean_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or any(ord(c) < 32 for c in value):
        raise ValueError("Use a worker name between 1 and 120 characters.")
    return value.strip()


class WorkerStore:
    def __init__(self, catalog, *, clock=time.time):
        self.catalog, self.clock = catalog, clock
        self.root = catalog.directory / "worker-results"

    @staticmethod
    def current(db, build_id):
        return db.execute("""SELECT 1 FROM lessons l JOIN builds b ON b.id=l.desired_build
            WHERE b.id=? AND l.deleted=0 AND b.state IN ('queued','running')""", (build_id,)).fetchone() is not None

    def server_heartbeat(self):
        now = self.clock()
        with self.catalog.connect() as db:
            db.execute("""INSERT INTO workers(id,name,platform,created,last_seen)
                VALUES('server','Library server','Local CPU',?,?)
                ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""", (now, now))

    def pairing(self):
        code = secrets.token_urlsafe(18)
        with self.catalog.connect() as db:
            db.execute("DELETE FROM worker_pairing WHERE expires<?", (self.clock(),))
            db.execute("INSERT INTO worker_pairing VALUES(?,?,NULL)", (secret_hash(code), self.clock() + 600))
        return {"code": code, "expires_in": 600}

    def pair(self, body):
        worker_id, token = body.get("id", ""), body.get("token", "")
        if len(worker_id) != 32 or any(c not in "0123456789abcdef" for c in worker_id) or not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ValueError("Invalid worker credentials.")
        name = clean_name(body.get("name"))
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM worker_pairing WHERE code_hash=? AND expires>?",
                             (secret_hash(str(body.get("code", ""))), self.clock())).fetchone()
            if row is None:
                raise WorkerUnauthorized("Pairing code expired or invalid. Create a new code on the Workers page.")
            existing = db.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
            if row["worker_id"]:
                if row["worker_id"] != worker_id or existing is None or existing["revoked"] or existing["token_hash"] != secret_hash(token):
                    raise WorkerUnauthorized("Pairing code has already been used.")
            elif existing:
                if existing["token_hash"] != secret_hash(token):
                    raise WorkerUnauthorized("The saved worker credential does not match this identity.")
                # A fresh code plus the original credential can reauthorize a
                # revoked/archived computer. Keep its name and contributions.
                if existing["revoked"] or existing["archived"]:
                    db.execute("""UPDATE workers SET revoked=0,archived=0,paused=0,
                        cooldown_until=0,last_seen=NULL WHERE id=?""", (worker_id,))
                db.execute("UPDATE worker_pairing SET worker_id=? WHERE code_hash=?", (worker_id, secret_hash(body["code"])))
            else:
                db.execute("INSERT INTO workers(id,name,token_hash,created) VALUES(?,?,?,?)",
                           (worker_id, name, secret_hash(token), self.clock()))
                db.execute("UPDATE worker_pairing SET worker_id=? WHERE code_hash=?", (worker_id, secret_hash(body["code"])))
        return {"id": worker_id, "protocol": PROTOCOL}

    def authenticate(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise WorkerUnauthorized("Worker credentials are required.")
        rows = self.catalog.rows("SELECT id FROM workers WHERE token_hash=? AND revoked=0", (secret_hash(token),))
        if not rows:
            raise WorkerUnauthorized("Worker credentials were revoked or are invalid.")
        return rows[0]["id"]

    def heartbeat(self, worker_id, body):
        if body.get("protocol") != PROTOCOL:
            raise WorkerConflict("Incompatible helper version. Install the same version as the server.")
        kinds = body.get("capabilities", [])
        if not isinstance(kinds, list) or any(kind not in KINDS for kind in kinds):
            raise ValueError("Invalid helper capabilities.")
        details = body.get("details", {})
        if not isinstance(details, dict) or len(json.dumps(details)) > 8192:
            raise ValueError("Invalid worker details.")
        now = self.clock()
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            worker = db.execute("SELECT * FROM workers WHERE id=? AND revoked=0", (worker_id,)).fetchone()
            if not worker:
                raise WorkerUnauthorized("Worker has been revoked.")
            db.execute("UPDATE workers SET last_seen=?,platform=?,capabilities=?,details=? WHERE id=?",
                       (now, str(body.get("platform", ""))[:120], json.dumps(kinds), json.dumps(details), worker_id))
            active = body.get("attempt")
            valid = False
            if active:
                try:
                    attempt, task = self._owned(db, worker_id, active)
                    if now - attempt["started"] >= TASK_TIMEOUT:
                        self._abandon(db, task, "lost", "Helper task exceeded its two-hour execution limit.")
                    else:
                        db.execute("UPDATE compute_attempts SET lease_until=?,updated=? WHERE id=?", (now + LEASE_SECONDS, now, active))
                        valid = True
                except WorkerConflict:
                    pass
        return {"paused": bool(worker["paused"]), "continue": valid,
                "lease_seconds": LEASE_SECONDS, "heartbeat_seconds": HEARTBEAT_SECONDS}

    def available(self, kind=None):
        enabled = self.catalog.rows("SELECT value FROM settings WHERE key='workers_enabled'")
        if enabled and enabled[0]["value"] == "0":
            return []
        rows = self.catalog.rows("""SELECT * FROM workers WHERE id!='server' AND revoked=0 AND paused=0
            AND last_seen>? AND cooldown_until<=?""", (self.clock() - OFFLINE_SECONDS, self.clock()))
        return [row for row in rows if json.loads(row["capabilities"]) and (kind is None or kind in json.loads(row["capabilities"]))]

    def ensure_task(self, task_id, build_id, stage, kind, spec):
        now = self.clock()
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.current(db, build_id):
                raise WorkerConflict("Build is no longer current.")
            db.execute("""INSERT OR IGNORE INTO compute_tasks
                (id,build_id,stage,kind,spec,state,created,updated) VALUES(?,?,?,?,?,'pending',?,?)""",
                       (task_id, build_id, stage, kind, json.dumps(spec), now, now))
            return dict(db.execute("SELECT * FROM compute_tasks WHERE id=?", (task_id,)).fetchone())

    def task(self, task_id):
        rows = self.catalog.rows("SELECT * FROM compute_tasks WHERE id=?", (task_id,))
        if not rows:
            raise KeyError(task_id)
        return rows[0]

    def _attempt(self, db, task, worker_id):
        now, attempt_id = self.clock(), uuid.uuid4().hex
        db.execute("""INSERT INTO compute_attempts(id,task_id,worker_id,state,started,updated,lease_until)
            VALUES(?,?,?,'running',?,?,?)""", (attempt_id, task["id"], worker_id, now, now,
                                             now + LEASE_SECONDS if worker_id != "server" else None))
        db.execute("UPDATE compute_tasks SET state='running',current_attempt=?,updated=? WHERE id=?",
                   (attempt_id, now, task["id"]))
        return attempt_id

    def claim(self, worker_id):
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            enabled = db.execute("SELECT value FROM settings WHERE key='workers_enabled'").fetchone()
            if enabled and enabled["value"] == "0":
                return None
            worker = db.execute("SELECT * FROM workers WHERE id=? AND revoked=0", (worker_id,)).fetchone()
            if not worker:
                raise WorkerUnauthorized("Worker has been revoked.")
            if worker["paused"] or (worker["last_seen"] or 0) <= self.clock() - OFFLINE_SECONDS or worker["cooldown_until"] > self.clock():
                return None
            if db.execute("SELECT 1 FROM compute_attempts WHERE worker_id=? AND state='running'", (worker_id,)).fetchone():
                return None
            kinds = json.loads(worker["capabilities"])
            for row in db.execute("SELECT * FROM compute_tasks WHERE state='pending' ORDER BY created,id").fetchall():
                if row["kind"] not in kinds or not self.current(db, row["build_id"]):
                    continue
                attempt_id = self._attempt(db, row, worker_id)
                spec = json.loads(row["spec"])
                inputs = {name: {key: value for key, value in item.items() if key != "path"}
                          for name, item in spec["inputs"].items()}
                return {"id": row["id"], "attempt": attempt_id, "kind": row["kind"],
                        "params": spec["params"], "inputs": inputs,
                        "output": spec.get("output") is not None, "lease_seconds": LEASE_SECONDS}
        return None

    def _owned(self, db, worker_id, attempt_id, *, accepted=False):
        attempt = db.execute("SELECT * FROM compute_attempts WHERE id=? AND worker_id=?", (attempt_id, worker_id)).fetchone()
        if attempt is None:
            raise WorkerConflict("Task assignment no longer belongs to this helper.")
        task = db.execute("SELECT * FROM compute_tasks WHERE id=?", (attempt["task_id"],)).fetchone()
        if accepted and attempt["state"] == "accepted" and task["accepted_attempt"] == attempt_id:
            return attempt, task
        if (attempt["state"] != "running" or task["current_attempt"] != attempt_id
                or (attempt["lease_until"] is not None and attempt["lease_until"] <= self.clock())
                or not self.current(db, task["build_id"])):
            raise WorkerConflict("Task was cancelled, reassigned, or its lease expired.")
        return attempt, task

    def owned(self, worker_id, attempt_id):
        with self.catalog.connect() as db:
            attempt, task = self._owned(db, worker_id, attempt_id)
            return dict(attempt), dict(task)

    def progress(self, worker_id, attempt_id, payload):
        if not isinstance(payload, dict) or len(json.dumps(payload, allow_nan=False)) > 16384:
            raise ValueError("Invalid task progress.")
        clean = {k: str(payload.get(k, ""))[:500] for k in ("phase", "label", "detail", "unit")}
        for key in ("completed", "total"):
            value = payload.get(key)
            clean[key] = value if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else None
        clean["updated"] = self.clock()
        eta = payload.get("eta_seconds")
        clean["eta_seconds"] = eta if isinstance(eta, (int, float)) and math.isfinite(eta) and 0 <= eta <= TASK_TIMEOUT else None
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, worker_id, attempt_id)
            db.execute("UPDATE compute_attempts SET progress=?,updated=? WHERE id=?",
                       (json.dumps(clean), self.clock(), attempt_id))

    def record_artifact(self, worker_id, attempt_id, path, digest, length):
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, worker_id, attempt_id)
            db.execute("UPDATE compute_attempts SET artifact=?,artifact_hash=?,artifact_bytes=? WHERE id=?",
                       (str(path), digest, length, attempt_id))

    def complete(self, worker_id, attempt_id, result, metrics=None):
        encoded = json.dumps(result, allow_nan=False)
        if len(encoded) > 16 * 1024**2:
            raise ValueError("Task result exceeds 16 MiB.")
        metrics = metrics or {}
        if not isinstance(metrics, dict):
            raise ValueError("Invalid task measurements.")
        clean = {}
        for key in ("processing_seconds", "transfer_bytes", "media_seconds"):
            value = metrics.get(key, 0)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1e12:
                raise ValueError("Invalid task measurements.")
            clean[key] = value
        clean["engine"] = str(metrics.get("engine", ""))[:160]
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            attempt, task = self._owned(db, worker_id, attempt_id, accepted=True)
            if attempt["state"] == "accepted":
                if task["result"] != encoded:
                    raise WorkerConflict("This attempt already completed with a different result.")
                return {"accepted": True}
            spec = json.loads(task["spec"])
            if spec.get("output") and worker_id != "server" and not attempt["artifact"]:
                raise ValueError("Upload the output artifact before completing the task.")
            validate_result(task["kind"], result)
            now = self.clock()
            db.execute("UPDATE compute_attempts SET state='accepted',finished=?,updated=?,metrics=? WHERE id=?",
                       (now, now, json.dumps(clean), attempt_id))
            db.execute("UPDATE compute_tasks SET state='accepted',accepted_attempt=?,result=?,updated=? WHERE id=?",
                       (attempt_id, encoded, now, task["id"]))
        return {"accepted": True}

    def _abandon(self, db, task, state, reason):
        now = self.clock()
        if task["current_attempt"]:
            db.execute("UPDATE compute_attempts SET state=?,error=?,finished=?,updated=? WHERE id=? AND state='running'",
                       (state, reason, now, now, task["current_attempt"]))
        db.execute("UPDATE compute_tasks SET state=?,error=?,current_attempt=NULL,updated=? WHERE id=?",
                   ("cancelled" if state == "cancelled" else "fallback", reason, now, task["id"]))

    def fail(self, worker_id, attempt_id, reason, *, category="worker"):
        if category not in ("worker", "server_storage"):
            raise ValueError("Unknown failure category.")
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, task = self._owned(db, worker_id, attempt_id)
            self._abandon(db, task, "failed", str(reason)[:1000])
            if category == "server_storage":
                db.execute("UPDATE compute_tasks SET state='failed' WHERE id=?", (task["id"],))
            else:
                db.execute("UPDATE workers SET cooldown_until=? WHERE id=?", (self.clock() + 60, worker_id))

    def expire(self, *, restart=False):
        now = self.clock()
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT t.*,a.worker_id,a.lease_until,a.started FROM compute_tasks t
                LEFT JOIN compute_attempts a ON a.id=t.current_attempt
                WHERE t.state IN ('running','pending','fallback')""").fetchall()
            for task in rows:
                if not self.current(db, task["build_id"]):
                    self._abandon(db, task, "cancelled", "Lesson was cancelled or replaced.")
                elif task["state"] == "running" and (
                    (task["worker_id"] == "server" and restart)
                    or (task["lease_until"] is not None and task["lease_until"] <= now)
                    or (task["worker_id"] != "server" and now - task["started"] >= TASK_TIMEOUT)
                ):
                    self._abandon(db, task, "lost", "Worker disconnected or stopped; returning this task to the server.")

    def start_local(self, task_id):
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM compute_tasks WHERE id=?", (task_id,)).fetchone()
            if not task or not self.current(db, task["build_id"]):
                raise WorkerConflict("Build is no longer current.")
            if task["state"] not in ("pending", "fallback"):
                return None
            return self._attempt(db, task, "server")

    def release_stage(self, build_id, stage):
        """Fence a failed batch's remaining assignments without losing results."""
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for task in db.execute("""SELECT * FROM compute_tasks WHERE build_id=? AND stage=?
                AND state IN ('pending','running','fallback')""", (build_id, stage)).fetchall():
                self._abandon(db, task, "lost" if self.current(db, build_id) else "cancelled",
                              "Media batch stopped; this unfinished operation will restart when the lesson resumes.")

    def prefer_local(self, task_id, reason):
        with self.catalog.connect() as db:
            db.execute("UPDATE compute_tasks SET state='fallback',error=?,updated=? WHERE id=? AND state='pending'",
                       (reason, self.clock(), task_id))

    @staticmethod
    def deletion_blocker(db, worker_id):
        # These execution records are also checkpoints. An offline computer may
        # have supplied a result that a running (or failed, still-current) lesson
        # will need on recovery. Removing its provenance must not destroy that.
        row = db.execute("""SELECT 1 FROM compute_attempts a
            JOIN compute_tasks t ON t.id=a.task_id JOIN builds b ON b.id=t.build_id
            JOIN lessons l ON l.desired_build=b.id
            WHERE a.worker_id=? AND l.deleted=0 AND b.state NOT IN ('ready','cancelled','superseded')
            AND (t.current_attempt=a.id OR t.accepted_attempt=a.id) LIMIT 1""", (worker_id,)).fetchone()
        return ("This worker has tasks or saved results needed by an unfinished lesson. "
                "Archive it now, or finish/retry that lesson before deleting its history.") if row else None

    def manage(self, worker_id, body):
        action = body.get("action")
        if worker_id == "server":
            raise ValueError("The library server remains available for local fallback.")
        artifacts = []
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            worker = db.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
            if not worker:
                raise KeyError(worker_id)
            if action == "rename":
                db.execute("UPDATE workers SET name=? WHERE id=?", (clean_name(body.get("name")), worker_id))
            elif action in ("pause", "resume"):
                if worker["revoked"]:
                    raise WorkerConflict("This worker has been revoked. Reconnect it with a fresh pairing code.")
                db.execute("UPDATE workers SET paused=?,cooldown_until=0 WHERE id=?", (int(action == "pause"), worker_id))
            elif action in ("stop", "revoke", "archive"):
                db.execute("UPDATE workers SET paused=1,revoked=?,archived=? WHERE id=?",
                           (int(action in ("revoke", "archive")) or worker["revoked"],
                            int(action == "archive") or worker["archived"], worker_id))
                for task in db.execute("""SELECT t.* FROM compute_tasks t JOIN compute_attempts a ON a.id=t.current_attempt
                    WHERE a.worker_id=? AND t.state='running'""", (worker_id,)).fetchall():
                    self._abandon(db, task, "lost", "Worker stopped by you; returning this task to the server.")
            elif action == "restore":
                # Unhiding history must not silently restore a revoked token.
                db.execute("UPDATE workers SET archived=0 WHERE id=?", (worker_id,))
            elif action == "delete":
                reason = self.deletion_blocker(db, worker_id)
                if reason:
                    raise WorkerConflict(reason)
                artifacts = [row["artifact"] for row in db.execute(
                    "SELECT artifact FROM compute_attempts WHERE worker_id=? AND artifact IS NOT NULL", (worker_id,))]
                # Logical task results belong to builds, and may have attempts
                # from several workers. Remove this worker's history only.
                db.execute("""UPDATE compute_tasks SET current_attempt=NULL WHERE current_attempt IN
                    (SELECT id FROM compute_attempts WHERE worker_id=?)""", (worker_id,))
                db.execute("""UPDATE compute_tasks SET accepted_attempt=NULL WHERE accepted_attempt IN
                    (SELECT id FROM compute_attempts WHERE worker_id=?)""", (worker_id,))
                db.execute("DELETE FROM compute_attempts WHERE worker_id=?", (worker_id,))
                db.execute("DELETE FROM worker_pairing WHERE worker_id=?", (worker_id,))
                db.execute("DELETE FROM workers WHERE id=?", (worker_id,))
            else:
                raise ValueError("Unknown worker action.")
        for artifact in artifacts:
            # These are transfer copies, never the published lesson media. A
            # cleanup failure leaves an orphan for the existing maintenance pass.
            with contextlib.suppress(OSError):
                path = Path(artifact)
                if path.resolve().is_relative_to(self.root.resolve()):
                    path.unlink(missing_ok=True)

    def overview(self):
        now = self.clock()
        workers = self.catalog.rows("SELECT * FROM workers ORDER BY id='server' DESC,created,id")
        for worker in workers:
            del worker["token_hash"]
            worker["capabilities"] = json.loads(worker["capabilities"])
            worker["details"] = json.loads(worker["details"])
            worker["online"] = not worker["revoked"] and worker["last_seen"] is not None and now - worker["last_seen"] < OFFLINE_SECONDS
            attempts = self.catalog.rows("""SELECT a.*,t.kind,t.stage,t.build_id,t.spec,t.state AS task_state,
                t.error AS fallback_reason,l.id AS lesson_id,l.title,l.path AS source_path,c.title AS course
                FROM compute_attempts a JOIN compute_tasks t ON t.id=a.task_id
                JOIN builds b ON b.id=t.build_id JOIN lessons l ON l.id=b.lesson_id JOIN courses c ON c.id=l.course_id
                WHERE a.worker_id=? ORDER BY a.state='running' DESC,a.started DESC LIMIT 13""", (worker["id"],))
            totals = self.catalog.rows("""SELECT
                COALESCE(SUM(a.state='accepted'),0) AS accepted_tasks,
                COALESCE(SUM(CASE WHEN a.state='accepted' THEN json_extract(a.metrics,'$.processing_seconds') ELSE 0 END),0) AS processing_seconds,
                COALESCE(SUM(CASE WHEN a.state='accepted' AND t.kind='transcribe' THEN json_extract(a.metrics,'$.media_seconds') ELSE 0 END),0) AS media_seconds,
                COALESCE(SUM(a.state IN ('lost','failed','cancelled')),0) AS interrupted_tasks
                FROM compute_attempts a JOIN compute_tasks t ON t.id=a.task_id WHERE a.worker_id=?""", (worker["id"],))[0]
            active, recent = [], []
            for attempt in attempts:
                metrics = json.loads(attempt["metrics"])
                item = {k: attempt[k] for k in ("id", "task_id", "kind", "stage", "build_id", "lesson_id", "title", "course", "state", "started", "finished", "error", "fallback_reason")}
                item["title"] = attempt["title"] or Path(attempt["source_path"]).stem
                item["description"] = json.loads(attempt["spec"]).get("description", "")
                item["progress"] = json.loads(attempt["progress"]) if attempt["progress"] else None
                item["metrics"] = metrics
                if attempt["state"] == "running":
                    active.append(item)
                elif len(recent) < 12:
                    recent.append(item)
            worker.update(totals=totals, active=active, recent=recent)
            with self.catalog.connect() as db:
                worker["deletion_blocked_reason"] = self.deletion_blocker(db, worker["id"]) if worker["id"] != "server" else "The library server cannot be removed."
            worker["status"] = ("archived" if worker["archived"] else "revoked" if worker["revoked"] else "offline" if not worker["online"] else
                                "draining" if worker["paused"] and active else "paused" if worker["paused"] else
                                "busy" if active else "cooldown" if worker["cooldown_until"] > now else "available")
        return {"updated": now, "protocol": PROTOCOL, "workers": workers}

    def cleanup(self):
        """Keep active/replayable outputs; discard abandoned and terminal copies."""
        now = self.clock()
        rows = self.catalog.rows("""SELECT a.id,a.artifact,a.state,a.finished,b.state AS build_state
            FROM compute_attempts a JOIN compute_tasks t ON t.id=a.task_id JOIN builds b ON b.id=t.build_id
            WHERE a.artifact IS NOT NULL""")
        for row in rows:
            terminal = row["build_state"] in ("ready", "cancelled", "superseded", "failed")
            if row["state"] in ("lost", "failed", "cancelled") or (terminal and (row["finished"] or now) < now - 86400):
                path = Path(row["artifact"])
                if path.resolve().is_relative_to(self.root.resolve()):
                    path.unlink(missing_ok=True)
                with self.catalog.connect() as db:
                    db.execute("UPDATE compute_attempts SET artifact=NULL WHERE id=?", (row["id"],))
        referenced = {row["artifact"] for row in self.catalog.rows("SELECT artifact FROM compute_attempts WHERE artifact IS NOT NULL")}
        # Uploaded retries and a crash before recording the artifact leave
        # unreferenced files. Never touch a live StorageManager partial file.
        for path in self.root.glob("*/*/*"):
            if path.is_file() and not path.name.startswith(".") and str(path) not in referenced and path.stat().st_mtime < now - 3600:
                path.unlink(missing_ok=True)


def validate_result(kind, value):
    if kind in ("frame", "clip", "audio"):
        if value is not None:
            raise ValueError("An artifact task must return a null value.")
    elif kind == "frame_hash":
        if value is not None and (not isinstance(value, int) or not 0 <= value < 2**256):
            raise ValueError("Invalid frame hash.")
    elif kind == "transcribe":
        if not isinstance(value, list):
            raise ValueError("Invalid transcript.")
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ValueError("Invalid transcript segment.")
            start, end = item.get("start"), item.get("end")
            if not all(isinstance(t, (int, float)) and math.isfinite(t) for t in (start, end)) or not 0 <= start <= end:
                raise ValueError("Invalid transcript timestamps.")
    elif kind in ("scenes", "activity"):
        if not isinstance(value, list) or any(not isinstance(p, (list, tuple)) or len(p) != 2 or
            not all(isinstance(t, (int, float)) and math.isfinite(t) and t >= 0 for t in p) for p in value):
            raise ValueError("Invalid scene measurements.")
