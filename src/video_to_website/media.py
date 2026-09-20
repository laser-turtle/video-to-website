"""ffmpeg/ffprobe wrappers: probe, audio extraction, scene detection, frame grabs."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .compute import operation
from .util import run, which_or_die
from .work_progress import report_work

FFMPEG_HINT = "Install ffmpeg, or run inside `nix develop` / `nix run` from this repo."


class FFmpegProgress:
    """Interpret ffmpeg's key=value progress channel; media stdout stays intact."""
    def __init__(self, phase: str, label: str, duration: float | None):
        self.phase, self.label = phase, label
        self.duration = duration if duration and math.isfinite(duration) and duration > 0 else None
        self.seconds = 0.0
        report_work(phase, label, completed=0, total=self.duration, unit="seconds")

    def __call__(self, line: str):
        key, _, value = line.partition("=")
        if key == "out_time_us":
            try:
                seconds = float(value) / 1_000_000
            except ValueError:
                return
            if math.isfinite(seconds) and seconds >= 0:
                self.seconds = max(self.seconds, seconds)
        elif key == "progress" and self.duration:
            # A final timestamp can precede muxing/flushing. Reserve 100% for
            # ffmpeg's explicit end report.
            completed = self.duration if value == "end" else min(self.seconds, self.duration * .99)
            report_work(self.phase, self.label, completed=completed, total=self.duration, unit="seconds")


_PROGRESS_FLAGS = ["-nostats", "-stats_period", "1", "-progress", "pipe:2"]


def ffmpeg_bin() -> str:
    return which_or_die("ffmpeg", FFMPEG_HINT)


def ffprobe_bin() -> str:
    return which_or_die("ffprobe", FFMPEG_HINT)


def _parse_fraction(value: str | None) -> float:
    if not value or "/" not in value:
        try:
            return float(value) if value else 0.0
        except ValueError:
            return 0.0
    num, _, den = value.partition("/")
    try:
        num_f, den_f = float(num), float(den)
    except ValueError:
        return 0.0
    return num_f / den_f if den_f else 0.0


@operation("probe", remote=False)
def probe(video: Path) -> dict:
    """Container/stream facts we care about: duration, dimensions, fps, codecs."""
    proc = run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video),
        ]
    )
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for candidate in (data.get("format", {}).get("duration"), video_stream.get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue

    return {
        "duration": duration,
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "fps": round(_parse_fraction(video_stream.get("avg_frame_rate")), 3),
        "video_codec": video_stream.get("codec_name"),
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
        "has_audio": audio_stream is not None,
        "size_bytes": int(data.get("format", {}).get("size") or 0),
    }


@operation("audio", output="out_wav", remote=False)
def extract_audio(video: Path, out_wav: Path, *, duration: float | None = None) -> Path:
    """16 kHz mono PCM, which is what whisper.cpp expects."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            ffmpeg_bin(),
            *_PROGRESS_FLAGS,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        ],
        on_stderr=FFmpegProgress("audio", "Extracting audio", duration),
    )
    return out_wav


_PTS_RE = re.compile(r"pts_time:([0-9]+\.?[0-9]*)")
_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9]+\.?[0-9]*)")


@operation("scenes")
def detect_scenes(
    video: Path,
    *,
    floor: float = 0.02,
    analysis_width: int = 480,
    duration: float | None = None,
) -> list[list[float]]:
    """Return [time, score] pairs for every frame that differs from the one before.

    `floor` is deliberately low: scoring once and filtering later means the
    screenshot threshold can be re-tuned without decoding the video again.
    Frames are downscaled first, which is much faster and stops a moving cursor
    from registering as a change.
    """
    pairs: dict[float, float] = {}
    pending: float | None = None

    def collect(line: str):
        nonlocal pending
        time_match = _PTS_RE.search(line)
        if time_match:
            pending = round(float(time_match[1]), 3)
            return
        score_match = _SCORE_RE.search(line)
        if score_match and pending is not None:
            score = float(score_match[1])
            if score > floor:
                pairs[pending] = max(round(score, 4), pairs.get(pending, 0.0))
            pending = None

    # Score every frame so ffmpeg's progress clock advances even through a
    # motionless passage. Filter the streamed scores here, without retaining
    # all frame metadata or sending it to journald.
    filtergraph = (
        f"scale={analysis_width}:-2,"
        "select='gte(scene,0)',"
        "metadata=print:file=-"
    )
    run(
        [
            ffmpeg_bin(),
            *_PROGRESS_FLAGS,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-an",
            "-sn",
            "-dn",
            "-vf",
            filtergraph,
            "-f",
            "null",
            "-",
        ],
        on_stdout=collect,
        capture_stdout=False,
        on_stderr=FFmpegProgress("scenes", "Analyzing visual changes", duration),
    )
    return [[time, pairs[time]] for time in sorted(pairs)]


def scene_entries(pairs: list, threshold: float) -> list[tuple[float, float]]:
    """Stored scene data as (time, score) tuples above a threshold."""
    entries: list[tuple[float, float]] = []
    for entry in pairs:
        if isinstance(entry, (int, float)):  # cached before scores were stored
            entries.append((float(entry), 1.0))
        elif entry and entry[1] >= threshold:
            entries.append((float(entry[0]), float(entry[1])))
    return entries


def scene_times(pairs: list, threshold: float) -> list[float]:
    """Filter stored scene data down to the timestamps worth screenshotting."""
    times = []
    for entry in pairs:
        if isinstance(entry, (int, float)):  # data cached before scores were stored
            times.append(float(entry))
        elif entry and entry[1] >= threshold:
            times.append(float(entry[0]))
    return times


@operation("frame", output="out_path")
def extract_frame(video: Path, timestamp: float, out_path: Path, *, width: int = 1920, quality: int = 3) -> Path:
    """Grab a single frame at `timestamp` as a JPEG."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            ffmpeg_bin(),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{max(0.0, timestamp):.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({width}\\,iw)':-2:flags=lanczos",
            "-q:v",
            str(quality),
            str(out_path),
        ]
    )
    return out_path


