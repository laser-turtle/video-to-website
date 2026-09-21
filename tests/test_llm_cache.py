"""Section checkpoints supplement, rather than invalidate, existing caches."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_to_website import steps
from video_to_website.llm import LLMError, ProviderBlocked
from video_to_website.llm_cache import read_section, request_key, write_section
from video_to_website.pipeline import BuildOptions, process_video
from test_pipeline import MockBackend, make_segments


class SectionCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.kwargs = dict(title='Lesson', duration=220, segments=make_segments(220), scene_times=[], chunk_minutes=2,
                           cache_dir=self.root / 'sections', cache_context={'source': 'original'})

    def test_credit_failure_reuses_successful_sections_and_matches_uninterrupted_output(self):
        backend = MockBackend()
        original = backend.complete
        count = 0
        def complete(*args):
            nonlocal count
            count += 1
            if count == 2: raise ProviderBlocked('Out of credits')
            return original(*args)
        with patch.object(backend, 'complete', side_effect=complete), self.assertRaises(ProviderBlocked):
            steps.extract_lesson(backend, **self.kwargs)
        self.assertEqual(len(list(self.kwargs['cache_dir'].glob('*.json'))), 1)
        backend = MockBackend()
        recovered = steps.extract_lesson(backend, **self.kwargs)
        direct = MockBackend()
        expected = steps.extract_lesson(direct, **dict(self.kwargs, cache_dir=None))
        self.assertEqual(recovered, expected)
        self.assertEqual(len(backend.calls), len(direct.calls) - 1)
        with patch.object(backend, 'complete', side_effect=AssertionError('must reuse sections')):
            self.assertEqual(steps.extract_lesson(backend, **self.kwargs), expected)

    def test_model_request_prompt_and_source_changes_cannot_reuse_wrong_sections(self):
        backend = MockBackend()
        base = request_key(backend, 'system', 'user', {'source': 1})
        variants = [request_key(backend, 'other', 'user', {'source': 1}),
                    request_key(backend, 'system', 'other', {'source': 1}),
                    request_key(backend, 'system', 'user', {'source': 2})]
        backend.model = 'another-model'
        variants.append(request_key(backend, 'system', 'user', {'source': 1}))
        self.assertTrue(all(key != base for key in variants))
        steps.extract_lesson(backend, **self.kwargs)
        next_backend = MockBackend()
        steps.extract_lesson(next_backend, **self.kwargs)
        self.assertEqual(len(next_backend.calls), 3)

    def test_corrupt_partial_or_unusable_results_are_not_accepted(self):
        directory = self.kwargs['cache_dir']; directory.mkdir()
        key = 'abc'; path = directory / (key + '.json')
        for content in ['{partial', '[]', '{}', json.dumps({'version': 1, 'request': key, 'result': {'steps': []}, 'checksum': 'bad'})]:
            path.write_text(content)
            self.assertIsNone(read_section(directory, key))
        for result in [[], {'message': 'oops'}, {'steps': 'invalid'}]:
            path.unlink(missing_ok=True); write_section(directory, key, result)
            self.assertFalse(path.exists())
        write_section(directory, key, {'steps': [], 'skip': [{'start': 0, 'end': 60}]})
        self.assertIsNotNone(read_section(directory, key), 'an intentionally skipped section is valid')

    def test_repaired_response_is_saved_and_no_cache_api_remains_compatible(self):
        backend = MockBackend(); original = backend.complete
        count = 0
        def complete(system, user):
            nonlocal count
            count += 1
            return 'broken JSON' if count == 1 else original(system, user)
        with patch.object(backend, 'complete', side_effect=complete):
            result = steps.extract_lesson(backend, **self.kwargs)
        self.assertEqual(count, 4)
        with patch.object(backend, 'complete', side_effect=LLMError('offline')):
            self.assertEqual(steps.extract_lesson(backend, **self.kwargs), result)

    def test_pipeline_whole_stage_cache_and_force_semantics(self):
        video = self.root / 'video.mp4'; video.write_bytes(b'video')
        backend = MockBackend()
        options = BuildOptions(out=self.root / 'site', llm='anthropic', chunk_minutes=2, clips='none')
        def frame(video, timestamp, target, **kwargs): target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b'image')
        from video_to_website import media, whisper
        with patch.object(media, 'probe', return_value={'duration': 220, 'width': 100, 'height': 100, 'has_audio': True}), \
                patch.object(media, 'extract_audio'), patch.object(whisper, 'transcribe', return_value=make_segments(220)), \
                patch.object(media, 'detect_scenes', return_value=[]), patch.object(media, 'frame_hash', return_value=None), \
                patch.object(media, 'extract_frame', side_effect=frame):
            def run(stage_runner=None):
                return process_video(video, work_dir=self.root / 'work', course_dir=self.root / 'staging' / 'build-id',
                    slug='lesson', options=options, backend=backend, model_path=self.root / 'model', run_stage=stage_runner)
            self.assertIsNotNone(run())
            self.assertEqual(len(backend.calls), 3)
            # A pre-feature whole-stage cache has the same key and stays valid,
            # even with all supplementary section files missing.
            import shutil
            shutil.rmtree(self.root / 'work' / 'llm-sections')
            with patch.object(backend, 'complete', side_effect=AssertionError('existing stage cache invalidated')):
                self.assertIsNotNone(run())
            options.force = {'steps'}
            run(); self.assertEqual(len(backend.calls), 6)
            run(); self.assertEqual(len(backend.calls), 9, 'standalone force is fresh each run')
            runner = lambda name, fn: fn()
            run(runner); self.assertEqual(len(backend.calls), 12)
            run(runner); self.assertEqual(len(backend.calls), 12, 'same durable forced build reuses its own completed sections')


if __name__ == '__main__': unittest.main()
