"""Persistent provider pauses without changing catalog/workflow schemas."""

from __future__ import annotations

import json
import re
import time
import uuid

PAUSE_PREFIX = "llm-pause:"
WAIT_PREFIX = "llm-wait:"
PROVIDER_NAMES = {
    "anthropic": "Anthropic API", "claude-cli": "Claude CLI",
    "codex-cli": "Codex CLI", "ollama": "Ollama",
}


class PauseConflict(ValueError):
    pass


class ProviderPauses:
    def __init__(self, catalog):
        self.catalog = catalog

    @staticmethod
    def _provider(provider):
        if not isinstance(provider, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", provider):
            raise ValueError("Invalid provider.")
        return provider

    def get(self, provider):
        key = PAUSE_PREFIX + self._provider(provider)
        rows = self.catalog.rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else None

    def active(self):
        return {row["key"][len(PAUSE_PREFIX):]: json.loads(row["value"])
                for row in self.catalog.rows("SELECT key,value FROM settings WHERE key LIKE ?", (PAUSE_PREFIX + "%",))}

    def waiting(self):
        return {row["key"][len(WAIT_PREFIX):]: json.loads(row["value"])
                for row in self.catalog.rows("SELECT key,value FROM settings WHERE key LIKE ?", (WAIT_PREFIX + "%",))}

    def pause(self, provider, reason, message):
        provider = self._provider(provider)
        key = PAUSE_PREFIX + provider
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if previous:
                return json.loads(previous[0])
            record = dict(id=uuid.uuid4().hex, provider=provider,
                          name=PROVIDER_NAMES.get(provider, provider), reason=reason,
                          message=str(message)[:500], paused_at=time.time())
            db.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, json.dumps(record)))
            return record

    def resume(self, provider, pause_id):
        key = PAUSE_PREFIX + self._provider(provider)
        if not isinstance(pause_id, str) or not re.fullmatch(r"[a-f0-9]{32}", pause_id):
            raise ValueError("Expected the current pause ID.")
        with self.catalog.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if not row:
                return False
            if json.loads(row[0])["id"] != pause_id:
                raise PauseConflict("The provider paused again. Refresh its status before resuming.")
            db.execute("DELETE FROM settings WHERE key=?", (key,))
            return True

    def mark_waiting(self, build_id, provider):
        with self.catalog.connect() as db:
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                       (WAIT_PREFIX + build_id, json.dumps(dict(provider=provider, since=time.time()))))

    def clear_waiting(self, build_id):
        with self.catalog.connect() as db:
            db.execute("DELETE FROM settings WHERE key=?", (WAIT_PREFIX + build_id,))

    def prune(self):
        with self.catalog.connect() as db:
            db.execute("""DELETE FROM settings WHERE key LIKE ? AND substr(key,?) NOT IN
                       (SELECT id FROM builds WHERE state IN ('queued','running'))""",
                       (WAIT_PREFIX + "%", len(WAIT_PREFIX) + 1))
