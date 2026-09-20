"""Turn a timestamped transcript into a validated list of steps."""

from __future__ import annotations

import math
import re
from typing import Any

from .llm import SYSTEM_PROMPT, LLMError, build_user_prompt, extract_json
from .util import log, warn

MAX_TITLE_CHARS = 90
MAX_ACTION_CHARS = 400


def transcript_lines(segments: list[dict]) -> list[str]:
    return [f"[{seg['start']:.1f}] {seg['text']}" for seg in segments]


def _window_segments(segments: list[dict], start: float, end: float) -> list[dict]:
    return [seg for seg in segments if start <= seg["start"] < end]


def _plan_windows(duration: float, chunk_minutes: float, overlap: float) -> list[tuple[float, float]]:
    span = chunk_minutes * 60.0
    if not math.isfinite(span) or span <= overlap:
        raise ValueError("chunk length must be finite and greater than the overlap (45 seconds)")
    if duration <= span * 1.25:
        return [(0.0, duration + 1.0)]
    windows: list[tuple[float, float]] = []
    cursor = 0.0
    while cursor < duration:
        end = min(cursor + span, duration)
        windows.append((cursor, end + 1.0))
        if end >= duration:
            break
        cursor = end - overlap
    return windows


def _ask(backend, system: str, user: str) -> dict:
    """One call plus one repair attempt, since a malformed object is cheap to retry."""
    text = backend.complete(system, user)
    try:
        return extract_json(text)
    except LLMError as exc:
        warn(f"{exc}; retrying once")
        repair = (
            user
            + "\n\nYour previous reply could not be parsed as JSON. "
            "Reply with the JSON object only: no prose, no code fences."
        )
        return extract_json(backend.complete(system, repair))


def extract_lesson(
    backend,
    *,
    title: str,
    duration: float,
    segments: list[dict],
    scene_times: list[float],
    chunk_minutes: float = 25.0,
    overlap_seconds: float = 45.0,
) -> dict:
    """Ask the backend for a structured lesson, chunking long videos into windows."""
    windows = _plan_windows(duration, chunk_minutes, overlap_seconds)
    promo_hints = detect_promo_ranges(segments)
    lesson: dict[str, Any] = {
        "title": "",
        "summary": "",
        "prerequisites": [],
        "skip": [],
        "steps": [],
    }

    for index, (start, end) in enumerate(windows):
        window_segments = _window_segments(segments, start, end)
        if not window_segments:
            continue
        if len(windows) > 1:
            log(f"  window {index + 1}/{len(windows)} ({start:.0f}-{min(end, duration):.0f}s)")

        window_scenes = [t for t in scene_times if start <= t <= end]
        user = build_user_prompt(
            title=title,
            duration=duration,
            transcript_lines=transcript_lines(window_segments),
            scene_times=window_scenes,
            window=(start, min(end, duration)) if len(windows) > 1 else None,
            promo_hints=[
                (hint_start, hint_end)
                for hint_start, hint_end in promo_hints
                if hint_end >= start and hint_start <= end
            ],
        )
        result = _ask(backend, SYSTEM_PROMPT, user)

        if index == 0:
            lesson["title"] = str(result.get("title") or "").strip()
            lesson["summary"] = str(result.get("summary") or "").strip()

        # Prerequisites come from every window, not just the first: an
        # instructor often mentions an add-on or a file partway through, and
        # taking only the opening window silently dropped those.
        seen = {p.lower() for p in lesson["prerequisites"]}
        for entry in result.get("prerequisites") or []:
            text = str(entry).strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                lesson["prerequisites"].append(text)

        for entry in result.get("skip") or []:
            if isinstance(entry, dict):
                lesson["skip"].append(entry)

        for step in result.get("steps") or []:
            if not isinstance(step, dict):
                continue
            # Overlap exists so the model has lead-in context, not to duplicate steps.
            if index > 0:
                try:
                    if float(step.get("start", 0)) < start + overlap_seconds * 0.5:
                        continue
                except (TypeError, ValueError):
                    continue
            lesson["steps"].append(step)

    return lesson


