"""Conservative filename/title numbering, independent of saved library order."""

from __future__ import annotations

import re
from pathlib import Path


_NUMBERED = re.compile(
    r"^(?:(?:chapter|section)\s+)?([0-9]{1,3})\s*[._\-–—]\s*"
    r"([0-9]{1,3})(?=$|[\s._:()\-–—])", re.IGNORECASE,
)
_LABELLED = re.compile(
    r"^(?:chapter|section)\s+([0-9]{1,3})\s*[:.\-–—]?\s*"
    r"(?:lesson|part)\s+([0-9]{1,3})(?=$|[\s._:()\-–—])", re.IGNORECASE,
)
_DATE = re.compile(r"^[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{4}(?=$|\D)")


def _numbering(title: str) -> tuple[int, int] | None:
    """Recognize leading 4.01 / 4-02 / 4_03, not embedded versions or dates."""
    title = title.strip()
    if _DATE.match(title):
        return None
    match = _LABELLED.match(title) or _NUMBERED.match(title)
    return (int(match[1]), int(match[2])) if match else None


def chapter_number(title: str) -> int | None:
    numbering = _numbering(title)
    return numbering[0] if numbering else None


def lesson_numbering(lesson: dict) -> tuple[int, int] | None:
    # An explicitly numbered rename wins. An unnumbered rename still benefits
    # from the source's chapter; generated descriptions never override a source.
    candidates = [lesson.get("display_title", "")]
    if lesson.get("source_name"):
        candidates.append(Path(lesson["source_name"]).stem)
    else:
        candidates.append(lesson.get("title", ""))
    for title in candidates:
        number = _numbering(title or "")
        if number is not None:
            return number
    return None


def lesson_chapter(lesson: dict) -> int | None:
    override = lesson.get("chapter_override")
    if override is not None:
        return None if override == -1 else override
    numbering = lesson_numbering(lesson)
    return numbering[0] if numbering else None


def chapter_groups(lessons: list[dict]) -> list[dict]:
    """Collect each chapter once, retaining first appearance and lesson order."""
    groups: dict[int | None, dict] = {}
    for lesson in lessons:
        number = lesson_chapter(lesson)
        if number not in groups:
            groups[number] = {
                "number": number,
                "key": f"{number if number is not None else 'other'}:1",
                "label": f"Chapter {number}" if number is not None else "Other lessons",
                "lessons": [],
            }
        groups[number]["lessons"].append(lesson)
    return list(groups.values())
