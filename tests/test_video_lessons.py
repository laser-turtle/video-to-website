"""Videos without instructional steps remain readable; real failures still fail."""

import contextlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from video_to_website import media, render, whisper
from video_to_website.durable import DurableWorker
from video_to_website.llm import LLMError
from video_to_website.pipeline import BuildOptions, process_video
from video_to_website.util import CommandError


class IntroBackend:
    name = 'anthropic'
    model = 'test-model'
    def complete(self, system, user):
        return json.dumps({'title': 'An overview of the chapter', 'summary': 'Meet the tools used in this chapter.',
                          'prerequisites': [], 'skip': [{'start': 0, 'end': 120, 'reason': 'introduction'}], 'steps': []})


class VideoLessonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / 'library'
        self.video = self.library / 'Course' / '9.01 - Chapter Introduction.mp4'
        self.video.parent.mkdir(parents=True); self.video.write_bytes(b'original video')
        self.options = BuildOptions(out=self.root / 'site', work=self.root / 'work', llm='anthropic', llm_model='test-model', clips='none')
        self.backend = IntroBackend()
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.probe = self.stack.enter_context(patch.object(media, 'probe', return_value={'duration': 133.65, 'width': 1920, 'height': 1080, 'has_audio': True}))
        self.audio = self.stack.enter_context(patch.object(media, 'extract_audio'))
        self.transcribe = self.stack.enter_context(patch.object(whisper, 'transcribe', return_value=[{'start': 0., 'end': 10., 'text': 'Welcome to the chapter.'}]))
        self.scenes = self.stack.enter_context(patch.object(media, 'detect_scenes', return_value=[]))
        self.image = self.stack.enter_context(patch.object(media, 'extract_frame', side_effect=self.poster))
        self.stack.enter_context(patch.object(whisper, 'resolve_model', return_value=self.root / 'model.bin'))
        self.stack.enter_context(patch('video_to_website.durable.make_backend', return_value=self.backend))

    @staticmethod
    def poster(video, timestamp, target, **kwargs):
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b'poster')

    def process(self, **kwargs):
        return process_video(self.video, work_dir=self.root / 'work', course_dir=self.root / 'site' / 'course',
                             slug='intro', options=self.options, backend=self.backend, model_path=self.root / 'model.bin', **kwargs)

    def test_successful_empty_steps_publish_video_and_reuse_legacy_instruction_cache(self):
        stages = []
        def execute(name, fn): stages.append(name); return fn()
        lesson = self.process(run_stage=execute)
        self.assertTrue(lesson['video_only'])
        self.assertEqual(lesson['steps'], [])
        self.assertEqual(lesson['summary'], 'Meet the tools used in this chapter.')
        self.assertEqual(len(lesson['transcript']), 1)
        self.assertEqual(stages, ['source', 'probe', 'transcribe', 'scenes', 'steps', 'frames', 'assemble'])
        self.assertTrue((self.root / 'site' / 'course' / lesson['poster']).is_file())
        self.assertTrue((self.root / 'site' / 'course' / lesson['video_href']).is_file())
        # Older jobs already saved steps.json before reporting this failure.
        with patch.object(self.backend, 'complete', side_effect=AssertionError('must reuse instructions')):
            self.assertTrue(self.process()['video_only'])
        self.assertEqual(self.transcribe.call_count, 1)
        self.assertEqual(self.image.call_count, 1)

    def test_no_audio_or_empty_transcript_skip_unneeded_extraction_stages(self):
        self.probe.return_value['has_audio'] = False
        with patch.object(self.backend, 'complete', side_effect=AssertionError('no transcript needs no model')):
            lesson = self.process()
        self.assertTrue(lesson['video_only'])
        self.audio.assert_not_called(); self.transcribe.assert_not_called(); self.scenes.assert_not_called()
        self.assertEqual(lesson['transcript'], [])
        self.options.force = {'probe', 'transcribe'}
        self.probe.return_value['has_audio'] = True
        self.transcribe.return_value = []
        with patch.object(self.backend, 'complete', side_effect=AssertionError('no transcript needs no model')):
            self.assertTrue(self.process()['video_only'])
        self.scenes.assert_not_called()

    def test_corrupt_media_and_processing_errors_do_not_become_successes(self):
        for duration, width in [(0, 1920), (float('nan'), 1920), (float('inf'), 1920), (10, 0), (10, -1)]:
            self.options.force = {'probe'}
            self.probe.return_value.update(duration=duration, width=width)
            with self.subTest(duration=duration, width=width): self.assertIsNone(self.process())
        self.probe.return_value.update(duration=133.65, width=1920)
        with patch.object(self.backend, 'complete', side_effect=LLMError('unavailable')):
            with self.assertRaises(LLMError): self.process()
        self.image.side_effect = CommandError('cannot decode image')
        with self.assertRaises(CommandError): self.process()

    def test_video_page_and_export_controls_match_the_available_content(self):
        lesson = self.process()
        course = {'slug': 'course', 'title': 'Course', 'lessons': [lesson]}
        page = render.render_lesson_page(lesson, course)
        self.assertIn('id="lesson-video" controls', page)
        self.assertIn('width="1920" height="1080"', page)
        self.assertIn('Full transcript', page)
        self.assertIn('Meet the tools used in this chapter.', page)
        self.assertNotIn('class="progress"', page)
        self.assertNotIn('id="player"', page)
        self.assertNotIn('0 steps', page)
        self.assertNotIn('Left out:', page)
        self.assertNotIn('Mark the step done', page)
        self.assertIn('Video lesson', render.render_course_page(course))
        self.assertIn('[Watch the original video](../videos/intro.mp4)', render.render_lesson_markdown(lesson))
        self.options.videos = 'none'
        exported = self.process()
        self.assertIsNone(exported['video_href'])
        page = render.render_lesson_page(exported, dict(course, lessons=[exported]))
        self.assertNotIn('<video', page)
        self.assertIn('not included in this export', page)

    def test_stop_after_keeps_its_existing_meaning(self):
        self.options.stop_after = 'steps'
        self.assertIsNone(self.process())
        self.image.assert_not_called()

    def test_retry_old_failed_job_publishes_and_durable_restart_keeps_video_lesson(self):
        with DurableWorker(self.library, self.options, self.root / 'state') as worker:
            worker.catalog.reconcile(self.library, worker.serialized)
            old = worker.catalog.rows('SELECT * FROM builds')[0]
            # Produce the same complete, empty steps cache left by the old code.
            from video_to_website.durable import deserialize_options, prepare_source
            spec = json.loads(old['spec']); source = prepare_source(spec)
            process_video(Path(source['video']), work_dir=Path(source['work']), course_dir=self.root / 'legacy-output',
                          slug=spec['slug'], options=deserialize_options(spec['options']), backend=self.backend, model_path=Path(source['model_path']))
            worker.catalog.update_build(old['id'], 'failed', error='video has no usable audio, transcript, or lesson steps')
            retry_id = worker.catalog.retry(old['lesson_id'])
            with patch.object(self.backend, 'complete', side_effect=AssertionError('cached introduction called the API again')):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    worker.tick()
                    if worker.catalog.build(retry_id)['state'] in {'ready', 'failed'}: break
                    time.sleep(.05)
            self.assertEqual(worker.catalog.build(retry_id)['state'], 'ready', worker.catalog.build(retry_id)['error'])
            course = worker.catalog.published_courses()[0]; lesson = course['lessons'][0]
            self.assertTrue(lesson['video_only'])
            self.assertIn('_sources/', lesson['video_href'])
            self.assertIn('_revisions/', lesson['poster'])
            self.assertTrue((self.options.out / course['slug'] / lesson['poster']).is_file())
            self.assertIn('id="lesson-video"', (self.options.out / course['slug'] / (lesson['slug'] + '.html')).read_text())
            self.assertEqual(self.transcribe.call_count, 1)
        with DurableWorker(self.library, self.options, self.root / 'state') as worker:
            worker.tick()
            self.assertEqual(worker.catalog.build(retry_id)['state'], 'ready')
            self.assertEqual(len(worker.catalog.published_courses()[0]['lessons']), 1)


if __name__ == '__main__': unittest.main()
