"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__, render
from .ingest import IngestHandler, IngestMixin
from .pipeline import STAGE_ORDER, BuildOptions, build
from .progress import Progress
from .util import die, find_videos, human_duration, log, warn


def _add_build_options(parser: argparse.ArgumentParser) -> None:
    """Options shared by `build` and `watch`."""
    parser.add_argument("-o", "--out", type=Path, default=Path("site"), help="output directory (default: site)")
    parser.add_argument("--work", type=Path, help="cache directory (default: <out>/.work)")
    parser.add_argument("--model", default=os.environ.get("V2W_MODEL", "small.en"), help="whisper model name or path (default: small.en)")
    parser.add_argument("--language", default="en", help="spoken language, or 'auto' (default: en)")
    parser.add_argument("--threads", type=int, help="whisper threads (default: whisper.cpp decides)")
    parser.add_argument(
        "--llm",
        default=os.environ.get("V2W_LLM", "auto"),
        choices=["auto", "claude-cli", "anthropic", "ollama", "codex-cli", "heuristic"],
        help="how steps get written (default: auto)",
    )
    parser.add_argument("--llm-model", help="override the model for the chosen backend")
    parser.add_argument(
        "--no-llm-fallbacks",
        action="store_true",
        help="disable Anthropic server-side refusal fallbacks",
    )
    parser.add_argument("--chunk-minutes", type=float, default=25.0, help="transcript minutes per request (default: 25)")
    parser.add_argument("--scene-threshold", type=float, default=0.03, help="screenshot sensitivity, lower finds more (default: 0.03)")
    parser.add_argument("--frame-width", type=int, default=1920, help="screenshot width, never upscaled (default: 1920)")
    parser.add_argument("--frame-quality", type=int, default=3, help="screenshot JPEG quality, 2 is finest, 31 coarsest (default: 3)")
    parser.add_argument("--clip-width", type=int, default=1920, help="clip width, never upscaled (default: 1920)")
    parser.add_argument("--clip-crf", type=int, default=24, help="clip quality, lower is better (default: 24)")
    parser.add_argument("--frames-per-step", type=int, default=4, help="most screenshots to show per step (default: 4)")
    parser.add_argument(
        "--screenshot-every",
        type=float,
        default=15.0,
        metavar="SECONDS",
        help="aim for one screenshot per this many seconds of a step (default: 15)",
    )
    parser.add_argument(
        "--clips",
        default="auto",
        choices=["auto", "all", "none"],
        help="short looping clips for steps that are movement (default: auto)",
    )
    parser.add_argument(
        "--clip-max-seconds",
        type=float,
        default=45.0,
        help="longest a clip may run; it otherwise covers its whole step (default: 45)",
    )
    parser.add_argument("--clip-fps", type=int, default=30, help="clip frame rate (default: 30)")
    parser.add_argument(
        "--clip-max-still",
        type=float,
        default=1.5,
        metavar="SECONDS",
        help="longest motionless stretch a clip keeps; 0 disables trimming (default: 1.5)",
    )
    parser.add_argument(
        "--shots-with-clip",
        default="one",
        choices=["one", "all", "none"],
        help="screenshots to keep on a step that also has a clip (default: one)",
    )
    parser.add_argument(
        "--frame-dedup-distance",
        type=int,
        default=16,
        help="how similar two screenshots may be before one is dropped, 0-256 (default: 16)",
    )
    parser.add_argument(
        "--videos",
        default="symlink",
        choices=["symlink", "copy", "none"],
        help="how the source video reaches the site (default: symlink)",
    )
    parser.add_argument("--no-markdown", action="store_true", help="skip the markdown export")
    parser.add_argument("--keep-audio", action="store_true", help="keep extracted wav files")
    parser.add_argument(
        "--force",
        action="append",
        default=[],
        choices=STAGE_ORDER + ["all"],
        help="recompute a stage even when cached (repeatable)",
    )
    parser.add_argument("--stop-after", choices=STAGE_ORDER, help="stop the pipeline after this stage")



