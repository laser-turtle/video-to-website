import concurrent.futures
import http.client
import http.server
import json
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path

from video_to_website import render
from video_to_website.catalog import Catalog, CatalogConflict, SCHEMA_VERSION
from video_to_website.ingest import IngestHandler
from video_to_website.reading_state import DEFAULTS, ReadingState


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = Catalog(Path(self.tmp.name))
        self.store = ReadingState(self.catalog)
        self.lessons = []
        with self.catalog.connect() as db:
            db.execute("INSERT INTO courses(id,path,slug,title) VALUES('course','course','course','Course')")
            for index in range(3):
                lesson = dict(id=str(index), slug=str(index), title=f'4.0{index} Lesson',
                    source_name=f'4.0{index} Lesson.mp4', reading_key=f'{index}:content', duration=60,
                    summary='', steps=[dict(index=n, title='Step', start=n, end=n+1, actions=['Do it']) for n in (1, 3)] if index else [],
                    poster=None, video_href=None)
                self.lessons.append(lesson)
                db.execute("INSERT INTO lessons(id,course_id,path,slug,digest,size,mtime,published) VALUES(?,?,?,?,?,?,?,?)",
                           (str(index), 'course', f'course/{index}.mp4', str(index), 'digest', 100, 0, json.dumps(lesson)))

    def entry(self, i, changes, revision=0):
        return {'key': f'v2w:{i}:content', 'changes': changes, 'revision': revision}

    def edit(self, *entries, action='patch'):
        return self.store.edit({'action': action, 'entries': list(entries)})

    def test_partial_video_and_reopened_catalog(self):
        self.edit(self.entry(0, {'__lessonComplete': True}), self.entry(1, {'step-3': True}))
        saved = ReadingState(Catalog(self.catalog.directory)).snapshot()
        self.assertEqual(saved['states']['v2w:1:content']['state'], {'step-3': True})
        self.assertTrue(saved['states']['v2w:0:content']['state']['__lessonComplete'])
        self.assertEqual(saved['preferences']['values'], DEFAULTS)
        self.assertEqual(self.catalog.rows('SELECT * FROM builds'), [])

    def test_import_keeps_server_state_including_explicit_resets(self):
        self.edit(self.entry(1, {'step-1': True}), action='import')
        self.edit(self.entry(1, {'step-1': False, 'step-3': False}, 1))
        self.edit(self.entry(1, {'step-1': True}), self.entry(2, {'step-3': True}), action='import')
        states = self.store.snapshot()['states']
        self.assertEqual(states['v2w:1:content']['revision'], 2)
        self.assertFalse(states['v2w:1:content']['state']['step-1'])
        self.assertTrue(states['v2w:2:content']['state']['step-3'])

    def test_bulk_conflict_rolls_back_entire_batch(self):
        self.edit(self.entry(2, {'step-1': True}))
        with self.assertRaises(CatalogConflict):
            self.edit(self.entry(1, {'step-1': True}), self.entry(2, {'step-3': True}))
        self.assertNotIn('v2w:1:content', self.store.snapshot()['states'])

    def test_simultaneous_devices_do_not_lose_writes(self):
        def update(step):
            try:
                self.edit(self.entry(1, {step: True}))
                return True
            except CatalogConflict:
                return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(update, ['step-1', 'step-3']))
        self.assertEqual(sum(results), 1)
        state = self.store.snapshot()['states']['v2w:1:content']
        missing = next(s for s in ['step-1', 'step-3'] if s not in state['state'])
        self.edit(self.entry(1, {missing: True}, state['revision']))
        self.assertEqual(self.store.snapshot()['states']['v2w:1:content']['state'], {'step-1': True, 'step-3': True})

    def test_regeneration_and_deleted_lessons_reject_old_pages(self):
        self.edit(self.entry(1, {'step-1': True}))
        with self.catalog.connect() as db:
            lesson = dict(self.lessons[1], reading_key='1:new-content')
            db.execute('UPDATE lessons SET published=? WHERE id=?', (json.dumps(lesson), '1'))
            db.execute("UPDATE lessons SET deleted=1 WHERE id='2'")
        snapshot = self.store.snapshot()
        self.assertNotIn('v2w:1:content', snapshot['states'])
        self.assertIn('v2w:1:new-content', snapshot['lessons'])
        for i in [1, 2]:
            with self.assertRaises(CatalogConflict): self.edit(self.entry(i, {'step-1': True}, 1))
        self.edit(self.entry(1, {'step-1': True}), action='import')
        self.assertEqual(self.store.snapshot()['states'], {})

    def test_validation_is_atomic(self):
        for bad in [self.entry(1, {'step-9': True}), self.entry(1, {'step-1': 'true'}),
                    self.entry(1, {'__lessonComplete': True}), self.entry(0, {'step-1': True}), None]:
            with self.assertRaises(ValueError): self.edit(self.entry(2, {'step-1': True}), bad)
            self.assertEqual(self.store.snapshot()['states'], {})
        with self.assertRaises(ValueError): self.edit(self.entry(1, {}), self.entry(1, {}))
        with self.assertRaises(CatalogConflict): self.edit(self.entry(1, {}, False))

    def test_preferences_conflicts_import_and_validation(self):
        self.store.edit({'action': 'import-preferences', 'values': {'rate': 1.5, 'loop': False}})
        self.store.edit({'action': 'preferences', 'revision': 1, 'values': {'rate': 2, 'clip_autoplay': False}})
        self.store.edit({'action': 'import-preferences', 'values': {'rate': 3}})
        with self.assertRaises(CatalogConflict): self.store.edit({'action': 'preferences', 'revision': 1, 'values': {'loop': True}})
        for values in [{'rate': True}, {'rate': 999}, {'loop': 1}, {'unknown': False}]:
            with self.assertRaises(ValueError): self.store.edit({'action': 'preferences', 'revision': 2, 'values': values})
        values = ReadingState(Catalog(self.catalog.directory)).snapshot()['preferences']['values']
        self.assertEqual(values, DEFAULTS | {'rate': 2, 'loop': False, 'clip_autoplay': False})

    def test_version_seven_migration_does_not_touch_workflows(self):
        with self.catalog.connect() as db:
            db.execute('DROP TABLE reading_state')
            db.execute('DROP TABLE reader_preferences')
            db.execute('PRAGMA user_version=7')
            db.execute("INSERT INTO builds VALUES('running','1','input','{}','running','transcribe',NULL,1,2)")
        before = self.catalog.rows('SELECT * FROM builds')
        migrated = Catalog(self.catalog.directory)
        self.assertEqual(migrated.rows('PRAGMA user_version')[0]['user_version'], SCHEMA_VERSION)
        self.assertEqual(migrated.rows('SELECT * FROM builds'), before)
        self.assertEqual(ReadingState(migrated).snapshot()['states'], {})

    def test_http_without_uploads_or_workers_and_invalid_bodies(self):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), IngestHandler)
        server.catalog = self.catalog
        server.library = None
        server.uploads_enabled = server.workers_enabled = False
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        def request(method='GET', body=None, content_type='application/json'):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                conn.request(method, '/api/reading', json.dumps(body) if body is not None else None, {'Content-Type': content_type})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        self.assertEqual(request()[0], 200)
        body = {'action': 'patch', 'entries': [self.entry(1, {'step-1': True})]}
        self.assertEqual(request('POST', body)[0], 200)
        self.assertEqual(request('POST', body)[0], 409)
        self.assertEqual(request('POST', body, 'text/plain')[0], 400)
        self.assertEqual(request('POST', [1])[0], 400)
        server.reading_enabled = False
        self.assertEqual(request()[0], 404)

    def test_rendered_manifests_match_reader_keys_and_actual_step_ids(self):
        class Parser(HTMLParser):
            def __init__(self, page):
                super().__init__(); self.attrs = []; self.feed(page)
            def handle_starttag(self, tag, attrs): self.attrs.append(dict(attrs))
        course = dict(id='course', slug='course', title='Course', lessons=self.lessons)
        for lesson in self.lessons:
            page = Parser(render.render_lesson_page(lesson, course))
            namespace = next(a['data-lesson'] for a in page.attrs if 'data-lesson' in a)
            manifest = json.loads(next(a['data-reading-control'] for a in page.attrs if 'data-reading-control' in a))
            self.assertEqual(manifest['key'], 'v2w:' + namespace)
            self.assertEqual(manifest['steps'], [a['id'] for a in page.attrs if a.get('class') == 'step'])
        for html in [render.render_root_index([course]), render.render_course_page(course)]:
            manifests = [json.loads(a['data-reading-summary']) for a in Parser(html).attrs if 'data-reading-summary' in a]
            self.assertEqual(manifests[0], [render._reading_lesson(lesson, course) for lesson in self.lessons])
        out = Path(self.tmp.name) / 'site'
        render.write_site(out, [course])
        for filename in ['index.html', 'course/index.html', 'course/1.html', 'settings.html']:
            self.assertIn('assets/reading.js?v=', (out / filename).read_text())
        self.assertEqual((out / 'assets/reading.js').read_text(), render.READING_SCRIPT)


if __name__ == '__main__':
    unittest.main()
