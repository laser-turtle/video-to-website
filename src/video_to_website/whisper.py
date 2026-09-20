"""whisper.cpp wrapper: model resolution, download, and transcription."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from .compute import operation

from .util import CommandError, die, log, run, warn, which_or_die
from .work_progress import report_work

_PROGRESS_RE = re.compile(r"whisper_print_progress_callback:\s*progress\s*=\s*(\d+)%")


def _report_progress(line: str) -> None:
    match = _PROGRESS_RE.search(line)
    if match and 0 <= int(match[1]) <= 100:
        report_work("speech", "Transcribing audio", completed=int(match[1]), total=100, unit="percent")

HF_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"

# sha256 digests for the models this project pins in flake.nix. Models without a
# digest still download; they just are not integrity-checked.
KNOWN_DIGESTS = {
    "base.en": "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
    "small.en": "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
}

SUGGESTED_MODELS = [
    "tiny.en",
    "base.en",
    "small.en",
    "medium.en",
    "large-v3-turbo",
]

WHISPER_HINT = (
    "Install whisper-cpp, or run inside `nix develop` / `nix run` from this repo. "
    "Override the binary name with V2W_WHISPER_BIN."
)


def whisper_bin() -> str:
    name = os.environ.get("V2W_WHISPER_BIN", "whisper-cli")
    return which_or_die(name, WHISPER_HINT)


def model_dir() -> Path:
    """Where downloaded models live (flake-pinned models are found separately)."""
    env = os.environ.get("V2W_MODEL_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base).expanduser() / "video-to-website" / "models"


def _search_paths(name: str) -> list[Path]:
    filename = f"ggml-{name}.bin"
    paths = [model_dir() / filename]
    # Directories (or exact files) injected by the Nix wrapper, colon-separated.
    for entry in (os.environ.get("V2W_MODEL_PATH") or "").split(":"):
        if not entry:
            continue
        candidate = Path(entry)
        if candidate.is_file():
            # An exact file only counts when it is the model that was asked for.
            if candidate.name == filename:
                paths.append(candidate)
        else:
            paths.append(candidate / filename)
    return paths


def resolve_model(spec: str, *, auto_download: bool = True) -> Path:
    """Turn a model spec ('small.en' or a path) into a file on disk."""
    as_path = Path(spec).expanduser()
    if as_path.suffix == ".bin" or as_path.is_file():
        if not as_path.is_file():
            die(f"whisper model file not found: {as_path}")
        return as_path

    for candidate in _search_paths(spec):
        if candidate.is_file():
            return candidate

    if not auto_download:
        die(
            f"whisper model '{spec}' is not available locally. "
            f"Run: v2w fetch-model {spec}"
        )
    return fetch_model(spec)


def fetch_model(name: str, *, dest_dir: Path | None = None) -> Path:
    """Download a ggml whisper model, verifying it when we know its digest."""
    dest_dir = dest_dir or model_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"ggml-{name}.bin"
    if target.is_file():
        log(f"model already present: {target}")
        return target

    url = HF_URL.format(name=name)
    tmp = target.with_suffix(".bin.part")
    log(f"downloading whisper model '{name}' from huggingface.co")

    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url) as response, tmp.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            read = 0
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                read += len(chunk)
                if total and sys.stderr.isatty():
                    pct = 100 * read / total
                    print(
                        f"\r  {pct:5.1f}%  {read / 1e6:,.0f} / {total / 1e6:,.0f} MB",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )
        if sys.stderr.isatty():
            print("", file=sys.stderr)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        die(f"could not download model '{name}': {exc}\n  tried: {url}")

    expected = KNOWN_DIGESTS.get(name)
    if expected and digest.hexdigest() != expected:
        tmp.unlink(missing_ok=True)
        die(
            f"checksum mismatch for model '{name}'.\n"
            f"  expected {expected}\n  got      {digest.hexdigest()}"
        )

    tmp.replace(target)
    log(f"model ready: {target}")
    return target


def _parse_whisper_json(path: Path) -> list[dict]:
    """whisper.cpp JSON -> [{start, end, text}] with times in seconds."""
    blob = json.loads(path.read_text(encoding="utf-8"))
    segments: list[dict] = []
    for item in blob.get("transcription", []):
        offsets = item.get("offsets") or {}
        text = (item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(offsets["from"]) / 1000.0
            end = float(offsets["to"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return segments


@operation("transcribe", source="audio", output="out_prefix")
def transcribe(
    audio: Path,
    model_path: Path,
    *,
    out_prefix: Path,
    language: str = "en",
    threads: int | None = None,
    extra_args: list[str] | None = None,
) -> list[dict]:
    """Run whisper.cpp over a wav file and return timestamped segments."""
    binary = whisper_bin()
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        binary,
        "--model",
        str(model_path),
        "--file",
        str(audio),
        "--output-json",
        "--output-file",
        str(out_prefix),
        "--language",
        language,
        "--print-progress",
    ]
    if threads:
        cmd += ["--threads", str(threads)]
    cmd += extra_args or []

    try:
        report_work("speech", "Transcribing audio", completed=0, total=100, unit="percent",
                    detail="Loading the model; waiting for the first progress report")
        # stderr streams through: transcription is the slow stage and progress helps.
        run(cmd, capture_stdout=True, capture_stderr=False, on_stderr=_report_progress, tee_stderr=True)
    except CommandError as exc:
        raise CommandError(f"whisper.cpp failed.\n  {exc}") from exc

    json_path = out_prefix.with_suffix(out_prefix.suffix + ".json")
    if not json_path.is_file():
        alt = Path(str(out_prefix) + ".json")
        json_path = alt if alt.is_file() else json_path
    if not json_path.is_file():
        raise CommandError(f"whisper.cpp produced no JSON at {json_path}")

    segments = _parse_whisper_json(json_path)
    report_work("speech", "Transcribing audio", completed=100, total=100, unit="percent")
    if not segments:
        warn(f"no speech found in {audio.name}")
    return segments
