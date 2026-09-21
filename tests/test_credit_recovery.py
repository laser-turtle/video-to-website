"""Upgrade a running workflow, pause for funding, restart and resume its ID."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from video_to_website.catalog import Catalog
from video_to_website.provider_pause import ProviderPauses


class CreditRecoveryTests(unittest.TestCase):
    def test_old_job_survives_upgrade_credit_pause_restart_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'library' / 'Course'; folder.mkdir(parents=True)
            (folder / '4.01 - Lesson.mp4').write_bytes(b'video')
            fixture = Path(__file__).with_name('credit_fixture.py')
            processes = []
            with (root / 'worker.log').open('w+') as output:
                def start(phase):
                    env = dict(os.environ)
                    if phase == 'legacy' and env.get('V2W_LEGACY_SRC'):
                        env['PYTHONPATH'] = env['V2W_LEGACY_SRC']
                    process = subprocess.Popen([sys.executable, str(fixture), str(root), phase], env=env,
                        stdout=output, stderr=output)
                    processes.append(process); return process
                def wait(condition, process):
                    deadline = time.monotonic() + 20
                    while not condition():
                        if process.poll() is not None or time.monotonic() > deadline:
                            output.flush(); output.seek(0)
                            self.fail(output.read())
                        time.sleep(.05)
                def kill(process):
                    process.kill(); process.wait(timeout=5)
                try:
                    old = start('legacy')
                    wait(lambda: (root / 'legacy-in-flight').exists(), old)
                    kill(old)
                    catalog = Catalog(root / 'state')
                    original = catalog.rows('SELECT * FROM builds')[0]
                    original_spec = original['spec']
                    for path in (root / 'work').rglob('transcript.json'): path.unlink()
                    self.assertFalse(list((root / 'work').rglob('llm-sections/*.json')))
                    upgraded = start('current')
                    pauses = ProviderPauses(catalog)
                    wait(lambda: any(v['state'] == 'blocked' for v in catalog.status()['videos']), upgraded)
                    pause = pauses.get('anthropic')
                    kill(upgraded)
                    before = (root / 'calls').read_text().splitlines()
                    self.assertEqual(before.count('transcribe'), 1, 'old DBOS checkpoints survive the upgrade')
                    self.assertEqual(before.count('legacy:0'), 1)
                    self.assertEqual(before.count('current:0'), 1, 'only work never saved by the old code repeats once')
                    self.assertEqual(before.count('current:75'), 1)
                    resumed = start('current')
                    # Let it reach the persisted pause again, with the same ID.
                    wait(lambda: (root / ('entered-gate-' + str(resumed.pid))).exists(), resumed)
                    self.assertEqual(catalog.status()['videos'][0]['state'], 'blocked')
                    # Intentional waits must not become failures merely because
                    # several deployments exceeded the ordinary recovery budget.
                    for _ in range(2):
                        kill(resumed)
                        resumed = start('current')
                        wait(lambda: (root / ('entered-gate-' + str(resumed.pid))).exists(), resumed)
                    time.sleep(.7)
                    self.assertEqual((root / 'calls').read_text().splitlines(), before, 'restart does not retry the provider while paused')
                    (root / 'funded').touch()
                    pauses.resume('anthropic', pause['id'])
                    wait(lambda: catalog.build(original['id'])['state'] == 'ready', resumed)
                    resumed.wait(timeout=10)
                    output.flush(); output.seek(0)
                    self.assertEqual(resumed.returncode, 0, output.read())
                    after = (root / 'calls').read_text().splitlines()
                    self.assertEqual(after.count('transcribe'), 1)
                    self.assertEqual(after.count('current:0'), 1, 'saved section is not billed again after another restart')
                    self.assertEqual(after.count('current:75'), 2)
                    self.assertEqual(after.count('current:150'), 1)
                    jobs = catalog.rows('SELECT * FROM builds')
                    self.assertEqual(len(jobs), 1)
                    self.assertEqual(jobs[0]['id'], original['id'])
                    self.assertEqual(jobs[0]['spec'], original_spec)
                    self.assertEqual(pauses.active(), {})
                    self.assertEqual(pauses.waiting(), {})
                    lesson = catalog.published_courses()[0]['lessons'][0]
                    self.assertEqual(len(lesson['steps']), 3)
                finally:
                    for process in processes:
                        if process.poll() is None: kill(process)


if __name__ == '__main__': unittest.main()
