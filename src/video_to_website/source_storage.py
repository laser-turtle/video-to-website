"""Remove redundant uploads while retaining an authoritative processing source."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .catalog import CatalogConflict
from .util import digest_file, natural_key


def published_snapshot(row: dict) -> Path | None:
    """Use the completed build's output root and original extension after renames."""
    published = json.loads(row["published"]) if row["published"] else {}
    if row["state"] != "ready" or published.get("revision") != row["desired_build"]:
        return None
    spec = json.loads(row["spec"])
    if spec["digest"] != row["digest"]:
        return None
    suffix = Path(published.get("source_name") or spec["path"]).suffix.lower()
    return Path(spec["options"]["out"]).resolve() / "_sources" / row["digest"] / ("source" + suffix)


SOURCE_ROWS = """SELECT l.*,b.state,b.spec,c.path AS course_path,c.title AS course_title,
    c.slug AS course_slug,r.snapshot AS retained_snapshot,r.build_id AS reclaimed_build
    FROM lessons l JOIN courses c ON c.id=l.course_id
    LEFT JOIN builds b ON b.id=l.desired_build
    LEFT JOIN reclaimed_sources r ON r.lesson_id=l.id WHERE l.deleted=0"""


def annotate_sources(catalog, library: Path, courses: list[dict]) -> list[dict]:
    """Cheap listing hints; destructive requests always validate bytes afresh."""
    by_course = {course["name"]: course for course in courses}
    for row in catalog.rows(SOURCE_ROWS):
        # The upload page's rename/delete API manages immediate course files.
        # Nested imports also get reclamation, through their stable lesson ID.
        path = Path(row["path"])
        # Group this file-management view by its actual upload folder. The
        # catalog course can briefly lag an API move until the next scan.
        course_path = Path(path.parts[0]) if len(path.parts) > 1 else Path(".")
        name = str(path.relative_to(course_path))
        course = by_course.setdefault(str(course_path), {"name": str(course_path), "videos": []})
        if str(course_path) == row["course_path"]:
            course["title"] = row["course_title"]
        else:
            course.setdefault("title", library.name if str(course_path) == "." else str(course_path))
        video = next((v for v in course["videos"] if v["name"] == name), None)
        original = library / path
        exists = original.is_file()
        reclaimed = bool(row["retained_snapshot"]) and not exists
        if video is None:
            if not exists and not reclaimed:
                continue
            video = {"name": name, "bytes": row["size"] if exists else 0}
            course["videos"].append(video)
        snapshot = published_snapshot(row)
        reason = ""
        if reclaimed:
            reason = "Original reclaimed · saved video retained"
        elif snapshot is None:
            reason = "Available after the current lesson finishes processing."
        elif not snapshot.is_file():
            reason = "Saved video is missing; keep the original."
        video.update(lesson_id=row["id"], build_id=row["desired_build"],
                     source_reclaimed=reclaimed, reclaimable=not reason,
                     reclaim_reason=reason, original_bytes=row["size"],
                     file_actions=len(path.parts) == 2 and len(course_path.parts) == 1,
                     href=f"{row['course_slug']}/{row['slug']}.html" if row["published"] else None)
        if reclaimed:
            video["bytes"] = 0
    for course in by_course.values():
        course["videos"].sort(key=lambda video: natural_key(video["name"]))
    return sorted(by_course.values(), key=lambda course: natural_key(course["name"]))


def _identity(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def reclaim_source(catalog, library: Path, lesson_id: str, body: dict) -> dict:
    if not isinstance(body, dict) or set(body) != {"build_id"} or not isinstance(body["build_id"], str):
        raise ValueError("Expected the completed lesson's build_id.")
    library = library.resolve()
    # Same lock as uploads, renames, cancellation, publication and reconciliation.
    with catalog.lock():
        rows = catalog.rows(SOURCE_ROWS + " AND l.id=?", (lesson_id,))
        if not rows:
            raise KeyError(lesson_id)
        row = rows[0]
        original = library / row["path"]
        if not original.resolve().is_relative_to(library) or original.resolve() != original:
            raise CatalogConflict("Only ordinary files inside the library can be reclaimed.")
        if row["retained_snapshot"] and not original.exists() and row["reclaimed_build"] == body["build_id"]:
            return {"lesson_id": lesson_id, "removed_bytes": 0, "already_reclaimed": True}
        if row["desired_build"] != body["build_id"]:
            raise CatalogConflict("This lesson changed. Refresh the source list before reclaiming space.")
        snapshot = published_snapshot(row)
        if snapshot is None:
            raise CatalogConflict("Wait for this lesson to finish processing before reclaiming its original.")
        if not original.is_file() or not snapshot.is_file():
            raise CatalogConflict("The original or saved video is missing. Nothing was removed.")
        if snapshot.resolve() != snapshot or original.stat().st_nlink != 1 or snapshot.stat().st_nlink != 1:
            raise CatalogConflict("Linked files cannot be reclaimed automatically. Nothing was removed.")
        before, saved_before = _identity(original), _identity(snapshot)
        if before[:2] == saved_before[:2]:
            raise CatalogConflict("The original and saved video are the same file. Nothing was removed.")
        if before[2:4] != (row["size"], row["mtime"]) or saved_before[2] != row["size"]:
            raise CatalogConflict("The video changed since processing. Wait for it to be processed again.")
    # Large originals can take minutes to verify. Do not hold up the queue,
    # other uploads or library edits; fence any concurrent change below.
    if digest_file(snapshot) != row["digest"] or digest_file(original) != row["digest"]:
        raise CatalogConflict("The saved video does not match the original. Nothing was removed.")
    # Flush the retained copy before making it the sole authoritative source.
    with snapshot.open("rb") as stream:
        os.fsync(stream.fileno())
    _sync_directory(snapshot.parent)
    _sync_directory(snapshot.parent.parent)
    _sync_directory(snapshot.parent.parent.parent)
    with catalog.lock():
        current = catalog.rows(SOURCE_ROWS + " AND l.id=?", (lesson_id,))
        if not current or any(current[0][key] != row[key] for key in ("path", "digest", "desired_build", "published", "state")):
            raise CatalogConflict("This lesson changed during verification. Nothing was removed; refresh before trying again.")
        if (original.resolve() != original or snapshot.resolve() != snapshot
                or _identity(original) != before or _identity(snapshot) != saved_before):
            raise CatalogConflict("A video changed during verification. Nothing was removed.")
        # Commit intent BEFORE unlink. A crash on either side of the unlink can
        # never make a subsequent scan interpret this as deletion of the lesson.
        with catalog.connect() as db:
            db.execute("INSERT OR REPLACE INTO reclaimed_sources VALUES (?,?,?,?,?)",
                       (lesson_id, row["digest"], str(snapshot), body["build_id"], time.time()))
        try:
            if _identity(original) != before or _identity(snapshot) != saved_before:
                raise CatalogConflict("A video changed during verification. Nothing was removed.")
            original.unlink()
        except (OSError, CatalogConflict):
            if original.exists():
                with catalog.connect() as db:
                    db.execute("DELETE FROM reclaimed_sources WHERE lesson_id=?", (lesson_id,))
            raise
        _sync_directory(original.parent)
        return {"lesson_id": lesson_id, "removed_bytes": row["size"], "already_reclaimed": False}