# A 16x16 grid, not the usual 8x8: a screencast is mostly static UI chrome, and
# at 8x8 real changes inside the small 3D viewport vanish into the panels.
HASH_GRID = 16
HASH_BITS = HASH_GRID * HASH_GRID


@operation("frame_hash")
def frame_hash(video: Path, timestamp: float) -> int | None:
    """A 256-bit difference hash of one frame, for spotting near-identical shots.

    Each bit compares a pixel with its right-hand neighbour in a grayscale
    thumbnail, so the hash tracks structure and ignores small colour shifts.
    """
    width = HASH_GRID + 1
    proc = run(
        [
            ffmpeg_bin(),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, timestamp):.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:{HASH_GRID},format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        text=False,
        check=False,
    )
    data = proc.stdout
    if proc.returncode != 0 or len(data) < width * HASH_GRID:
        return None

    bits = 0
    for row in range(HASH_GRID):
        offset = row * width
        for column in range(HASH_GRID):
            bits <<= 1
            if data[offset + column] > data[offset + column + 1]:
                bits |= 1
    return bits


def hamming(left: int, right: int) -> int:
    """How many bits differ between two frame hashes."""
    return bin(left ^ right).count("1")


@operation("activity")
def frame_activity(
    video: Path,
    start: float,
    duration: float,
    *,
    fps: int = 10,
    analysis_width: int = 480,
) -> list[tuple[float, float]]:
    """How much the picture changes across one window, sampled at `fps`.

    Sampling matters: these sources are 60fps, where consecutive frames differ
    so little during a slow drag that real movement scores the same as a still
    screen. Comparing frames 100ms apart separates the two cleanly.
    Returned times are relative to `start`.
    """
    filtergraph = (
        f"fps={fps},scale={analysis_width}:-2,"
        "select='gte(scene\\,0)',metadata=print:file=-"
    )
    proc = run(
        [
            ffmpeg_bin(),
            *_PROGRESS_FLAGS,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, start):.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(video),
            "-an",
            "-sn",
            "-dn",
            "-vf",
            filtergraph,
            "-f",
            "null",
            "-",
        ],
        on_stderr=FFmpegProgress("activity", "Analyzing clip motion", duration),
    )
    samples: list[tuple[float, float]] = []
    pending: float | None = None
    for line in (proc.stdout or "").splitlines():
        time_match = _PTS_RE.search(line)
        if time_match:
            pending = round(float(time_match.group(1)), 3)
            continue
        score_match = _SCORE_RE.search(line)
        if score_match and pending is not None:
            samples.append((pending, round(float(score_match.group(1)), 5)))
            pending = None
    return samples


@operation("duration", source="path", remote=False)
def duration_of(path: Path) -> float:
    """Length of a media file in seconds, or 0.0 if it cannot be read."""
    proc = run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=False,
    )
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return 0.0


@operation("clip", output="out_path")
def extract_clip(
    video: Path,
    start: float,
    out_path: Path,
    *,
    duration: float = 3.5,
    width: int = 1920,
    fps: int = 30,
    crf: int = 24,
    keep: list[tuple[float, float]] | None = None,
    encoder: str = "libx264",
) -> Path:
    """Cut a short silent clip, for steps where the motion is the point.

    Encoded as h264 rather than GIF: at the same length a GIF is several times
    larger and lower resolution, and a muted looping <video> plays the same way.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Keeping only some ranges means re-timing what survives, so the clip plays
    # continuously instead of freezing where frames were dropped.
    scaling = f"scale='min({width}\\,iw)':-2:flags=lanczos"
    if keep:
        picks = "+".join(f"between(t\\,{a:.3f}\\,{b:.3f})" for a, b in keep)
        filtergraph = f"fps={fps},select='{picks}',setpts=N/FRAME_RATE/TB,{scaling}"
    else:
        filtergraph = f"{scaling},fps={fps}"

    encoding = clip_encoding_args(encoder, crf)
    run(
        [
            ffmpeg_bin(),
            *_PROGRESS_FLAGS,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{max(0.0, start):.3f}",
            "-i",
            str(video),
            "-t",
            f"{duration:.2f}",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            filtergraph,
            *encoding,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(out_path),
        ],
        on_stderr=FFmpegProgress("clip", "Encoding clip", sum(b - a for a, b in keep) if keep else duration),
    )
    return out_path


def clip_encoding_args(encoder: str, crf: int) -> list[str]:
    # `auto` authorizes a helper's native encoder; local execution stays on CPU.
    # CQ and CRF are different controls. These are explicit encoding profiles,
    # not a promise of identical size or quality between hardware and software.
    if encoder in ("auto", "libx264"):
        return ["-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast"]
    if encoder == "h264_nvenc":
        return ["-c:v", encoder, "-preset", "p4", "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    raise ValueError("Unsupported clip encoder.")
