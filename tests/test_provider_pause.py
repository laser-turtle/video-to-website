"""Credit failures pause requests without losing work or changing build intent."""

import http.client
import http.server
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from video_to_website.catalog import Catalog, SCHEMA_VERSION
from video_to_website.durable import LazyBackend, WorkerStopping, check_current, retryable, serialize_options
from video_to_website.ingest import IngestHandler
from video_to_website.llm import AnthropicBackend, ClaudeCliBackend, LLMError, ProviderBlocked, funding_error
from video_to_website.pipeline import BuildOptions
from video_to_website.provider_pause import PauseConflict, ProviderPauses
from video_to_website.util import BuildCancelled, process_control


class FundingErrorTests(unittest.TestCase):
    def test_explicit_funding_failures_are_distinct_from_transient_errors(self):
        for message, status, body in [
            ('Your credit balance is too low to access the API.', 400, None),
            ('Credit balance too low', None, None),
            ('Insufficient credits', 429, None),
            ('Payment required', 402, None),
            ('Billing issue', None, {'error': {'type': 'billing_error'}}),
            ('Limit reached', 429, {'error': {'details': {'error_code': 'enforced_spend_limit_reached'}}}),
            ('You have reached your specified workspace API usage limits', 400, None),
            ('Monthly spending limit exceeded', 400, None),
        ]:
            with self.subTest(message=message):
                error = funding_error(message, status=status, body=body)
                self.assertIsInstance(error, ProviderBlocked)
                self.assertFalse(retryable(error))
        for message, status in [('Too many requests per minute', 429), ('Your API usage limits were exceeded: requests per minute', 429),
                                ('Overloaded', 529), ('Invalid API key', 401), ('max_tokens is too large', 400)]:
            with self.subTest(message=message):
                self.assertIsNone(funding_error(message, status=status))
        self.assertIsNone(funding_error('error', body={'error': {'details': {'error_code': []}}}))

    def test_sdk_billing_and_streaming_errors_are_classified(self):
        import anthropic
        import httpx

        backend = AnthropicBackend.__new__(AnthropicBackend)
        backend._anthropic = anthropic
        for code, message, expected in [(400, 'Your credit balance is too low', ProviderBlocked),
                                        (402, 'Billing issue', ProviderBlocked), (429, 'Rate limited', LLMError)]:
            response = httpx.Response(code, request=httpx.Request('POST', 'https://example.invalid'))
            error = anthropic.APIStatusError(message, response=response, body={'error': {'message': message}})
            backend._final_message = Mock(side_effect=error)
            with self.subTest(code=code), self.assertRaises(expected) as caught:
                backend.complete('system', 'user')
            self.assertEqual(caught.exception.retryable, code == 429)
        backend._final_message = Mock(side_effect=anthropic.APIError('stream billing', request=httpx.Request('POST', 'https://example.invalid'),
            body={'error': {'type': 'billing_error'}}))
        with self.assertRaises(ProviderBlocked):
            backend.complete('system', 'user')

    def test_cli_only_classifies_error_envelopes_not_lesson_content(self):
        backend = ClaudeCliBackend.__new__(ClaudeCliBackend)
        backend.binary, backend.model, backend.timeout = 'claude', 'test', 10
        for is_error in (False, True):
            result = SimpleNamespace(returncode=0, stdout=json.dumps({'is_error': is_error, 'result': 'Insufficient credits'}), stderr='')
            with patch('video_to_website.llm.subprocess.run', return_value=result):
                if is_error:
                    with self.assertRaises(ProviderBlocked): backend.complete('s', 'u')
                else:
                    self.assertEqual(backend.complete('s', 'u'), 'Insufficient credits')
        self.assertIsInstance(funding_error("You've hit your limit", cli=True), ProviderBlocked)
        self.assertIsNone(funding_error("You've hit your limit"))


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / 'library'
        folder = self.library / 'Course'; folder.mkdir(parents=True)
        for name in ['one', 'two', 'three']:
            (folder / (name + '.mp4')).write_bytes(name.encode())
        self.catalog = Catalog(self.root / 'state')
        self.options = serialize_options(BuildOptions(out=self.root / 'site', llm='anthropic'))
        self.catalog.reconcile(self.library, self.options)
        self.builds = self.catalog.rows('SELECT * FROM builds ORDER BY created,id')
        self.pauses = ProviderPauses(self.catalog)

    def wait(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            if time.monotonic() > deadline: self.fail('condition did not become true')
            time.sleep(.02)

    def test_pause_and_waiting_are_persistent_additive_and_resume_is_fenced(self):
        before = self.catalog.library()['revision']
        first = self.pauses.pause('anthropic', 'credits', 'Add credits')
        self.assertEqual(self.pauses.pause('anthropic', 'credits', 'Again'), first)
        job = self.builds[0]
        self.catalog.update_build(job['id'], 'running', stage='steps')
        self.pauses.mark_waiting(job['id'], 'anthropic')
        reopened = Catalog(self.catalog.directory)
        self.assertEqual(ProviderPauses(reopened).get('anthropic'), first)
        self.assertEqual(reopened.rows('PRAGMA user_version')[0]['user_version'], SCHEMA_VERSION)
        self.assertEqual(reopened.library()['revision'], before)
        self.assertTrue(all(v['state'] == 'blocked' for v in reopened.status()['videos']))
        self.assertEqual(reopened.library()['courses'][0]['videos'][0]['state'], 'blocked')
        self.assertEqual(reopened.build(job['id'])['state'], 'running', 'DBOS/catalog execution state stays compatible')
        self.assertTrue(self.pauses.resume('anthropic', first['id']))
        self.assertFalse(self.pauses.resume('anthropic', first['id']))
        second = self.pauses.pause('anthropic', 'credits', 'Still empty')
        with self.assertRaises(PauseConflict): self.pauses.resume('anthropic', first['id'])
        self.assertEqual(self.pauses.get('anthropic'), second)
        self.catalog.cancel(job['lesson_id']); self.pauses.prune()
        self.assertNotIn(job['id'], self.pauses.waiting())
        self.assertEqual(next(v for v in self.catalog.status()['videos'] if v['build_id'] == job['id'])['state'], 'cancelled')

    def test_shared_gate_stops_request_fanout_and_waits_without_holding_llm_lock(self):
        calls, funded = [], threading.Event()
        stop = threading.Event()
        jobs = self.builds[:2]
        for job in jobs: self.catalog.update_build(job['id'], 'running', stage='steps')
        def make(name, model, **kwargs):
            def complete(system, user):
                calls.append(name)
                if name == 'anthropic' and not funded.is_set(): raise ProviderBlocked('Add credits')
                return 'success'
            return SimpleNamespace(complete=complete)
        def run(job):
            def check():
                if stop.is_set(): raise WorkerStopping()
                check_current(self.catalog, job['id'])
            with process_control(check):
                return LazyBackend('anthropic', 'test', True, catalog=self.catalog, build_id=job['id']).complete('s', 'u')
        with patch('video_to_website.durable.make_backend', side_effect=make), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, job) for job in jobs]
            try:
                self.wait(lambda: len(self.pauses.waiting()) == 2)
                self.assertEqual(calls, ['anthropic'])
                other = LazyBackend('ollama', 'local', True, catalog=self.catalog, build_id=self.builds[2]['id'])
                self.assertEqual(other.complete('s', 'u'), 'success', 'a paused provider releases the request lock')
                first = self.pauses.get('anthropic')
                self.pauses.resume('anthropic', first['id'])
                self.wait(lambda: self.pauses.get('anthropic') is not None)
                second = self.pauses.get('anthropic')
                self.assertNotEqual(first['id'], second['id'])
                self.assertEqual(calls.count('anthropic'), 2, 'resuming without funding pauses again after one caller')
                funded.set(); self.pauses.resume('anthropic', second['id'])
                self.assertEqual([f.result(timeout=5) for f in futures], ['success', 'success'])
                self.assertEqual(self.pauses.waiting(), {})
            finally:
                stop.set()

    def test_waiting_call_can_be_cancelled_or_interrupted_for_shutdown(self):
        self.pauses.pause('anthropic', 'credits', 'Empty')
        for stopping in (False, True):
            job = self.builds[int(stopping)]
            stop = threading.Event()
            self.catalog.update_build(job['id'], 'running', stage='steps')
            def check():
                if stop.is_set(): raise WorkerStopping()
                check_current(self.catalog, job['id'])
            def run():
                with process_control(check):
                    LazyBackend('anthropic', 'test', True, catalog=self.catalog, build_id=job['id']).complete('s', 'u')
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run)
                self.wait(lambda: job['id'] in self.pauses.waiting())
                if stopping: stop.set()
                else: self.catalog.cancel(job['lesson_id'])
                with self.assertRaises(WorkerStopping if stopping else BuildCancelled): future.result(timeout=2)

    def test_resume_endpoint_rejects_stale_and_invalid_requests(self):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), IngestHandler)
        server.library, server.catalog = self.library, self.catalog
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        def request(body):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                conn.request('POST', '/api/providers/anthropic/resume', json.dumps(body), {'Content-Type': 'application/json'})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally: conn.close()
        pause = self.pauses.pause('anthropic', 'credits', 'Empty')
        self.assertEqual(request({})[0], 400)
        self.assertEqual(request({'pause_id': pause['id']}), (200, {'resumed': True}))
        self.pauses.pause('anthropic', 'credits', 'Still empty')
        self.assertEqual(request({'pause_id': pause['id']})[0], 409)


if __name__ == '__main__': unittest.main()