def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="v2w",
        description="Turn tutorial videos into an illustrated, timestamp-linked website.",
    )
    parser.add_argument("--version", action="version", version=f"video-to-website {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    build_cmd = sub.add_parser("build", help="build a site from video files or course folders")
    build_cmd.add_argument("paths", nargs="+", type=Path, help="video files, or folders of them")
    build_cmd.add_argument(
        "--prune",
        action="store_true",
        help="delete output for courses and lessons not named here (only safe when these paths are the whole library)",
    )
    _add_build_options(build_cmd)

    watch_cmd = sub.add_parser(
        "watch", help="rebuild whenever the library changes, for running as a service"
    )
    watch_cmd.add_argument("library", type=Path, help="directory of course folders to watch")
    watch_cmd.add_argument("--interval", type=float, default=30.0, help="seconds between polls (default: 30)")
    watch_cmd.add_argument(
        "--api-port",
        type=int,
        help="also accept browser uploads into the library on this port",
    )
    watch_cmd.add_argument("--api-bind", default="127.0.0.1", help="address for --api-port (default: 127.0.0.1)")
    _add_build_options(watch_cmd)

    fetch_cmd = sub.add_parser("fetch-model", help="download a whisper.cpp model")
    fetch_cmd.add_argument("names", nargs="*", help="model names, e.g. small.en")
    fetch_cmd.add_argument("--list", action="store_true", help="list suggested models and exit")
    fetch_cmd.add_argument("--dir", type=Path, help="destination directory")

    serve_cmd = sub.add_parser("serve", help="serve a built site over http")
    serve_cmd.add_argument("directory", nargs="?", type=Path, default=Path("site"))
    serve_cmd.add_argument("-p", "--port", type=int, default=8000)
    serve_cmd.add_argument("--bind", default="127.0.0.1", help="address to bind (default: 127.0.0.1)")
    serve_cmd.add_argument(
        "--library",
        type=Path,
        help="accept browser uploads into this directory, as the service does",
    )
    serve_cmd.add_argument(
        "--work",
        type=Path,
        help="stage cache to keep in step when a video is renamed (default: <directory>/.work)",
    )

    sub.add_parser("doctor", help="check that external tools and credentials are in place")
    return parser


def _options_from(args: argparse.Namespace) -> BuildOptions:
    return BuildOptions(
        out=args.out.expanduser(),
        work=args.work.expanduser() if args.work else None,
        model=args.model,
        language=args.language,
        threads=args.threads,
        llm=args.llm,
        llm_model=args.llm_model,
        llm_fallbacks=not args.no_llm_fallbacks,
        chunk_minutes=args.chunk_minutes,
        scene_threshold=args.scene_threshold,
        frame_width=args.frame_width,
        frame_quality=max(2, min(31, args.frame_quality)),
        clip_width=args.clip_width,
        clip_crf=max(0, min(51, args.clip_crf)),
        frames_per_step=max(1, args.frames_per_step),
        seconds_per_frame=max(1.0, args.screenshot_every),
        clips=args.clips,
        clip_max_seconds=max(2.0, args.clip_max_seconds),
        clip_fps=max(5, args.clip_fps),
        clip_max_still=max(0.0, args.clip_max_still),
        shots_with_clip=args.shots_with_clip,
        frame_dedup_distance=args.frame_dedup_distance,
        videos=args.videos,
        markdown=not args.no_markdown,
        keep_audio=args.keep_audio,
        force=set(args.force),
        stop_after=args.stop_after,
    )


def _backend_for(options: BuildOptions):
    if not options.wants("steps") or options.llm == "heuristic":
        return None
    from .llm import make_backend

    return make_backend(options.llm, options.llm_model, fallbacks=options.llm_fallbacks)


def _cmd_build(args: argparse.Namespace) -> int:
    options = _options_from(args)
    backend = _backend_for(options)

    courses = build(
        args.paths,
        options,
        backend,
        Progress(options.out if options.wants("render") else None),
        prune=args.prune,
    )
    if not courses:
        # Stopping early on purpose is not a failure; it just produces no lessons.
        if options.stop_after and options.stop_after != "render":
            log(f"stopped after the {options.stop_after} stage")
            return 0
        return 1

    lessons = [lesson for course in courses for lesson in course["lessons"]]
    total_steps = sum(len(lesson["steps"]) for lesson in lessons)
    total_time = sum(lesson["duration"] for lesson in lessons)
    log(
        f"done: {len(courses)} courses, {len(lessons)} lessons, {total_steps} steps "
        f"from {human_duration(total_time)} of video"
    )
    if options.wants("render"):
        log(f"open {options.out / 'index.html'} or run: v2w serve {options.out}")
    return 0


def _library_state(library: Path) -> tuple:
    """What the library looks like right now, for spotting changes."""
    state = []
    for video in find_videos(library):
        try:
            stat = video.stat()
        except OSError:
            continue
        state.append((str(video), stat.st_size, stat.st_mtime_ns))
    return tuple(state)


def _watch_decision(state: tuple, seen: tuple | None, done: tuple | None) -> str:
    """What to do with the library as it looks right now.

    A file is often still being copied in when it first appears, so a change is
    only acted on once it has stayed the same for a full poll.
    """
    if state != seen:
        return "settle"
    if state and state != done:
        return "build"
    return "idle"


# An hour between attempts is often enough to catch a service coming back, and
# rare enough to be no kind of load if it does not.
_MAX_BACKOFF = 3600.0


def _retry_delay(failures: int, interval: float) -> float:
    """How long to wait before trying a failed build again.

    Doubling, because what usually fails here is something that needs time --
    an API refusing, a disk filling, a network down -- and hammering it every
    poll neither helps nor is free when the backend charges per call.
    """
    return min(interval * 2**max(1, failures), _MAX_BACKOFF)


def _start_api(library: Path, bind: str, port: int, work: Path | None = None) -> None:
    """Take uploads on a side port while the watcher keeps polling.

    A daemon thread rather than a second process: an upload is finished the
    moment the file is in the library, and the loop in the main thread finds it
    on its next pass with no coordination between the two at all.
    """
    import http.server
    import threading

    http.server.ThreadingHTTPServer.allow_reuse_address = True
    httpd = http.server.ThreadingHTTPServer((bind, port), IngestHandler)
    httpd.library = library
    # So a rename can carry the stage cache with it rather than orphaning it.
    httpd.work = work
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"accepting uploads on http://{bind}:{port}/api/")