def heuristic_lesson(
    *,
    title: str,
    duration: float,
    segments: list[dict],
    scene_times: list[float],
    target_seconds: float = 100.0,
) -> dict:
    """Segment mechanically, with no LLM: boundaries at scene changes, text kept as-is."""
    boundaries = [0.0]
    for time in scene_times:
        if time - boundaries[-1] >= target_seconds * 0.6:
            boundaries.append(time)
    # Fill gaps where nothing changed visually for a long stretch.
    filled = [0.0]
    for time in boundaries[1:] + [duration]:
        while time - filled[-1] > target_seconds * 2:
            filled.append(filled[-1] + target_seconds)
        filled.append(time)
    boundaries = sorted(set(round(b, 2) for b in filled if b < duration))

    promo = detect_promo_ranges(segments)

    steps = []
    for index, start in enumerate(boundaries):
        end = boundaries[index + 1] if index + 1 < len(boundaries) else duration
        if any(_overlap((start, end), rng) / max(0.001, end - start) >= 0.6 for rng in promo):
            continue  # a course ad is not a step
        chunk = [seg for seg in segments if start <= seg["start"] < end]
        text = " ".join(seg["text"] for seg in chunk).strip()
        if not text:
            continue
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        steps.append(
            {
                "title": (sentences[0] if sentences else text)[:MAX_TITLE_CHARS],
                "start": start,
                "end": end,
                "actions": sentences[:6] or [text],
                "shortcuts": [],
                "note": "",
            }
        )

    return {
        "title": title,
        "summary": "Segmented from the transcript without a language model.",
        "prerequisites": [],
        "skip": [
            {"start": start, "end": end, "reason": "promotional (keyword match)"}
            for start, end in promo
        ],
        "steps": steps,
    }


def _clean_str(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].strip()


def _clean_list(value: Any, limit: int, max_items: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    cleaned = [_clean_str(item, limit) for item in value]
    return [item for item in cleaned if item][:max_items]


def normalize_lesson(lesson: dict, *, duration: float, fallback_title: str) -> dict:
    """Coerce model output into something the renderer can trust."""
    raw_steps = lesson.get("steps") or []
    steps: list[dict] = []

    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("start"))
            end = float(raw.get("end"))
        except (TypeError, ValueError):
            continue
        start = min(max(0.0, start), max(duration - 0.1, 0.0))
        end = min(max(start + 1.0, end), duration)
        actions = _clean_list(raw.get("actions"), MAX_ACTION_CHARS, 8)
        title = _clean_str(raw.get("title"), MAX_TITLE_CHARS)
        if not title and not actions:
            continue
        entry = {
            "title": title or actions[0][:MAX_TITLE_CHARS],
            "start": round(start, 2),
            "end": round(end, 2),
            "actions": actions,
            "shortcuts": _clean_list(raw.get("shortcuts"), 60, 8),
            "note": _clean_str(raw.get("note"), 500),
        }
        motion = raw.get("motion")
        entry["motion"] = bool(motion) if isinstance(motion, bool) else looks_like_motion(entry)
        steps.append(entry)

    steps.sort(key=lambda s: s["start"])

    skip_ranges: list[tuple[float, float]] = []
    skip_records: list[dict] = []
    for entry in lesson.get("skip") or []:
        if not isinstance(entry, dict):
            continue
        try:
            skip_start = max(0.0, float(entry.get("start")))
            skip_end = min(duration, float(entry.get("end")))
        except (TypeError, ValueError):
            continue
        if skip_end <= skip_start:
            continue
        skip_ranges.append((skip_start, skip_end))
        skip_records.append(
            {
                "start": round(skip_start, 2),
                "end": round(skip_end, 2),
                "reason": _clean_str(entry.get("reason"), 120),
            }
        )

    steps, dropped = drop_skipped_steps(steps, skip_ranges)
    if dropped:
        log(f"  dropped {dropped} step(s) inside promotional or skipped ranges")

    # Stop steps from overlapping; a step ends where the next one begins.
    for index, step in enumerate(steps[:-1]):
        next_start = steps[index + 1]["start"]
        if step["end"] > next_start:
            step["end"] = max(step["start"] + 1.0, next_start)
    if steps:
        steps[-1]["end"] = max(steps[-1]["end"], min(steps[-1]["start"] + 1.0, duration))

    for index, step in enumerate(steps, start=1):
        step["index"] = index

    return {
        "title": _clean_str(lesson.get("title"), 120) or fallback_title,
        "summary": _clean_str(lesson.get("summary"), 600),
        "prerequisites": _clean_list(lesson.get("prerequisites"), 300, 10),
        "skip": skip_records,
        "steps": steps,
    }


