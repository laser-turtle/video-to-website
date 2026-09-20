"""Course discovery and the per-video stage pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import media, render, steps as steps_mod, whisper
from .progress import Progress
from .util import (
    CommandError,
    die,
    file_fingerprint,
    find_videos,
    hms,
    human_duration,
    log,
    natural_key,
    read_stage,
    slugify,
    title_from_filename,
    warn,
    write_stage,
    VIDEO_SUFFIXES,
)

# Bump when the prompt or step schema changes, so cached steps are recomputed.
PROMPT_VERSION = 4

STAGE_ORDER = ["probe", "transcribe", "scenes", "steps", "frames", "render"]


@dataclass
class BuildOptions:
    out: Path
    work: Path | None = None
    model: str = "small.en"
    language: str = "en"
    threads: int | None = None
    llm: str = "auto"
    llm_model: str | None = None
    llm_fallbacks: bool = True
    chunk_minutes: float = 25.0
    scene_threshold: float = 0.03
    scene_floor: float = 0.015
    scene_analysis_width: int = 480
    frame_width: int = 1920
    frame_quality: int = 3
    frames_per_step: int = 4
    seconds_per_frame: float = 15.0
    frame_dedup_distance: int = 16
    clips: str = "auto"
    clip_max_seconds: float = 45.0
    clip_width: int = 1920
    clip_crf: int = 24
    clip_fps: int = 30
    clip_max_still: float = 1.5
    shots_with_clip: str = "one"
    videos: str = "symlink"
    markdown: bool = True
    keep_audio: bool = False
    force: set[str] = field(default_factory=set)
    stop_after: str | None = None

    def wants(self, stage: str) -> bool:
        if not self.stop_after:
            return True
        return STAGE_ORDER.index(stage) <= STAGE_ORDER.index(self.stop_after)

    def forced(self, stage: str) -> bool:
        return "all" in self.force or stage in self.force


def _has_videos(directory: Path, *, recursive: bool) -> bool:
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    return any(
        p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith(".")
        for p in iterator
    )


def discover_courses(paths: list[Path]) -> list[dict]:
    """Each input path becomes a course, unless it is a folder of course folders."""
    courses: list[dict] = []
    for path in paths:
        path = path.expanduser().resolve()
        if not path.exists():
            die(f"no such path: {path}")

        if path.is_file():
            courses.append({"title": path.parent.name, "root": path.parent, "videos": [path]})
            continue

        direct = [
            p
            for p in sorted(path.iterdir(), key=lambda p: natural_key(p.name))
            if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith(".")
        ]
        subdirs = [
            d
            for d in sorted(path.iterdir(), key=lambda p: natural_key(p.name))
            if d.is_dir() and not d.name.startswith(".") and _has_videos(d, recursive=True)
        ]

        if not direct and subdirs:
            for sub in subdirs:
                courses.append({"title": sub.name, "root": sub, "videos": find_videos(sub)})
        else:
            courses.append({"title": path.name, "root": path, "videos": find_videos(path)})

    for course in courses:
        if not course["videos"]:
            warn(f"no video files under {course['root']}")
    return [course for course in courses if course["videos"]]


def _unique(slug: str, taken: set[str]) -> str:
    candidate, counter = slug, 2
    while candidate in taken:
        candidate = f"{slug}-{counter}"
        counter += 1
    taken.add(candidate)
    return candidate


def process_video(
    video: Path,
    *,
    work_dir: Path,
    course_dir: Path,
    slug: str,
    options: BuildOptions,
    backend,
    model_path: Path | None,
    progress: Progress | None = None,
) -> dict | None:
    """Run every enabled stage for one video and return its lesson record."""
    progress = progress or Progress(None)
    fingerprint = file_fingerprint(video)
    work_dir.mkdir(parents=True, exist_ok=True)
    fallback_title = title_from_filename(video)

    progress.stage("probe")
    # --- probe -------------------------------------------------------------
    probe_params = {"stage": "probe"}
    info = None if options.forced("probe") else read_stage(work_dir / "probe.json", fingerprint, probe_params)
    if info is None:
        info = media.probe(video)
        write_stage(work_dir / "probe.json", fingerprint, probe_params, info)
    duration = float(info.get("duration") or 0.0)
    if duration <= 0:
        warn(f"could not read a duration from {video.name}; skipping")
        return None
    log(f"{video.name}  ({human_duration(duration)})")
    if not options.wants("transcribe"):
        return None

    progress.stage("transcribe")
    # --- transcribe --------------------------------------------------------
    if not info.get("has_audio"):
        warn(f"{video.name} has no audio track; skipping")
        return None
    transcribe_params = {
        "stage": "transcribe",
        "model": model_path.name if model_path else options.model,
        "language": options.language,
    }
    segments = (
        None
        if options.forced("transcribe")
        else read_stage(work_dir / "transcript.json", fingerprint, transcribe_params)
    )
    if segments is None:
        log("  transcribing with whisper.cpp")
        audio = work_dir / "audio.wav"
        media.extract_audio(video, audio)
        try:
            segments = whisper.transcribe(
                audio,
                model_path,
                # Raw whisper output stays alongside our cache, under its own name.
                out_prefix=work_dir / "whisper",
                language=options.language,
                threads=options.threads,
            )
        finally:
            if not options.keep_audio:
                audio.unlink(missing_ok=True)
        write_stage(work_dir / "transcript.json", fingerprint, transcribe_params, segments)
    else:
        log(f"  transcript cached ({len(segments)} segments)")
    if not segments:
        warn(f"{video.name}: empty transcript; skipping")
        return None
    if not options.wants("scenes"):
        return None

    progress.stage("scenes")
    # --- scenes ------------------------------------------------------------
    # The threshold is deliberately absent from the cache key: scores are stored
    # once, so re-tuning --scene-threshold never re-decodes the video.
    scene_params = {
        "stage": "scenes",
        "floor": options.scene_floor,
        "analysis_width": options.scene_analysis_width,
    }
    scene_pairs = (
        None if options.forced("scenes") else read_stage(work_dir / "scenes.json", fingerprint, scene_params)
    )
    if scene_pairs is None:
        log("  detecting on-screen changes")
        scene_pairs = media.detect_scenes(
            video,
            floor=options.scene_floor,
            analysis_width=options.scene_analysis_width,
        )
        write_stage(work_dir / "scenes.json", fingerprint, scene_params, scene_pairs)
    scene_times = media.scene_times(scene_pairs, options.scene_threshold)
    log(f"  {len(scene_times)} on-screen changes above {options.scene_threshold}")
    if not options.wants("steps"):
        return None

    progress.stage("steps")
    # --- steps -------------------------------------------------------------
    backend_name = backend.name if backend else "heuristic"
    steps_params = {
        "stage": "steps",
        "backend": backend_name,
        "model": getattr(backend, "model", None),
        "chunk_minutes": options.chunk_minutes,
        "prompt_version": PROMPT_VERSION,
    }
    if backend is None:
        # Heuristic boundaries come straight from the scene times, so the
        # threshold belongs in the cache key. For an LLM the scene times are
        # only a hint, and re-running would cost real money.
        steps_params["scene_threshold"] = options.scene_threshold
    lesson = (
        None if options.forced("steps") else read_stage(work_dir / "steps.json", fingerprint, steps_params)
    )
    if lesson is None:
        if backend is None:
            lesson = steps_mod.heuristic_lesson(
                title=fallback_title,
                duration=duration,
                segments=segments,
                scene_times=scene_times,
            )
        else:
            log(f"  writing steps with {backend_name}")
            lesson = steps_mod.extract_lesson(
                backend,
                title=fallback_title,
                duration=duration,
                segments=segments,
                scene_times=scene_times,
                chunk_minutes=options.chunk_minutes,
            )
        lesson = steps_mod.normalize_lesson(lesson, duration=duration, fallback_title=fallback_title)
        write_stage(work_dir / "steps.json", fingerprint, steps_params, lesson)
    else:
        log(f"  steps cached ({len(lesson['steps'])})")

    if not lesson["steps"]:
        warn(f"{video.name}: no steps were produced; skipping")
        return None

    gaps = steps_mod.coverage_gaps(lesson["steps"], duration)
    for start, end in gaps:
        warn(f"  {video.name}: no step covers {hms(start)}-{hms(end)}")

    if not options.wants("frames"):
        return None

    progress.stage("frames")
    # --- frames and clips --------------------------------------------------
    # Emitting real dimensions stops the page reflowing as screenshots arrive,
    # which otherwise moves the step you are reading.
    source_width = int(info.get("width") or 0)
    source_height = int(info.get("height") or 0)

    def scaled(width: int) -> tuple[int, int]:
        if source_width <= 0 or source_height <= 0:
            return width, 0
        width = min(width, source_width)  # ffmpeg is told not to upscale either
        height = round(width * source_height / source_width)
        return width, height + (height % 2)  # ffmpeg's -2 keeps height even

    shot_width, shot_height = scaled(options.frame_width)
    clip_width, clip_height = scaled(options.clip_width)

    def wants_clip(step: dict) -> bool:
        if options.clips == "none":
            return False
        return options.clips == "all" or bool(step.get("motion"))

    def shot_cap(step: dict) -> int:
        # A clip already shows the step moving, so extra stills of the same
        # span are the same information twice. One still is still worth having:
        # it is the checkpoint you compare your own screen against, and it
        # reads without waiting for a loop.
        if not wants_clip(step):
            return options.frames_per_step
        return {"one": 1, "none": 0, "all": options.frames_per_step}[options.shots_with_clip]

    frames_dir = course_dir / "frames" / slug
    clips_dir = course_dir / "clips" / slug
    entries = media.scene_entries(scene_pairs, options.scene_threshold)
    assets_params = {
        "stage": "assets",
        "scene_threshold": options.scene_threshold,
        "frames_per_step": options.frames_per_step,
        "seconds_per_frame": options.seconds_per_frame,
        "dedup_distance": options.frame_dedup_distance,
        "frame_width": options.frame_width,
        "frame_quality": options.frame_quality,
        "clip_crf": options.clip_crf,
        "clips": options.clips,
        "clip_max_seconds": options.clip_max_seconds,
        "clip_width": options.clip_width,
        "clip_fps": options.clip_fps,
        "clip_max_still": options.clip_max_still,
        "shots_with_clip": options.shots_with_clip,
        "steps": [[step["start"], step["end"]] for step in lesson["steps"]],
    }
    cached = (
        None if options.forced("frames") else read_stage(work_dir / "assets.json", fingerprint, assets_params)
    )
    usable = cached is not None and all(
        (course_dir / entry["src"]).exists()
        for record in cached
        for entry in (record.get("frames") or []) + ([record["clip"]] if record.get("clip") else [])
    )

    if usable:
        for step, record in zip(lesson["steps"], cached):
            step["frames"] = record.get("frames") or []
            if record.get("clip"):
                step["clip"] = record["clip"]
    else:
        records: list[dict] = []
        for step in lesson["steps"]:
            step_frames: list[dict] = []
            cap = shot_cap(step)
            if cap:
                times = steps_mod.frames_for_step(
                    step,
                    entries,
                    duration=duration,
                    cap=cap,
                    seconds_per_frame=options.seconds_per_frame,
                )
                # Two scene changes can show the same screen; a perceptual hash
                # keeps the gallery from repeating itself.
                seen_hashes: list[int] = []
                for time in times:
                    digest = media.frame_hash(video, time)
                    if digest is not None:
                        if any(
                            media.hamming(digest, prior) <= options.frame_dedup_distance
                            for prior in seen_hashes
                        ):
                            continue
                        seen_hashes.append(digest)
                    name = f"step-{step['index']:03d}-{len(step_frames) + 1}.jpg"
                    frame_path = frames_dir / name
                    # Always re-extract: reaching here means the cache key
                    # changed, and a file of the same name from a previous
                    # build was made with the settings that just changed.
                    try:
                        media.extract_frame(
                            video,
                            time,
                            frame_path,
                            width=options.frame_width,
                            quality=options.frame_quality,
                        )
                    except CommandError as exc:
                        warn(f"  could not grab a frame at {hms(time)}: {exc}")
                        continue
                    step_frames.append(
                        {
                            "time": round(time, 2),
                            "src": f"frames/{slug}/{name}",
                            "width": shot_width,
                            "height": shot_height,
                        }
                    )
            step["frames"] = step_frames

            clip_record = None
            if wants_clip(step):
                clip_start, clip_end = steps_mod.clip_window(
                    step,
                    segments,
                    duration=duration,
                    max_seconds=options.clip_max_seconds,
                )
                name = f"step-{step['index']:03d}.mp4"
                clip_path = clips_dir / name
                span = clip_end - clip_start
                keep = None
                if options.clip_max_still > 0:
                    keep = steps_mod.trim_still_ranges(
                        media.frame_activity(video, clip_start, span),
                        duration=span,
                        max_still=options.clip_max_still,
                    )
                try:
                    media.extract_clip(
                        video,
                        clip_start,
                        clip_path,
                        duration=span,
                        width=options.clip_width,
                        fps=options.clip_fps,
                        crf=options.clip_crf,
                        keep=keep,
                    )
                    # Trimming re-times the clip, so ask the file how long it is
                    # rather than assuming.
                    length = media.duration_of(clip_path) or span
                    clip_record = {
                        "src": f"clips/{slug}/{name}",
                        "time": clip_start,
                        "seconds": round(length, 2),
                        "source_seconds": round(span, 2),
                        "width": clip_width,
                        "height": clip_height,
                    }
                except CommandError as exc:
                    warn(f"  could not cut a clip at {hms(clip_start)}: {exc}")
            if clip_record:
                step["clip"] = clip_record

            records.append({"frames": step_frames, "clip": clip_record})

        # These directories are entirely ours, so anything not referenced now is
        # left over from an earlier set of settings.
        wanted = {
            (course_dir / entry["src"]).resolve()
            for record in records
            for entry in (record.get("frames") or []) + ([record["clip"]] if record.get("clip") else [])
        }
        for directory in (frames_dir, clips_dir):
            if not directory.is_dir():
                continue
            for stale in directory.iterdir():
                if stale.is_file() and stale.resolve() not in wanted:
                    stale.unlink()

        write_stage(work_dir / "assets.json", fingerprint, assets_params, records)

    for step in lesson["steps"]:
        step["frame"] = step["frames"][0]["src"] if step.get("frames") else None

    shot_count = sum(len(step.get("frames") or []) for step in lesson["steps"])
    clip_count = sum(1 for step in lesson["steps"] if step.get("clip"))
    clip_total = sum(step["clip"]["seconds"] for step in lesson["steps"] if step.get("clip"))
    log(
        f"  {len(lesson['steps'])} steps, {shot_count} screenshots, "
        f"{clip_count} clips ({human_duration(clip_total)})"
    )

    # --- video link --------------------------------------------------------
    video_href = None
    if options.videos != "none":
        target = course_dir / "videos" / f"{slug}{video.suffix.lower()}"
        video_href = render.link_video(video, target, options.videos)

    first_frame = next((step.get("frame") for step in lesson["steps"] if step.get("frame")), None)
    return {
        "slug": slug,
        "title": lesson["title"] or fallback_title,
        "source_name": video.name,
        "source_path": str(video),
        "duration": duration,
        "video_href": video_href,
        "poster": first_frame,
        "summary": lesson["summary"],
        "prerequisites": lesson["prerequisites"],
        "skip": lesson.get("skip") or [],
        "steps": lesson["steps"],
        "transcript": segments,
        "backend": backend_name,
    }


def build(paths: list[Path], options: BuildOptions, backend, progress: Progress | None = None) -> list[dict]:
    courses_in = discover_courses(paths)
    if not courses_in:
        die("no video files found under the given paths")

    progress = progress or Progress(None)
    progress.plan(courses_in)

    model_path = None
    if options.wants("transcribe"):
        model_path = whisper.resolve_model(options.model)
        log(f"whisper model: {model_path}")

    course_slugs: set[str] = set()
    rendered: list[dict] = []

    for course in courses_in:
        course_slug = _unique(slugify(course["title"]), course_slugs)
        course_dir = options.out / course_slug
        work_root = (options.work or (options.out / ".work")) / course_slug
        log(f"course: {course['title']} ({len(course['videos'])} videos)")

        lesson_slugs: set[str] = set()
        lessons = []
        for video in course["videos"]:
            slug = _unique(slugify(video.stem), lesson_slugs)
            progress.start(video)
            try:
                lesson = process_video(
                    video,
                    work_dir=work_root / slug,
                    course_dir=course_dir,
                    slug=slug,
                    options=options,
                    backend=backend,
                    model_path=model_path,
                    progress=progress,
                )
            except CommandError as exc:
                warn(f"{video.name}: {exc}")
                progress.finish("failed")
                continue
            progress.finish("done" if lesson else "skipped")
            if lesson:
                lessons.append(lesson)

        if lessons:
            rendered.append({"slug": course_slug, "title": course["title"], "lessons": lessons})

    if rendered and options.wants("render"):
        render.write_site(options.out, rendered, write_markdown=options.markdown)
    progress.finish_build()
    return rendered
