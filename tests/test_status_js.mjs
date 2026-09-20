// Runtime checks for the index page's build-status script, against a DOM stub.
// It is all asynchronous polling and self-reloading, which is exactly the kind
// of thing that only breaks once it is in front of someone.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(process.argv[2], 'utf8');

function makeElement(tag) {
  return {
    tagName: tag,
    className: '',
    children: [],
    hidden: false,
    style: {},
    listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    _text: '',
    appendChild(child) { this.children.push(child); return child; },
    get textContent() {
      return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text;
    },
    set textContent(value) { this._text = value; this.children = []; },
  };
}

const panel = makeElement('section');
const list = makeElement('ul');
const count = makeElement('span');
const byId = { 'build-status': panel, 'build-list': list, 'build-count': count };

const documentListeners = {};
const fakeDocument = {
  hidden: false,
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => makeElement(tag),
  createTextNode: (text) => { const n = makeElement('#text'); n.textContent = text; return n; },
  addEventListener(type, fn) { (documentListeners[type] ||= []).push(fn); },
};

// The script polls; the test drives every tick by hand.
let payload = null;
let responseOk = true;
let fetched = [];
const fakeFetch = (url) => {
  fetched.push(url);
  if (!responseOk) { return Promise.resolve({ ok: false, status: 404 }); }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
};

let tick = null;
const fakeSetInterval = (fn) => { tick = fn; return 1; };
let reloads = 0;
const fakeLocation = { reload() { reloads++; } };

const flush = () => new Promise((resolve) => setImmediate(resolve));

function video(overrides) {
  return Object.assign({
    id: '/lib/a.mp4', course: 'Launch Pad 2', title: 'Car Body',
    state: 'queued', stage: null, label: 'Waiting', step: 0, steps: 5,
    started: null, elapsed: 0,
  }, overrides);
}

// 1. Nothing to show: the panel stays out of the way.
payload = { updated: 1, building: false, built: 0, videos: [] };
new Function('document', 'fetch', 'setInterval', 'location', source)(
  fakeDocument, fakeFetch, fakeSetInterval, fakeLocation,
);
await flush();
await flush();
assert.equal(panel.hidden, true, 'an idle builder shows no panel');
assert.ok(fetched[0].startsWith('status.json?t='), 'the poll is cache-busted');

// 2. A video being worked on shows its stage, course and elapsed time.
payload = {
  updated: 2, building: true, built: 0,
  videos: [video({ state: 'working', label: 'Transcribing the audio', step: 2, elapsed: 95 })],
};
tick();
await flush();
await flush();
assert.equal(panel.hidden, false, 'a running build shows the panel');
assert.equal(list.children.length, 1, 'one row for one video');
const row = list.children[0];
assert.equal(row.className, 'working');
assert.ok(row.textContent.includes('Car Body'), 'the row names the video');
assert.ok(row.textContent.includes('Transcribing the audio'), 'and the stage it is in');
assert.ok(row.textContent.includes('Launch Pad 2'), 'and the course it belongs to');
assert.ok(row.textContent.includes('1m 35s'), 'and how long it has been at it');

// 3. The bar counts a stage under way as half done, so it moves during whisper.
const bar = row.children.find((c) => c.className === 'bar');
assert.ok(bar, 'a working row has a progress bar');
assert.equal(bar.children[0].style.width, (100 * 1.5 / 5) + '%');

// 4. A long queue collapses rather than filling the page.
payload = {
  updated: 3, building: true, built: 0,
  videos: [
    video({ id: 'w', state: 'working', label: 'Reading the file', step: 1 }),
    ...Array.from({ length: 9 }, (_, i) => video({ id: 'q' + i, title: 'Lesson ' + i })),
  ],
};
tick();
await flush();
await flush();
assert.equal(list.children.length, 8, 'one working, six queued, one summary');
assert.equal(list.children[7].className, 'more');
assert.equal(list.children[7].textContent, 'View all 9 waiting lessons');
assert.equal(list.children[7].children[0].href, 'queue.html');
assert.equal(count.textContent, '1 running · 9 waiting');

// 5. A failure is always shown, never collapsed away.
payload = {
  updated: 4, building: true, built: 0,
  videos: [
    video({ id: 'bad', title: 'Broken', state: 'failed', label: 'Failed' }),
    ...Array.from({ length: 9 }, (_, i) => video({ id: 'q' + i })),
  ],
};
tick();
await flush();
await flush();
assert.equal(list.children[0].className, 'failed', 'the failure comes first');
assert.ok(list.children[0].textContent.includes('Broken'));

// 6. Finished videos are counted rather than listed; they are courses now.
payload = {
  updated: 5, building: true, built: 0,
  videos: [
    video({ id: 'w', state: 'working', label: 'Writing the steps', step: 4 }),
    video({ id: 'd1', state: 'done', label: 'Done' }),
    video({ id: 'd2', state: 'skipped', label: 'Skipped' }),
  ],
};
tick();
await flush();
await flush();
assert.equal(list.children.length, 1, 'only the unfinished video is listed');
assert.equal(count.textContent, '1 running');

// 7. A build finishing moves `built`, and the page reloads to pick up the
// courses that just appeared.
assert.equal(reloads, 0, 'nothing has reloaded yet');
payload = { updated: 6, building: false, built: 1758300000, videos: [] };
tick();
await flush();
await flush();
assert.equal(reloads, 1, 'a finished build reloads the page');

// 8. A build that stopped part way says so, and says when it will try again.
payload = {
  // fail_build leaves `built` where it was -- the pages on disk are still the
  // last good build's, so an open page has no reason to reload.
  updated: 6, building: false, built: 0,
  error: 'the API returned 529', retry_in: 120,
  videos: [video({ id: 'bad', state: 'failed', label: 'Failed' }), video({ id: 'q' })],
};
tick();
await flush();
await flush();
assert.equal(panel.hidden, false, 'a stopped build still shows the panel');
assert.equal(list.children[0].className, 'failed');
assert.ok(list.children[0].textContent.includes('Build stopped'));
assert.ok(list.children[0].textContent.includes('the API returned 529'), 'with the reason');
assert.ok(list.children[0].textContent.includes('retrying in 2m 0s'), 'and when it retries');
assert.equal(reloads, 1, 'and nothing reloads: the pages on disk did not change');

// 9. A missing or half-written status file is a normal state, not an error.
responseOk = false;
tick();
await flush();
await flush();
assert.equal(panel.hidden, true, 'no status file just means no panel');

// 10. Returning to a backgrounded tab polls immediately.
responseOk = true;
payload = { updated: 7, building: true, built: 1758300000, videos: [video({ state: 'working' })] };
const before = fetched.length;
documentListeners.visibilitychange.forEach((fn) => fn());
assert.equal(fetched.length, before + 1, 'coming back to the tab refreshes it');

// Durable jobs expose retry through stable lesson IDs.
await flush(); await flush();
payload = { updated: 8, built: 0, videos: [video({id: 'a'.repeat(32), state: 'failed'})] };
tick(); await flush(); await flush();
const retry = list.children[0].children.find(c => c.tagName === 'button');
assert.equal(retry.textContent, 'Retry');
retry.listeners.click();
await flush(); await flush();
assert.ok(fetched.some(url => url === 'api/lessons/' + 'a'.repeat(32) + '/retry'));

console.log('status.js runtime checks passed');
