"""Materialize catalog metadata independently of media processing."""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import render
from .catalog import Catalog
from .util import atomic_write, digest_json


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
