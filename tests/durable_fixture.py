"""A real DBOS worker with inexpensive media adapters, for crash/restart tests."""

import contextlib
import sys
import time
from pathlib import Path
from unittest.mock import patch

from video_to_website import media, whisper
from video_to_website.durable import DurableWorker
from video_to_website.pipeline import BuildOptions


def main():
    root = Path(sys.argv[1])
    model = root / "model.bin"
    model.touch()

    def transcribe(*args, **kwargs):
        with (root / "transcriptions").open("a") as f:
            f.write("transcribe\n")
        return [{"start": 0.0, "end": 10.0, "text": "Create the first object."}]

    def scenes(*args, **kwargs):
        (root / "entered-scenes").touch()
        deadline = time.monotonic() + 30
        while not (root / "release-scenes").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("test gate timed out")
            time.sleep(0.05)
        return []

    def image(video, timestamp, target, **kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test image")

    with contextlib.ExitStack() as stack:
        for obj, attr, kwargs in [
            (whisper, "resolve_model", {"return_value": model}),
            (whisper, "transcribe", {"side_effect": transcribe}),
            (
                media,
                "probe",
                {
                    "return_value": {
                        "duration": 10.0,
                        "width": 100,
                        "height": 100,
                        "has_audio": True,
                    }
                },
            ),
            (media, "extract_audio", {}),
            (media, "detect_scenes", {"side_effect": scenes}),
            (media, "frame_hash", {"return_value": None}),
            (media, "extract_frame", {"side_effect": image}),
        ]:
            stack.enter_context(patch.object(obj, attr, **kwargs))
        options = BuildOptions(
            out=root / "site", work=root / "work", llm="heuristic", clips="none"
        )
        with DurableWorker(root / "library", options, root / "state") as worker:
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                worker.tick()
                states = worker.catalog.rows("SELECT state FROM builds")
                if states and all(r["state"] == "ready" for r in states):
                    return
                time.sleep(0.05)
            raise RuntimeError("worker did not publish")


if __name__ == "__main__":
    main()