def _cmd_watch(args: argparse.Namespace) -> int:
    """Rebuild whenever the library settles after a change.

    Polling rather than inotify: a video appearing is not latency-critical, and
    it keeps this to the standard library and works the same on any filesystem,
    including the network shares a video is likely to arrive over.
    """
    library = args.library.expanduser()
    library.mkdir(parents=True, exist_ok=True)
    options = _options_from(args)
    backend = _backend_for(options)

    # Before anything is built the site directory is empty, and a web server
    # pointed at it serves 403 rather than anything explanatory. Say so.
    if not (options.out / "index.html").exists():
        render.write_placeholder(options.out, f"Nothing built yet. Drop a course folder in {library}.")
    progress = Progress(options.out)
    if args.api_port:
        _start_api(library, args.api_bind, args.api_port, options.work or (options.out / ".work"))

    log(f"watching {library} every {args.interval:.0f}s, writing to {options.out}")
    seen: tuple | None = None
    done: tuple | None = None
    failures = 0
    retry_at = 0.0

    while True:
        state = _library_state(library)
        decision = _watch_decision(state, seen, done)
        seen = state
        if decision == "settle":
            if state:
                log(f"library changed: {len(state)} videos, waiting for it to settle")
                progress.queue([Path(path) for path, _, _ in state], "Waiting for the copy to finish")
            # Whatever the library just did, give it another go straight away.
            failures, retry_at = 0, 0.0
        elif decision == "build" and time.time() >= retry_at:
            try:
                # The watcher always sees the whole library, so it is the one
                # caller that can tell a removed course from an unmentioned one.
                build([library], options, backend, progress, prune=True)
                done = state
                failures = 0
            except SystemExit:
                raise
            except Exception as exc:  # a bad video must not take the service down
                # `done` is deliberately not advanced: the build gets retried
                # rather than waiting for someone to touch the library. Most of
                # what fails here is transient -- the API, the network, the
                # disk -- and the stage cache means a retry resumes rather than
                # starting over. Backing off so a permanent failure is not an
                # endless loop of API calls.
                failures += 1
                delay = _retry_delay(failures, args.interval)
                retry_at = time.time() + delay
                warn(f"build failed, retrying in {human_duration(delay)}: {exc}")
                progress.fail_build(str(exc), retry_in=delay)
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("", file=sys.stderr)
            return 0