def frames_for_step(
    step: dict,
    scene_entries: list[tuple[float, float]],
    *,
    duration: float,
    cap: int = 4,
    seconds_per_frame: float = 15.0,
    min_spacing: float = 4.0,
) -> list[float]:
    """Pick the moments worth screenshotting inside one step.

    A single screenshot per step skips the middle of the work, so a step earns
    roughly one shot per `seconds_per_frame`. Detected visual changes are
    preferred, starting with the last one because that is the state the reader
    is trying to match. Screencasts often produce no detected change at all
    during steady modelling work, so whatever is still missing is filled in at
    evenly spread times rather than left out.
    """
    start, end = step["start"], step["end"]
    span = max(0.0, end - start)
    wanted = max(1, min(cap, math.ceil(span / max(1.0, seconds_per_frame))))

    lead_in = start + min(1.5, span * 0.1)
    latest = max(lead_in, end - 0.75)
    candidates = [(time, score) for time, score in scene_entries if lead_in <= time <= latest]

    chosen: list[float] = []
    if candidates:
        chosen.append(candidates[-1][0])  # the step's finished state
        for time, _score in sorted(candidates, key=lambda entry: entry[1], reverse=True):
            if len(chosen) >= wanted:
                break
            if all(abs(time - taken) >= min_spacing for taken in chosen):
                chosen.append(time)

    # Top up with evenly spread times, so density does not depend on whether
    # ffmpeg happened to score a change here.
    if len(chosen) < wanted:
        divisions = wanted + 1
        for index in range(1, divisions):
            if len(chosen) >= wanted:
                break
            candidate = start + span * index / divisions
            if candidate < lead_in or candidate > latest:
                continue
            if all(abs(candidate - taken) >= min_spacing for taken in chosen):
                chosen.append(candidate)

    if not chosen:
        chosen.append(max(start, min(end - 0.5, duration - 0.1)))

    return [round(max(0.0, min(time, duration - 0.1)), 2) for time in sorted(chosen)]


MOTION_PATTERNS = [
    r"\bdrag(ging)?\b", r"\bslide?(ing)?\b", r"\bmove\b", r"\bmoving\b",
    r"\bextrud(e|ing)\b", r"\bscal(e|ing)\b", r"\brotat(e|ing)\b",
    r"\bsculpt(ing)?\b", r"\borbit\b", r"\bspin\b", r"\bsweep\b",
    r"\bloop cut\b", r"\bbevel\b", r"\binset\b", r"\bproportional editing\b",
    r"\buntil it (looks|sits)\b", r"\bback and forth\b", r"\bgradually\b",
]

_MOTION_RE = re.compile("|".join(MOTION_PATTERNS), re.IGNORECASE)


def looks_like_motion(step: dict) -> bool:
    """Fallback when the model did not label a step: does it describe movement?"""
    text = " ".join([step.get("title") or ""] + list(step.get("actions") or []))
    return bool(_MOTION_RE.search(text))


def clip_window(
    step: dict,
    segments: list[dict],
    *,
    duration: float,
    max_seconds: float = 45.0,
    min_seconds: float = 1.5,
    lead_in: float = 0.5,
    tail: float = 1.0,
) -> tuple[float, float]:
    """The span a step's clip should cover.

    A fixed-length clip stops wherever it stops, which strands whatever happened
    just after it: the next step's clip has already moved on, so the only way to
    see the join is the original video. Steps tile the lesson end to end and
    their bounds come from whisper's timestamps, so covering the step's own span
    makes consecutive clips meet instead of leaving holes.
    """
    start = max(0.0, step["start"] - lead_in)
    end = min(duration, step["end"] + tail)

    # Do not cut mid-sentence: if speech straddles the step boundary, run to the
    # end of that segment.
    for segment in segments:
        if segment["start"] < step["end"] <= segment["end"]:
            end = max(end, min(segment["end"] + 0.3, duration))

    if end - start > max_seconds:
        # Too long to show whole. Keep the tail, where the step's result appears.
        start = max(0.0, end - max_seconds)

    if end - start < min_seconds:
        end = min(duration, start + min_seconds)
        start = max(0.0, end - min_seconds)

    return round(start, 2), round(end, 2)


