import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = ['reader-state.js', 'reading.js'].map(name => fs.readFileSync('src/video_to_website/assets/' + name, 'utf8')).join('\n');
const clone = value => JSON.parse(JSON.stringify(value));
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
const lessons = [
  {key: 'v2w:a:content', steps: ['step-1', 'step-3']},
  {key: 'v2w:b:content', steps: []},
];
function server() {
  const db = {version: 1, states: {}, views: {}, courses: ['a', 'b'], lessons: {[lessons[0].key]: lessons[0].steps, [lessons[1].key]: ['__lessonComplete']},
    preferences: {values: {rate: 1, loop: true, player_collapsed: true, clip_autoplay: true, hide_completed: false}, revision: 0}};
  const state = {db, requests: [], online: true, status: 200, loseResponse: false};
  state.fetch = async (_url, options) => {
    if (!state.online) throw new Error('Network unavailable');
    const body = options.body ? JSON.parse(options.body) : null;
    state.requests.push(body);
    if (body && state.beforeWrite) await state.beforeWrite(body);
    if (body?.action === 'patch' && state.beforePatch) await state.beforePatch(body);
    if (!body && state.beforeGet) await state.beforeGet();
    let status = state.status;
    if (body && status === 200) {
      if (body.action === 'patch' && body.entries.some(e => e.revision !== (db.states[e.key]?.revision || 0))) status = 409;
      if (body.action === 'preferences' && body.revision !== db.preferences.revision) status = 409;
      if (body.action === 'view' && body.revision !== (db.views[body.scope]?.revision || 0)) status = 409;
      if (status === 200 && ['import', 'patch'].includes(body.action)) {
        for (const entry of body.entries) {
          if (body.action === 'import' && db.states[entry.key]) continue;
          const before = db.states[entry.key] || {state: {}, revision: 0};
          db.states[entry.key] = {state: {...before.state, ...entry.changes}, revision: before.revision + 1};
        }
      } else if (status === 200 && body.action === 'view') {
        const before = db.views[body.scope] || {values: {}, revision: 0};
        db.views[body.scope] = {values: {...before.values, ...body.values}, revision: before.revision + 1};
      } else if (status === 200 && body.action === 'import-views') {
        for (const entry of body.entries) if (!db.views[entry.scope]) db.views[entry.scope] = {values: entry.values, revision: 1};
      } else if (status === 200 && (body.action === 'preferences' || (body.action === 'import-preferences' && !db.preferences.revision))) {
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
  const document = {body: {dataset: {readingApi: 'api/reading'}}, querySelector: () => null, querySelectorAll: () => [], addEventListener() {}};
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
await first.state.edit([], {hide_completed: true});
await second.state.refresh();
assert.equal(second.state.preferences().hide_completed, true, 'Hide completed synchronizes across isolated browser stores');
const freshDevice = await browser(remote);
assert.equal(freshDevice.state.preferences().hide_completed, true, 'a new device receives the shared preference');
assert.equal(freshDevice.state.preferences().rate, 2, 'saving visibility preserves playback settings');

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

// Rapid reader edits stay optimistic while the first response is delayed.
const slowServer = server(), slow = await browser(slowServer), held = deferred();
let patchCount = 0;
slowServer.beforePatch = () => ++patchCount === 1 ? held.promise : undefined;
assert.equal(slow.reading.queueStep(lessons[0], 'step-1', true), true);
assert.equal(slow.reading.stats(lessons[0]).done, 1, 'the first checkmark is visible synchronously');
await flush();
assert.equal(slow.state.writable(), false, 'bulk edits wait for pending step changes');
assert.equal(slow.state.canQueueStep(), true, 'more step edits remain available during the save');
slow.reading.queueStep(lessons[0], 'step-3', true);
slow.reading.queueStep(lessons[0], 'step-1', false);
assert.equal(slow.state.pendingSteps().count, 2);
assert.deepEqual(slow.reading.read(lessons[0]), {'step-1': false, 'step-3': true}, 'newer toggles win in the pending view');
assert.equal(slowServer.requests.filter(r => r?.action === 'patch').length, 1, 'only one save is in flight');
await assert.rejects(slow.reading.mark(lessons, true), /still saving/);
const leaving = {preventDefault() { this.prevented = true; }};
slow.events.beforeunload(leaving); assert.equal(leaving.prevented, true, 'unconfirmed changes cannot disappear silently on navigation');
held.resolve(); await flush();
assert.equal(slow.state.pendingSteps().count, 0);
assert.equal(slow.state.writable(), true);
assert.deepEqual(slowServer.db.states[lessons[0].key], {state: {'step-1': false, 'step-3': true}, revision: 2});
const batches = slowServer.requests.filter(r => r?.action === 'patch');
assert.equal(batches.length, 2, 'edits arriving during a save are batched');
assert.equal(batches[1].entries[0].revision, 1, 'the next batch uses the acknowledged revision');
const savedLeaving = {preventDefault() { this.prevented = true; }};
slow.events.beforeunload(savedLeaving); assert.equal(savedLeaving.prevented, undefined);

// Save errors roll back the optimistic view and retain the intent for retry.
const failingServer = server(), failing = await browser(failingServer);
failingServer.beforePatch = () => { throw new Error('Connection interrupted'); };
failing.reading.queueStep(lessons[0], 'step-1', true);
failing.reading.queueStep(lessons[0], 'step-3', true);
await flush();
assert.equal(failing.reading.stats(lessons[0]).done, 0, 'failed checks revert to confirmed progress');
assert.equal(failing.state.pendingSteps().count, 2, 'failed choices remain available for retry');
assert.match(failing.state.pendingSteps().error, /interrupted/);
assert.equal(failing.state.canQueueStep(), false);
assert.equal(failing.reading.queueStep(lessons[0], 'step-1', true), false);
await failing.state.refresh();
assert.match(failing.state.pendingSteps().error, /interrupted/, 'polling cannot hide failed changes');
delete failingServer.beforePatch;
await failing.state.retrySteps(); await flush();
assert.equal(failing.state.pendingSteps().count, 0);
assert.equal(failing.reading.stats(lessons[0]).done, 2);

// Another device's reset cannot be silently overwritten by a queued batch.
const conflictingServer = server(), conflict = await browser(conflictingServer), conflictGate = deferred();
conflictingServer.beforePatch = () => conflictGate.promise;
conflict.reading.queueStep(lessons[0], 'step-1', true); await flush();
conflictingServer.db.states[lessons[0].key] = {state: {'step-1': false, 'step-3': true}, revision: 1};
conflictGate.resolve(); await flush();
assert.match(conflict.state.pendingSteps().error, /another device/);
assert.equal(conflict.reading.read(lessons[0])['step-1'], false);
conflict.state.discardSteps();
assert.equal(conflict.state.pendingSteps().count, 0);
assert.equal(conflict.state.canQueueStep(), true);
assert.equal(conflict.reading.stats(lessons[0]).done, 1, 'discard preserves server progress');

// A keypress can arrive during a poll which subsequently loses its connection.
const pollingServer = server(), polling = await browser(pollingServer), pollGate = deferred();
pollingServer.beforeGet = async () => { await pollGate.promise; throw new Error('Disconnected during refresh'); };
const poll = polling.state.refresh();
assert.equal(polling.reading.queueStep(lessons[0], 'step-1', true), true);
pollGate.resolve(); await poll; await flush();
assert.match(polling.state.pendingSteps().error, /Reconnect/, 'the queue reports failure instead of remaining stuck on Saving');
assert.equal(polling.reading.stats(lessons[0]).done, 0);
delete pollingServer.beforeGet;
await polling.state.retrySteps(); await flush();
assert.equal(polling.state.pendingSteps().count, 0);
assert.equal(polling.reading.stats(lessons[0]).done, 1);

// Local exports use the same optimistic queue without losing newer toggles.
local.reading.queueStep(lessons[0], 'step-1', false);
local.reading.queueStep(lessons[0], 'step-3', true);
await flush();
assert.deepEqual(JSON.parse(local.saved.get(lessons[0].key)), {'step-1': false, 'step-3': true});
const blockedLocal = await browser(staticServer, new Map(), true);
blockedLocal.reading.queueStep(lessons[0], 'step-1', true); await flush();
assert.ok(blockedLocal.state.pendingSteps().error, 'storage failures retain a retryable intent too');
blockedLocal.state.discardSteps();
assert.equal(blockedLocal.state.pendingSteps().count, 0);

const viewServer = server();
const legacy = await browser(viewServer, new Map([
  ['v2w:course:a:navigation', JSON.stringify({sort: 'title', view: 'flat', density: 'compact', readerDensity: 'detailed', opened: {'4:1': false}, hideCompleted: true})],
  ['v2w:library:view', JSON.stringify({grouping: 'chapters', density: 'compact', expanded: {a: false}, chapters: {'a|4:1': true}})],
]));
assert.deepEqual(legacy.state.view('course:a'), {sort: 'title', view: 'flat', density: 'compact', readerDensity: 'detailed', 'opened:4:1': false});
assert.deepEqual(legacy.state.view('library'), {grouping: 'chapters', density: 'compact', 'expanded:a': false, 'chapter:a|4:1': true});
const otherView = await browser(viewServer, new Map([['v2w:course:a:navigation', '{"density":"detailed","opened":{"4:1":true}}']]));
assert.equal(otherView.state.view('course:a').density, 'compact', 'an older browser cannot overwrite imported server choices');
const viewGate = deferred(); let writes = 0;
viewServer.beforeWrite = () => ++writes === 1 ? viewGate.promise : undefined;
legacy.state.queueView('course:a', {sort: 'number', 'opened:4:1': true}); await flush();
legacy.state.queueView('course:a', {sort: 'shortest', readerDensity: 'compact'});
legacy.state.queueView('library', {grouping: 'flat', 'chapter:a|4:1': false});
legacy.state.queuePreferences({hide_completed: true});
legacy.reading.queueStep(lessons[0], 'step-1', true);
assert.equal(legacy.state.view('course:a').sort, 'shortest', 'view changes are immediate while a save is held');
assert.equal(legacy.state.preferences().hide_completed, true);
assert.equal(writes, 1, 'progress and preferences share one write queue');
viewGate.resolve(); await flush();
assert.equal(legacy.state.pendingChanges().count, 0);
await otherView.state.refresh();
assert.equal(otherView.state.view('course:a').sort, 'shortest');
assert.equal(otherView.state.view('course:a').readerDensity, 'compact');
assert.equal(otherView.state.view('course:a').density, 'compact', 'unrelated view fields are preserved');
assert.equal(otherView.state.view('library')['chapter:a|4:1'], false);
assert.equal(otherView.reading.stats(lessons[0]).done, 1);
assert.equal(otherView.state.preferences().hide_completed, true);
viewServer.beforeWrite = () => { throw Error('View save interrupted'); };
legacy.state.queueView('course:a', {density: 'detailed'}); await flush();
assert.equal(legacy.state.view('course:a').density, 'compact', 'failed view saves roll back the optimistic layout');
assert.match(legacy.state.pendingChanges().error, /interrupted/);
delete viewServer.beforeWrite;
await legacy.state.retryChanges(); await flush();
assert.equal(legacy.state.view('course:a').density, 'detailed');
const reloadedView = await browser(viewServer);
assert.equal(reloadedView.state.view('course:a').density, 'detailed');
console.log('reading state and sync runtime checks passed');