def _cmd_fetch_model(args: argparse.Namespace) -> int:
    from .whisper import SUGGESTED_MODELS, fetch_model, model_dir

    if args.list or not args.names:
        print("Suggested whisper models (larger is more accurate and slower):")
        for name in SUGGESTED_MODELS:
            print(f"  {name}")
        print(f"\nDownloads go to: {args.dir or model_dir()}")
        return 0
    for name in args.names:
        fetch_model(name, dest_dir=args.dir.expanduser() if args.dir else None)
    return 0


class _RangeHandler:
    """Mixin adding HTTP Range support, which video seeking depends on.

    http.server's handler answers every request with the whole file, so without
    this a click on a timestamp would pull the entire video before it could seek.
    """

    protocol_version = "HTTP/1.1"

    def end_headers(self):  # noqa: D102
        self.send_header("Accept-Ranges", "bytes")
        # Always revalidate while iterating locally: a stale script is a
        # confusing way to find out the page was rebuilt.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _parse_range(self, size: int):
        header = self.headers.get("Range", "")
        if not header.startswith("bytes="):
            return None
        first, _, last = header[6:].partition("-")
        try:
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            elif last:  # suffix form: bytes=-500
                start, end = max(0, size - int(last)), size - 1
            else:
                return None
        except ValueError:
            return None
        if start >= size or start > end:
            return None
        return start, min(end, size - 1)

    def send_head(self):  # noqa: D102
        import os

        if "Range" not in self.headers:
            return super().send_head()
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            handle = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        with handle:
            size = os.fstat(handle.fileno()).st_size
            span = self._parse_range(size)
            if span is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None

            start, end = span
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()

            handle.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser seeked elsewhere; normal for video
        return None


def _cmd_serve(args: argparse.Namespace) -> int:
    import functools
    import http.server

    directory = args.directory.expanduser().resolve()
    if not (directory / "index.html").exists():
        die(f"no index.html in {directory}. Run `v2w build` first.")

    class Handler(IngestMixin, _RangeHandler, http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if not self.api_get():
                super().do_GET()

    handler = functools.partial(Handler, directory=str(directory))
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer((args.bind, args.port), handler) as httpd:
        httpd.library = args.library.expanduser().resolve() if args.library else None
        httpd.work = (args.work.expanduser().resolve() if args.work else directory / ".work")
        log(f"serving {directory} at http://{args.bind}:{args.port}  (ctrl-c to stop)")
        if httpd.library:
            httpd.library.mkdir(parents=True, exist_ok=True)
            log(f"uploads land in {httpd.library}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("", file=sys.stderr)
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    from .llm import DEFAULT_OLLAMA_HOST, _anthropic_credentials_present, _ollama_reachable
    from .whisper import model_dir

    ok = True

    def check(label: str, value: str | None, *, required: bool = True) -> None:
        nonlocal ok
        mark = "ok  " if value else ("MISS" if required else "--  ")
        print(f"[{mark}] {label}: {value or 'not found'}")
        if required and not value:
            ok = False

    check("ffmpeg", shutil.which("ffmpeg"))
    check("ffprobe", shutil.which("ffprobe"))
    check("whisper-cli", shutil.which(os.environ.get("V2W_WHISPER_BIN", "whisper-cli")))

    models = sorted(p.name for p in model_dir().glob("ggml-*.bin")) if model_dir().exists() else []
    injected = os.environ.get("V2W_MODEL_PATH") or ""
    check(
        "whisper models",
        ", ".join(models) or (injected if injected else None),
        required=False,
    )
    print(f"       model dir: {model_dir()}")

    try:
        import anthropic  # noqa: F401

        sdk = "installed"
    except ImportError:
        sdk = None
    check("anthropic sdk", sdk, required=False)
    check("anthropic credentials", "present" if _anthropic_credentials_present() else None, required=False)
    check("ollama", DEFAULT_OLLAMA_HOST if _ollama_reachable() else None, required=False)

    print()
    if ok:
        print("Required tools are present.")
    else:
        print("Missing required tools. Run inside `nix develop` from this repo.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {
        "build": _cmd_build,
        "watch": _cmd_watch,
        "fetch-model": _cmd_fetch_model,
        "serve": _cmd_serve,
        "doctor": _cmd_doctor,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
