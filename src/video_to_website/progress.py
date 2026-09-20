"""What the builder is doing right now, written where the site can read it.

The site is static files behind a web server, so progress has to travel the
same way the pages do: the builder writes a small JSON file into the output
directory and the index page polls it. Whisper alone runs for minutes per
video, so a library that sits there looking empty and idle during a build is
the most confusing thing this tool does.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .util import title_from_filename

STATUS_NAME = "status.json"

# Shown on the page while each stage runs, in the order the pipeline runs them.
STAGE_LABELS = {
    "probe": "Reading the file",
    "transcribe": "Transcribing the audio",
    "scenes": "Finding on-screen changes",
    "steps": "Writing the steps",
    "frames": "Cutting screenshots and clips",
}


class Progress:
    """Per-video build state, kept in step with a JSON file beside the site.

    A null site_dir makes every method a no-op, which is what `--stop-after`
    runs and the tests want.
    """

    def __init__(self, site_dir: Path | None, *, stages: list[str] | None = None):
        self.site_dir = site_dir
        self.stages = [s for s in (stages or list(STAGE_LABELS)) if s in STAGE_LABELS]
        self.videos: list[dict] = []
        self.current: dict | None = None
        self.building = False
        self.error: str | None = None
        self.retry_in: float | None = None
        # Epoch of the last finished build. The page reloads when this moves,
        # which is how a course that finishes appears without a manual refresh.
        # Read back from any existing file so a service restart does not look
        # like a fresh build to a browser that is already open.
        self.built = self._previous_built()

    # -- recording ----------------------------------------------------------

    def plan(self, courses: list[dict]) -> None:
        """Queue every video the build is about to walk through."""
        self.videos = [
            self._new_entry(video, course["title"], "Waiting")
            for course in courses
            for video in course["videos"]
        ]
        self.building = True
        self.error = None
        self.retry_in = None
        self.write()

    def queue(self, videos: list[Path], label: str) -> None:
        """Show files that have turned up but are not being built yet.

        The watcher waits a full poll before touching a new file, in case it is
        still being copied in. That wait is exactly when someone who just
        dropped a video goes looking at the page, so say what is happening.
        """
        self.videos = [self._new_entry(video, video.parent.name, label) for video in videos]
        self.building = False
        self.write()

    def start(self, video: Path) -> None:
        self.current = self._entry(video)
        if self.current is not None:
            self.current["state"] = "working"
            self.current["started"] = time.time()
        self.write()

    def stage(self, name: str) -> None:
        if self.current is None:
            return
        self.current["stage"] = name
        self.current["label"] = STAGE_LABELS.get(name, name)
        self.current["step"] = self.stages.index(name) + 1 if name in self.stages else 0
        self.write()

    def finish(self, state: str = "done") -> None:
        if self.current is not None:
            self.current["state"] = state
            self.current["label"] = {
                "done": "Done",
                "failed": "Failed",
                "skipped": "Skipped",
            }.get(state, state)
            self.current["step"] = len(self.stages) if state == "done" else self.current["step"]
            self.current = None
        self.write()

    def finish_build(self) -> None:
        self.building = False
        self.built = time.time()
        for video in self.videos:
            if video["state"] in ("queued", "working"):
                video["state"] = "done"
                video["label"] = "Done"
        self.write()

    def fail_build(self, message: str, *, retry_in: float | None = None) -> None:
        """A build that stopped part way through, rather than one that finished.

        Deliberately does not move `built`: the pages on disk are whatever the
        last good build left, so nothing that is reading them should reload.
        Videos still queued stay queued, because the retry will get to them.
        """
        self.building = False
        self.error = message
        self.retry_in = retry_in
        if self.current is not None:
            self.current["state"] = "failed"
            self.current["label"] = "Failed"
            self.current = None
        self.write()

    # -- writing ------------------------------------------------------------

    def snapshot(self) -> dict:
        now = time.time()
        videos = []
        for video in self.videos:
            entry = dict(video)
            if entry["started"] and entry["state"] == "working":
                entry["elapsed"] = round(now - entry["started"], 1)
            videos.append(entry)
        return {
            "updated": now,
            "building": self.building,
            "built": self.built,
            "error": self.error,
            "retry_in": self.retry_in,
            "videos": videos,
        }

    def write(self) -> None:
        if self.site_dir is None:
            return
        self.site_dir.mkdir(parents=True, exist_ok=True)
        path = self.site_dir / STATUS_NAME
        # Written mid-build and polled from a browser, so it goes in atomically
        # rather than being caught half-serialised.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.snapshot(), indent=2))
        tmp.replace(path)

    # -- internals ----------------------------------------------------------

    def _new_entry(self, video: Path, course: str, label: str) -> dict:
        return {
            "id": str(video),
            "course": course,
            "title": title_from_filename(video),
            "state": "queued",
            "stage": None,
            "label": label,
            "step": 0,
            "steps": len(self.stages),
            "started": None,
            "elapsed": 0.0,
        }

    def _entry(self, video: Path) -> dict | None:
        key = str(video)
        for entry in self.videos:
            if entry["id"] == key:
                return entry
        return None

    def _previous_built(self) -> float:
        if self.site_dir is None:
            return 0.0
        try:
            data = json.loads((self.site_dir / STATUS_NAME).read_text())
            return float(data.get("built") or 0.0)
        except (OSError, ValueError, TypeError):
            return 0.0
