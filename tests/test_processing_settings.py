"""Per-lesson section length survives scans and safely creates a new attempt."""

import contextlib
import http.client
import http.server
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from video_to_website import media, whisper
from video_to_website.catalog import Catalog, CatalogConflict
from video_to_website.durable import DurableWorker, serialize_options
from video_to_website.ingest import IngestHandler
from video_to_website.llm import LLMError
from video_to_website.pipeline import BuildOptions
from video_to_website.util import file_fingerprint, write_stage


class ProcessingSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.library = self.root / 'library'
        folder = self.library / 'polygon_runway'; folder.mkdir(parents=True)
        for name in ['polygon-runway-katana', 'another-lesson']:
            (folder / (name + '.mp4')).write_bytes(name.encode())
        self.catalog = Catalog(self.root / 'state')
        self.options = serialize_options(BuildOptions(out=self.root / 'site', llm='anthropic', chunk_minutes=25))
        self.catalog.reconcile(self.library, self.options)
        self.lesson = self.catalog.rows("SELECT * FROM lessons WHERE path LIKE '%katana.mp4'")[0]
        self.old = self.catalog.build(self.lesson['desired_build'])
        self.catalog.update_build(self.old['id'], 'failed', error='response hit max_tokens before finishing')

    def configure(self, minutes, build_id=None):
        return self.catalog.configure_lesson(self.lesson['id'], {'build_id': build_id or self.old['id'], 'chunk_minutes': minutes})

    def test_setting_changes_only_this_lesson_and_preserves_old_workflow_spec(self):
        before = self.catalog.rows('SELECT * FROM builds ORDER BY id')
        new_id = self.configure(5)
        current = self.catalog.build(new_id)
        self.assertEqual(json.loads(current['spec'])['options']['chunk_minutes'], 5)
        self.assertNotEqual(current['input_key'], self.old['input_key'])
        self.assertEqual(self.catalog.build(self.old['id'])['spec'], self.old['spec'])
        self.assertTrue(all(self.catalog.build(row['id']) == row for row in before))
        self.catalog.reconcile(self.library, self.options)
        self.assertEqual(len(self.catalog.rows('SELECT * FROM builds')), len(before) + 1)
        profile = self.catalog.lesson_processing(self.lesson['id'])
        self.assertEqual(profile['chunk_minutes'], 5)
        self.assertEqual(profile['default_chunk_minutes'], 25)
        self.assertEqual(profile['custom_chunk_minutes'], 5)

    def test_override_survives_restart_rename_and_server_default_change(self):
        new_id = self.configure(10)
        source = self.library / self.lesson['path']; source.rename(source.with_name('renamed-katana.mp4'))
        self.catalog = Catalog(self.root / 'state')
        options = {**self.options, 'chunk_minutes': 15}
        self.catalog.reconcile(self.library, options)
        profile = self.catalog.lesson_processing(self.lesson['id'])
        self.assertEqual(profile['build_id'], new_id)
        self.assertEqual(profile['chunk_minutes'], 10)
        self.assertEqual(profile['default_chunk_minutes'], 15)
        self.catalog.update_build(new_id, 'failed')
        reset = self.configure(None, new_id)
        self.assertEqual(json.loads(self.catalog.build(reset)['spec'])['options']['chunk_minutes'], 15)
        self.catalog.reconcile(self.library, options)
        self.assertEqual(self.catalog.lesson_processing(self.lesson['id'])['build_id'], reset)
        self.assertIsNone(self.catalog.lesson_processing(self.lesson['id'])['custom_chunk_minutes'])

    def test_invalid_stale_active_and_foreign_settings_cannot_change_work(self):
        before = self.catalog.rows('SELECT * FROM builds ORDER BY id')
        for value in [0, .75, 121, float('nan'), float('inf'), True, '5', []]:
            with self.subTest(value=value), self.assertRaises(ValueError): self.configure(value)
        with self.assertRaises(ValueError):
            self.catalog.configure_lesson(self.lesson['id'], {'build_id': self.old['id'], 'chunk_minutes': 5, 'out': '/elsewhere'})
        with self.assertRaises(CatalogConflict): self.configure(5, 'stale')
        with self.assertRaises(KeyError): self.catalog.configure_lesson('missing', {'build_id': self.old['id'], 'chunk_minutes': 5})
        self.assertEqual(self.catalog.rows('SELECT * FROM builds ORDER BY id'), before)
        new_id = self.configure(5)
        with self.assertRaises(CatalogConflict): self.configure(10)
        with self.assertRaises(CatalogConflict): self.configure(10, new_id)
        self.assertEqual(self.catalog.lesson_processing(self.lesson['id'])['chunk_minutes'], 5)
        self.assertEqual(len(self.catalog.rows('SELECT * FROM builds')), len(before) + 1)

    def test_profile_reads_current_cached_duration_without_media_processing(self):
        spec = json.loads(self.old['spec'])
        snapshot = Path(spec['options']['out']) / '_sources' / spec['digest'] / 'source.mp4'
        snapshot.parent.mkdir(parents=True); snapshot.write_bytes(b'video')
        cache = Path(spec['options']['work']) / 'lessons' / self.lesson['id'] / spec['digest'] / 'probe.json'
        write_stage(cache, file_fingerprint(snapshot), {'stage': 'probe'}, {'duration': 480})
        self.assertEqual(self.catalog.lesson_processing(self.lesson['id'])['duration'], 480)
        cache.write_text('{broken')
        self.assertIsNone(self.catalog.lesson_processing(self.lesson['id'])['duration'])

    def test_http_settings_load_save_retry_and_conflict(self):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), IngestHandler)
        server.library, server.catalog = self.library, self.catalog
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        def request(method, body=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                conn.request(method, '/api/lessons/' + self.lesson['id'] + '/settings', json.dumps(body) if body is not None else None)
                response = conn.getresponse(); return response.status, json.loads(response.read())
            finally: conn.close()
        status, profile = request('GET'); self.assertEqual(status, 200)
        self.assertEqual(profile['chunk_minutes'], 25)
        body = {'build_id': profile['build_id'], 'chunk_minutes': 5}
        status, result = request('POST', body); self.assertEqual(status, 200)
        self.assertNotEqual(result['build_id'], self.old['id'])
        self.assertEqual(request('POST', body)[0], 409)
        self.assertEqual(request('POST', {**body, 'chunk_minutes': 0})[0], 400)


class ProcessingRecoveryTests(unittest.TestCase):
    def test_smaller_sections_fix_truncation_without_retranscribing(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp); library = root / 'library'
            video = library / 'polygon_runway' / 'polygon-runway-katana.mp4'
            video.parent.mkdir(parents=True); video.write_bytes(b'katana')
            class Backend:
                name, model = 'anthropic', 'test-model'
                def complete(self, system, user):
                    window = re.search(r'given seconds (\d+)-(\d+)', user)
                    if not window: raise LLMError('response hit max_tokens before finishing')
                    start = float(window[1])
                    return json.dumps({'title': 'Build a katana', 'steps': [{'title': 'Shape the blade', 'start': start+30,
                        'end': start+50, 'actions': ['Adjust the mesh'], 'motion': False}]})
            def image(video, timestamp, target, **kwargs): target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b'frame')
            stack.enter_context(patch('video_to_website.durable.make_backend', return_value=Backend()))
            stack.enter_context(patch.object(whisper, 'resolve_model', return_value=root / 'model.bin'))
            transcribe = stack.enter_context(patch.object(whisper, 'transcribe', return_value=[{'start': t, 'end': t+5, 'text': 'Shape the mesh.'} for t in range(0, 720, 5)]))
            scenes = stack.enter_context(patch.object(media, 'detect_scenes', return_value=[]))
            stack.enter_context(patch.object(media, 'probe', return_value={'duration': 720, 'width': 320, 'height': 200, 'has_audio': True}))
            stack.enter_context(patch.object(media, 'extract_audio'))
            stack.enter_context(patch.object(media, 'frame_hash', return_value=None))
            stack.enter_context(patch.object(media, 'extract_frame', side_effect=image))
            options = BuildOptions(out=root / 'site', work=root / 'work', llm='anthropic', llm_model='test-model', chunk_minutes=25, clips='none')
            def wait(worker, condition):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    worker.tick()
                    if condition(): return
                    time.sleep(.05)
                self.fail(str(worker.catalog.rows('SELECT state,error FROM builds')))
            with DurableWorker(library, options, root / 'state') as worker:
                wait(worker, lambda: bool(worker.catalog.rows("SELECT * FROM builds WHERE state='failed'")))
                old = worker.catalog.rows("SELECT * FROM builds WHERE state='failed'")[0]
                new_id = worker.catalog.configure_lesson(old['lesson_id'], {'build_id': old['id'], 'chunk_minutes': 5})
                wait(worker, lambda: worker.catalog.build(new_id)['state'] == 'ready')
                self.assertEqual(transcribe.call_count, 1)
                self.assertEqual(scenes.call_count, 1)
                self.assertEqual(len(worker.catalog.published_courses()[0]['lessons'][0]['steps']), 3)
                self.assertEqual(worker.catalog.build(old['id'])['spec'], old['spec'])
                worker.catalog.reconcile(library, worker.serialized)
                self.assertEqual(len(worker.catalog.rows('SELECT * FROM builds')), 2)


if __name__ == '__main__': unittest.main()
