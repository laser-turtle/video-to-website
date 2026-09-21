import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = ['reader-state.js', 'reading.js'].map(name => fs.readFileSync('src/video_to_website/assets/' + name, 'utf8')).join('\n');
const clone = value => JSON.parse(JSON.stringify(value));
const lessons = [
  {key: 'v2w:a:content', steps: ['step-1', 'step-3']},
  {key: 'v2w:b:content', steps: []},
];
function server() {
  const db = {version: 1, states: {}, lessons: {[lessons[0].key]: lessons[0].steps, [lessons[1].key]: ['__lessonComplete']},
    preferences: {values: {rate: 1, loop: true, player_collapsed: true, clip_autoplay: true}, revision: 0}};
  const state = {db, requests: [], online: true, status: 200, loseResponse: false};
  state.fetch = async (_url, options) => {
    if (!state.online) throw new Error('Network unavailable');
    const body = options.body ? JSON.parse(options.body) : null;
    state.requests.push(body);
    let status = state.status;
    if (body && status === 200) {
      if (body.action === 'patch' && body.entries.some(e => e.revision !== (db.states[e.key]?.revision || 0))) status = 409;
      if (body.action === 'preferences' && body.revision !== db.preferences.revision) status = 409;
      if (status === 200 && ['import', 'patch'].includes(body.action)) {
        for (const entry of body.entries) {
          if (body.action === 'import' && db.states[entry.key]) continue;
          const before = db.states[entry.key] || {state: {}, revision: 0};
          db.states[entry.key] = {state: {...before.state, ...entry.changes}, revision: before.revision + 1};
        }
      } else if (status === 200 && (body.action === 'preferences' || !db.preferences.revision)) {
        db.preferences = {values: {...db.preferences.values, ...body.values}, revision: db.preferences.revision + 1};
      }
      if (state.loseResponse) { state.loseResponse = false; throw new Error('Response lost; check current progress.'); }
    }
    return {ok: status === 200, status, headers: {get: () => 'application/json'},
      json: async () => clone(status === 200 ? db : {error: status === 409 ? 'Progress changed on another device.' : 'Unavailable'})};
  };
  return state;
}
async function browser(server, saved = new Map(), blocked = false) {
  const events = {};
  const storage = {
    getItem(key) { if (blocked) throw Error('blocked'); return saved.get(key) ?? null; },
    setItem(key, value) { if (blocked) throw Error('blocked'); saved.set(key, value); },
    removeItem(key) { saved.delete(key); },
  };
  const document = {body: {dataset: {readingApi: 'api/reading'}}, querySelectorAll: () => [], addEventListener() {}};
  const window = {addEventListener(type, fn) { events[type] = fn; }};
  const result = new Function('document', 'window', 'location', 'localStorage', 'fetch', 'setTimeout', 'clearTimeout', 'setInterval',
    source + '\nreturn {state:V2WReaderState,reading:V2WReading};')(document, window, {protocol: 'http:'}, storage, server.fetch, () => 1, () => {}, () => {});
  await result.state.ready;
  return {...result, saved, events};
}

const remote = server();
const first = await browser(remote, new Map([[lessons[0].key, '{"step-1":true}'], ['v2w:rate', '1.5'], ['v2w:loop', '0']]));
assert.equal(first.reading.label(lessons[0]), '1 of 2 steps · 50%');
assert.equal(first.reading.summary(lessons), '0 of 2 lessons complete · 25%');
assert.equal(first.state.preferences().rate, 1.5);
assert.equal(first.state.preferences().loop, false);
assert.ok(first.state.status().startsWith('Synced'));
const second = await browser(remote);
await second.reading.setStep(lessons[0], 'step-3', true);
await first.state.refresh();
assert.equal(first.reading.stats(lessons[0]).status, 'complete');

// An older tab cannot overwrite an edit committed by another device.
await second.reading.setStep(lessons[0], 'step-1', false);
await assert.rejects(first.reading.mark(lessons, true), /another device/);
assert.equal(remote.db.states[lessons[1].key], undefined, 'failed bulk action does not affect other lessons');
assert.equal(first.reading.stats(lessons[0]).done, 1, 'conflict reloads authoritative state');
const completed = await first.reading.mark(lessons, true);
assert.equal(first.reading.summary(lessons), '2 of 2 lessons complete · 100%');
await first.reading.undo(completed.changes);
assert.equal(first.reading.stats(lessons[0]).done, 1, 'undo restores partial progress');
assert.equal(first.reading.stats(lessons[1]).done, 0);

const changed = await first.reading.mark(lessons, true);
await second.state.refresh();
await second.reading.setStep(lessons[0], 'step-1', false);
await first.state.refresh();
const undone = await first.reading.undo(changed.changes);
assert.equal(undone.skipped, 1, 'undo preserves newer edits');
assert.equal(first.reading.stats(lessons[0]).done, 1);
await first.reading.mark(lessons, false);
const oldBrowser = await browser(remote, new Map([[lessons[0].key, '{"step-1":true,"step-3":true}']]));
assert.equal(oldBrowser.reading.stats(lessons[0]).done, 0, 'old browser cannot resurrect explicit resets');

await first.state.edit([], {rate: 2, clip_autoplay: false});
await second.state.refresh();
assert.equal(second.state.preferences().rate, 2);
assert.equal(second.state.preferences().clip_autoplay, false);
assert.equal(second.saved.get('v2w:rate'), '2');

remote.online = false;
await second.state.refresh();
assert.equal(second.state.writable(), false);
await assert.rejects(second.reading.mark(lessons, true), /Reconnect/);
assert.equal(second.reading.stats(lessons[0]).done, 0, 'outage never pretends a local edit was synced');
remote.online = true;
await second.state.refresh();
assert.equal(second.state.writable(), true);
remote.loseResponse = true;
await assert.rejects(second.reading.mark(lessons, true), /Response lost/);
assert.equal(second.reading.stats(lessons[0]).status, 'complete', 'lost save response reconciles with committed state');

const blocked = await browser(remote, new Map(), true);
await blocked.reading.mark(lessons, false);
assert.equal(blocked.reading.stats(lessons[0]).done, 0, 'SQLite sync works when browser storage is blocked');
const staticServer = server(); staticServer.status = 404;
const local = await browser(staticServer, new Map([[lessons[0].key, '{"step-1":true}']]));
assert.ok(local.state.status().includes('static export'));
const localChange = await local.reading.mark(lessons, true);
await local.reading.undo(localChange.changes);
assert.equal(local.reading.stats(lessons[0]).done, 1);
const wasServer = await browser(staticServer, new Map([['v2w:reader:server', '1']]));
assert.equal(wasServer.state.writable(), false, 'a missing endpoint after an upgrade is not silently treated as a static export');
const corrupt = await browser(staticServer, new Map([[lessons[0].key, 'null'], [lessons[1].key, '[]']]));
assert.equal(corrupt.reading.aggregate(lessons).fraction, 0);
const corruptPrefs = await browser(staticServer, new Map([['v2w:reader:preferences', '{"rate":"fast","loop":[],"clip_autoplay":null}']]));
assert.deepEqual(corruptPrefs.state.preferences(), corruptPrefs.state.defaults, 'invalid cached preferences cannot break media playback');
console.log('reading state and sync runtime checks passed');
