"""Accepted LLM section results, independent of DBOS step numbering."""

from __future__ import annotations

import json
from pathlib import Path

from .util import atomic_write, digest_json


def request_key(backend, system, user, context):
    return digest_json({"version": 1, "backend": backend.name,
                        "model": getattr(backend, "model", None),
                        "fallbacks": getattr(backend, "fallbacks", None),
                        "system": system, "user": user, "context": context})


def valid_result(result):
    return isinstance(result, dict) and isinstance(result.get("steps"), list)


def read_section(directory: Path | None, key: str):
    if directory is None:
        return None
    try:
        payload = json.loads((directory / (key + ".json")).read_text())
        if (isinstance(payload, dict) and payload.get("version") == 1
                and payload.get("request") == key and valid_result(payload.get("result"))
                and payload.get("checksum") == digest_json(payload["result"])):
            return payload["result"]
    except (OSError, ValueError, TypeError):
        pass
    return None


def write_section(directory: Path | None, key: str, result):
    if directory is None or not valid_result(result):
        return
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(directory / (key + ".json"), json.dumps({
        "version": 1, "request": key, "result": result, "checksum": digest_json(result),
    }))
