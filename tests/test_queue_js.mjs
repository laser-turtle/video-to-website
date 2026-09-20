import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(process.argv[2], 'utf8');
function element(tag) {
  return {
    tagName: tag, children: [], dataset: {}, style: {}, listeners: {}, attributes: {},
    value: '', hidden: false, disabled: false, _text: '',
    appendChild(child) { this.children.push(child); return child; },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    fire(name) { this.listeners[name]?.(); },
    focus() { document.activeElement = this; },
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); },
    set textContent(text) { this._text = text; this.children = []; },
  };
}
const ids = {};
for (const id of ['job-list', 'queue-summary', 'job-filter', 'job-search', 'queue-empty', 'queue-notice', 'queue-updated']) {
  ids[id] = element('div');
}
const list = ids['job-list'];
const filter = ids['job-filter'];
filter.value = 'active';
filter.options = ['active', 'queued', 'failed', 'cancelled', 'done', 'all'].map(value => ({ value }));
const document = {
  hidden: false, activeElement: null, listeners: {},
  getElementById(id) { return ids[id]; },
  createElement: element,
  addEventListener(name, fn) { this.listeners[name] = fn; },
};
const id = n => n.toString(16).padStart(32, '0');
const running = {id: id(1), title: 'Lighting', course: 'Blender', source_name: '01 Lighting.mp4', state: 'working', label: 'Transcribing the audio', step: 2, steps: 6, elapsed: 90};
const queued = Array.from({length: 9}, (_, i) => ({
  id: id(i + 2), title: 'Lesson ' + (i + 2), course: i === 8 ? 'Drawing' : 'Blender',
  source_path: 'chapter/lesson-' + (i + 2) + '.mp4', state: 'queued', queue_position: i + 1,
}));
const failed = {id: id(11), title: 'Broken source', course: 'Drawing', state: 'failed', error: 'No audio track', lesson_href: 'drawing/lesson-old.html'};
const ready = {id: id(12), title: 'Introduction', course: 'Blender', state: 'done', lesson_href: 'blender/lesson-intro.html'};
let payload = {updated: Date.now() / 1000, built: 1, videos: [ready, ...queued, failed, running]};
let apiAvailable = true;
let staticAvailable = true;
let completeAction;
const requests = [];
const fetch = (url, init = {}) => {
  requests.push({url, method: init.method || 'GET'});
  if (init.method === 'POST') {
    return new Promise(resolve => { completeAction = (ok, error) => resolve({
      ok, status: ok ? 200 : 409, json: () => Promise.resolve({error})
    }); });
  }
  const available = url === 'api/jobs' ? apiAvailable : staticAvailable;
  return Promise.resolve({ok: available, json: () => Promise.resolve(structuredClone(payload))});
};
let tick;
const flush = async () => { for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve)); };
function descendants(node) { return node.children.flatMap(child => [child, ...descendants(child)]); }
const buttons = () => descendants(list).filter(node => node.tagName === 'button');
const links = () => descendants(list).filter(node => node.tagName === 'a');
const select = value => { filter.value = value; filter.fire('change'); };
const search = value => { ids['job-search'].value = value; ids['job-search'].fire('input'); };

new Function('document', 'fetch', 'setInterval', source)(document, fetch, fn => { tick = fn; });
await flush();

assert.equal(list.children.length, 10, 'every queued lesson is visible, beyond the compact preview');
assert.equal(list.children[0].className, 'job working', 'running lesson appears first');
assert.ok(list.children[0].textContent.includes('Transcribing the audio'));
assert.ok(list.children[0].textContent.includes('1m 30s'));
assert.ok(list.children[1].textContent.includes('#1 in queue'));
assert.ok(list.children[9].textContent.includes('#9 in queue'));
assert.ok(ids['queue-summary'].textContent.includes('1 running · 9 waiting · 1 failed · 1 ready'));
assert.equal(buttons().length, 10, 'each active lesson can be cancelled');
assert.equal(ids['queue-notice'].hidden, true);

search('drawing');
assert.equal(list.children.length, 1, 'search includes course names');
assert.ok(list.children[0].textContent.includes('#9 in queue'), 'search does not renumber the queue');
search('chapter/lesson-2');
assert.equal(list.children.length, 1, 'search includes the source path');
search('missing');
assert.equal(list.children.length, 0);
assert.equal(ids['queue-empty'].textContent, 'No lessons match your search.');
search('');

select('failed');
assert.equal(list.children.length, 1);
assert.ok(list.textContent.includes('No audio track'));
assert.equal(buttons()[0].textContent, 'Retry processing');
assert.equal(links()[0].textContent, 'Open published version');
assert.equal(links()[0].href, 'drawing/lesson-old.html');

const retry = buttons()[0];
retry.focus();
retry.fire('click');
retry.fire('click');
assert.equal(requests.filter(r => r.method === 'POST').length, 1, 'duplicate action submissions are suppressed');
assert.equal(buttons()[0].disabled, true);
assert.equal(buttons()[0].textContent, 'Retrying…');
completeAction(false, 'The lesson has been removed');
await flush();
assert.ok(list.textContent.includes('The lesson has been removed'), 'server errors stay visible');
assert.equal(buttons()[0].disabled, false);
assert.equal(document.activeElement, buttons()[0], 'polling/rendering preserves keyboard focus');

buttons()[0].fire('click');
failed.state = 'queued'; failed.queue_position = 10; failed.error = null;
completeAction(true);
await flush();
assert.equal(list.children.length, 0, 'successful retry refreshes the catalog immediately');
assert.equal(filter.value, 'failed', 'actions keep the chosen filter');
select('queued');
assert.equal(list.children.length, 10);
const cancel = buttons()[0];
cancel.fire('click');
queued[0].state = 'cancelled'; queued[0].queue_position = null;
completeAction(true);
await flush();
select('cancelled');
assert.equal(list.children.length, 1);
assert.ok(list.textContent.includes('source video is still in the library'));
assert.equal(buttons()[0].textContent, 'Retry processing');
assert.ok(requests.some(r => r.url === 'api/lessons/' + queued[0].id + '/cancel' && r.method === 'POST'));

select('done');
assert.equal(links()[0].href, 'blender/lesson-intro.html');
assert.equal(buttons().length, 0, 'completed lessons do not offer a destructive action');
select('all');
assert.equal(list.children.length, 12);
payload.built = 2;
tick(); await flush();
assert.equal(filter.value, 'all', 'publication updates in place without a page reload');

apiAvailable = false;
tick(); await flush();
assert.equal(list.children.length, 12, 'static status remains readable without an API');
assert.equal(buttons().length, 0, 'read-only status does not offer actions that cannot work');
assert.ok(ids['queue-notice'].textContent.includes('Read-only'));
assert.ok(requests.some(r => r.url.startsWith('status.json?t=')));

staticAvailable = false;
tick(); await flush();
assert.equal(list.children.length, 12, 'connection loss preserves the last known queue');
assert.ok(ids['queue-notice'].textContent.includes('Connection lost'));
apiAvailable = true;
payload = {updated: Date.now() / 1000, videos: []};
tick(); await flush();
assert.equal(list.children.length, 0);
assert.ok(ids['queue-empty'].textContent.includes('Upload videos'));
assert.equal(ids['queue-notice'].hidden, true, 'connection recovery clears the notice');
console.log('queue.js runtime checks passed');
