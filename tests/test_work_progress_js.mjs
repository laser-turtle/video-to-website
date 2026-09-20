import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const source = readFileSync(process.argv[2], 'utf8');
const progress = new Function(source + '\nreturn V2WProgress;')();
const entry = {label: 'Transcribing the audio', stage_started: 100,
  progress: {phase: 'speech', label: 'Transcribing audio', completed: 40, total: 100,
    unit: 'percent', fraction: .4, eta_seconds: 120, updated: 140}};
let result = progress.describe(entry, 150);
assert.equal(result.text, 'Transcribing audio · 40%');
assert.equal(result.estimate, 'About 2m left for transcription');
assert.equal(result.elapsed, '50s', 'new measurements do not reset stage elapsed time');
assert.equal(result.fraction, .4);
result = progress.describe(entry, 300);
assert.ok(result.stale);
assert.ok(result.estimate.includes('Estimate paused'));
assert.ok(!result.estimate.includes('left'));

result = progress.describe({label: 'Writing steps', step: 4, steps: 6, elapsed: 42}, 150);
assert.equal(result.fraction, null, 'the fourth of six stages is not 67% of the work');
assert.equal(result.estimate, 'Time remaining is not available yet.');

result = progress.describe({progress: {phase: 'scenes', label: 'Analyzing visual changes',
  completed: 90, total: 180, fraction: .5, unit: 'seconds', eta_seconds: 28, updated: 100}}, 100);
assert.equal(result.detail, '1:30 / 3:00 of video processed');
assert.equal(result.estimate, 'About 30s left for visual analysis');

result = progress.describe({progress: {phase: 'sections', label: 'Writing instructions',
  completed: 0, total: 1, fraction: 0, unit: 'sections', detail: '1,245 characters received', updated: 100}}, 100);
assert.ok(result.detail.includes('0 of 1 transcript sections completed'));
assert.ok(result.detail.includes('1,245 characters received'));
assert.equal(result.estimate, 'Writing length varies; no time estimate yet.');
assert.equal(result.fraction, null, 'a single unfinished model call has no measurable percentage');

result = progress.describe({progress: {phase: 'assets', label: 'Creating media',
  completed: 3, total: 10, fraction: .3, unit: 'steps', detail: 'Step 4 — Encoding clip · 60%', eta_seconds: 71, updated: 100}}, 100);
assert.ok(result.detail.includes('3 of 10 steps illustrated'));
assert.ok(result.detail.includes('Encoding clip · 60%'));
assert.equal(result.estimate, 'About 1m 30s left for lesson media');

result = progress.describe({progress: {phase: 'cached', fraction: 1, updated: 100}}, 100);
assert.equal(result.estimate, 'Already processed; using saved results.');
assert.equal(progress.describe({progress: {fraction: NaN, eta_seconds: 60}}, 100).fraction, null);
assert.equal(progress.describe({progress: {fraction: Infinity, eta_seconds: 60}}, 100).estimate, 'Time remaining is not available yet.');
console.log('progress.js runtime checks passed');