def trim_still_ranges(
    activity: list[tuple[float, float]],
    *,
    duration: float,
    max_still: float = 1.5,
    threshold: float = 0.004,
    max_ranges: int = 30,
) -> list[tuple[float, float]] | None:
    """Which parts of a clip window to keep, collapsing long motionless runs.

    A clip covers a whole step, and a step often contains a stretch where the
    instructor is talking and the screen does not move. Those seconds are worth
    keeping in the lesson video, which has the audio, but in a silent looping
    clip they are dead air. Each motionless run is cut back to `max_still`
    seconds so the clip keeps its rhythm without the waiting.

    Returns None when there is nothing worth trimming, which keeps the caller
    on the simpler no-filter path.
    """
    if not activity or duration <= 0:
        return None

    drops: list[tuple[float, float]] = []
    run_start: float | None = None
    for index, (time, score) in enumerate(activity):
        if score < threshold:
            if run_start is None:
                run_start = time
            continue
        if run_start is not None and time - run_start > max_still:
            drops.append((run_start + max_still, time))
        run_start = None
    if run_start is not None and duration - run_start > max_still:
        drops.append((run_start + max_still, duration))

    if not drops:
        return None

    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in drops:
        if start - cursor > 0.15:
            keeps.append((round(cursor, 3), round(start, 3)))
        cursor = max(cursor, end)
    if duration - cursor > 0.15:
        keeps.append((round(cursor, 3), round(duration, 3)))

    kept = sum(end - start for start, end in keeps)
    # Not worth a filter if almost nothing goes, and a clip trimmed to nothing
    # is worse than one with some waiting in it.
    if not keeps or len(keeps) > max_ranges or kept < 1.0 or duration - kept < 0.75:
        return None
    return keeps


PROMO_PATTERNS = [
    r"link in the (video )?description",
    r"check (it |them )?out (my|our|the) ",
    r"(my|our) (free |other |full |new )*(starter |beginner )?courses?\b",
    r"\bpatreon\b",
    r"sponsored by|this video is sponsored|our sponsor",
    r"(like and )?subscribe\b|hit the bell|smash that",
    r"discount code|coupon code|promo code",
    r"sign up (for|at)|enroll (in|now)",
    r"\bgumroad\b|blender ?market",
    r"support (me|us|the channel)",
    r"courses that finally make",
]

_PROMO_RE = re.compile("|".join(PROMO_PATTERNS), re.IGNORECASE)


def detect_promo_ranges(
    segments: list[dict],
    *,
    pad: float = 6.0,
    merge_gap: float = 25.0,
) -> list[tuple[float, float]]:
    """Keyword scan for self-promotion, used as a hint to the model and as the
    heuristic backend's only defence against course ads becoming steps."""
    hits: list[tuple[float, float]] = []
    for segment in segments:
        if _PROMO_RE.search(segment.get("text") or ""):
            hits.append((max(0.0, segment["start"] - pad), segment["end"] + pad))

    merged: list[list[float]] = []
    for start, end in sorted(hits):
        if merged and start - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(round(start, 2), round(end, 2)) for start, end in merged]


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def drop_skipped_steps(
    steps: list[dict],
    skip_ranges: list[tuple[float, float]],
    *,
    threshold: float = 0.6,
) -> tuple[list[dict], int]:
    """Remove steps that sit mostly inside a skipped range."""
    if not skip_ranges:
        return steps, 0
    kept = []
    dropped = 0
    for step in steps:
        span = max(0.001, step["end"] - step["start"])
        covered = sum(_overlap((step["start"], step["end"]), rng) for rng in skip_ranges)
        if covered / span >= threshold:
            dropped += 1
            continue
        kept.append(step)
    return kept, dropped


def coverage_gaps(steps: list[dict], duration: float, *, min_gap: float = 90.0) -> list[tuple[float, float]]:
    """Stretches of the lesson no step covers, for the build report."""
    gaps = []
    cursor = 0.0
    for step in steps:
        if step["start"] - cursor >= min_gap:
            gaps.append((cursor, step["start"]))
        cursor = max(cursor, step["end"])
    if duration - cursor >= min_gap:
        gaps.append((cursor, duration))
    return gaps
