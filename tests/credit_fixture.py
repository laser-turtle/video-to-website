"""Real DBOS recovery with a fake provider and cheap media adapters.

The legacy phase can run under an actual older source tree (V2W_LEGACY_SRC in
the parent test). In normal checks it disables the two new behaviors to create
the same pre-upgrade checkpoint history and unsaved, interrupted LLM stage.
"""

import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import patch

from video_to_website import durable, media, steps, whisper
from video_to_website.pipeline import BuildOptions


def main():
    root = Path(sys.argv[1])
    legacy = sys.argv[2] == 'legacy'
    model = root / 'model.bin'; model.touch()
    def record(text):
        with (root / 'calls').open('a') as out: out.write(text + '\n')
    class Backend:
        name, model = 'anthropic', 'test-model'
        def complete(self, system, user):
            start = int(re.search(r'given seconds (\d+)', user)[1])
            record(('legacy' if legacy else 'current') + ':' + str(start))
            if legacy and start == 75:
                (root / 'legacy-in-flight').touch()
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline: time.sleep(.05)
                raise RuntimeError('legacy test request was not interrupted')
            if not legacy and start == 75 and not (root / 'funded').exists():
                from video_to_website.llm import ProviderBlocked
                raise ProviderBlocked('Add credits, then resume requests.')
            return json.dumps(dict(title='Cached instructions', summary='A lesson.', prerequisites=[], skip=[],
                steps=[dict(title='Make the object', start=start + 30, end=min(start + 50, 220), actions=['Create a mesh'], motion=False)]))
    def transcribe(*args, **kwargs):
        record('transcribe')
        return [dict(start=t, end=t+5, text='Create the mesh.') for t in range(0, 220, 5)]
    def image(video, timestamp, target, **kwargs):
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b'image')
    def legacy_complete(self, system, user):
        # Frozen pre-feature LazyBackend behavior; no billing gate.
        with durable.acquire(durable.LLM_SLOT):
            return durable.make_backend(self.name, self.model, fallbacks=self.fallbacks).complete(system, user)
    original_complete = durable.LazyBackend.complete
    def current_complete(self, system, user):
        (root / ('entered-gate-' + str(os.getpid()))).touch()
        return original_complete(self, system, user)
    original_extract = steps.extract_lesson
    def legacy_extract(*args, **kwargs):
        kwargs.pop('cache_dir', None); kwargs.pop('cache_context', None)
        return original_extract(*args, **kwargs)
    with contextlib.ExitStack() as stack:
        for obj, name, kwargs in [
            (durable, 'make_backend', dict(return_value=Backend())),
            (whisper, 'resolve_model', dict(return_value=model)),
            (whisper, 'transcribe', dict(side_effect=transcribe)),
            (media, 'probe', dict(return_value=dict(duration=220., width=100, height=100, has_audio=True))),
            (media, 'extract_audio', {}), (media, 'detect_scenes', dict(return_value=[])),
            (media, 'frame_hash', dict(return_value=None)), (media, 'extract_frame', dict(side_effect=image)),
        ]:
            stack.enter_context(patch.object(obj, name, **kwargs))
        if legacy:
            stack.enter_context(patch.object(durable.LazyBackend, 'complete', legacy_complete))
            stack.enter_context(patch.object(steps, 'extract_lesson', legacy_extract))
        else:
            stack.enter_context(patch.object(durable.LazyBackend, 'complete', current_complete))
        options = BuildOptions(out=root / 'site', work=root / 'work', llm='anthropic', llm_model='test-model',
                               chunk_minutes=2, clips='none')
        with durable.DurableWorker(root / 'library', options, root / 'state') as worker:
            deadline = time.monotonic() + 50
            while time.monotonic() < deadline:
                worker.tick()
                jobs = worker.catalog.rows('SELECT * FROM builds')
                if jobs and all(job['state'] == 'ready' for job in jobs): return
                if any(job['state'] == 'failed' for job in jobs): raise RuntimeError(str(jobs))
                time.sleep(.05)
            raise RuntimeError('test worker timed out')


if __name__ == '__main__': main()
