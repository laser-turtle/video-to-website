"""An interrupted media stage with real DBOS/native-task checkpoint records."""

import contextlib
import sys
import time
from pathlib import Path
from unittest.mock import patch

from video_to_website import media, steps, whisper
from video_to_website.compute import operation
from video_to_website.durable import DurableWorker
from video_to_website.pipeline import BuildOptions


def main():
    root = Path(sys.argv[1])
    model = root / "model.bin"; model.touch()

    @operation("clip", output="out_path")
    def clip(video, start, out_path, **kwargs):
        with (root / "clip-calls").open("a") as log:
            log.write(out_path.name + "\n")
        if out_path.name == "step-002.mp4":
            (root / "entered-second-clip").touch()
            deadline = time.monotonic() + 30
            while not (root / "release-second-clip").exists():
                if time.monotonic() > deadline: raise RuntimeError("Fixture timed out")
                time.sleep(.05)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fixture clip")
        return out_path

    lesson = {"title": "Tutorial", "summary": "Summary", "prerequisites": [], "steps": [
        {"start": n, "end": n + 1, "title": f"Step {n + 1}", "actions": ["Create a cube"], "motion": True}
        for n in range(2)]}
    with contextlib.ExitStack() as stack:
        for target, name, kwargs in [
            (whisper, "resolve_model", {"return_value": model}),
            (whisper, "transcribe", {"return_value": [{"start": 0, "end": 2, "text": "Create a cube"}]}),
            (media, "probe", {"return_value": {"duration": 2, "width": 96, "height": 64, "has_audio": True}}),
            (media, "extract_audio", {}), (media, "detect_scenes", {"return_value": []}),
            (steps, "heuristic_lesson", {"return_value": lesson}),
            (media, "extract_clip", {"new": clip}), (media, "duration_of", {"return_value": 1}),
        ]:
            stack.enter_context(patch.object(target, name, **kwargs))
        options = BuildOptions(out=root / "site", work=root / "work", llm="heuristic", clips="all", shots_with_clip="none", clip_max_still=0)
        with DurableWorker(root / "library", options, root / "state") as coordinator:
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                coordinator.tick()
                states = coordinator.catalog.rows("SELECT state FROM builds")
                if states and all(row["state"] == "ready" for row in states): return
                time.sleep(.05)
            raise RuntimeError("Media fixture did not publish")


if __name__ == "__main__":
    main()
