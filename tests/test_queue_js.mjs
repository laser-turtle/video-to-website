import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../src/video_to_website/assets/progress.js', import.meta.url), 'utf8') + '\n' + readFileSync(process.argv[2], 'utf8');
function element(tag) {
  return {
    tagName: tag, children: [], dataset: {}, style: {}, listeners: {}, attributes: {},
    value: '', hidden: false, disabled: false, _text: '',
    appendChild(child) { this.children.push(child); return child; },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    fire(name, event = {}) { this.listeners[name]?.({preventDefault() {}, ...event}); },
    showModal() { this.open = true; },
    close() { this.open = false; this.fire('close'); },
    focus() { document.activeElement = this; },
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); },
    set textContent(text) { this._text = text; this.children = []; },
  };
}
const ids = {};
for (const id of ['job-list', 'queue-summary', 'job-filter', 'job-search', 'queue-empty', 'queue-notice', 'queue-updated', 'queue-course', 'queue-course-name', 'queue-all-courses', 'provider-pauses']) {
  ids[id] = element('div');
}
for (const id of ['processing-settings', 'processing-settings-form', 'processing-settings-lesson', 'processing-settings-current', 'processing-settings-default', 'processing-settings-chunk', 'processing-settings-hint', 'processing-settings-error', 'processing-settings-save', 'processing-settings-reload', 'processing-settings-close']) ids[id] = element('div');
const list = ids['job-list'];
const filter = ids['job-filter'];
filter.value = 'active';
filter.options = ['active', 'queued', 'blocked', 'failed', 'cancelled', 'done', 'all'].map(value => ({ value }));
const document = {
  hidden: false, activeElement: null, listeners: {},
  getElementById(id) { return ids[id]; },
  createElement: element,
  querySelector(selector) {
    const job = /data-job-id="([^"]+)"/.exec(selector)?.[1];
    return descendants(list).find(node => node.dataset.jobId === job && node.dataset.action === 'settings');
  },
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
let processingProfile;
const requests = [];
const fetch = (url, init = {}) => {
  requests.push({url, method: init.method || 'GET', body: init.body && JSON.parse(init.body)});
  if (init.method === 'POST') {
    return new Promise(resolve => { completeAction = (ok, error) => resolve({
      ok, status: ok ? 200 : 409, json: () => Promise.resolve({error})
    }); });
  }
  if (url.endsWith('/settings')) return Promise.resolve({ok: true, json: async () => structuredClone(processingProfile)});
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

assert.equal(list.children.length, 10, 'every queued lesson is visible');
assert.equal(list.children[0].className, 'job working', 'running lesson appears first');
assert.ok(list.children[0].textContent.includes('Transcribing the audio'));
assert.ok(list.children[0].textContent.includes('1m 30s'));
assert.ok(list.children[1].textContent.includes('#1 in queue'));
assert.ok(list.children[9].textContent.includes('#9 in queue'));
assert.ok(ids['queue-summary'].textContent.includes('1 running · 9 waiting · 1 failed · 1 ready'));
assert.equal(buttons().length, 10, 'each active lesson can be cancelled');
assert.equal(ids['queue-notice'].hidden, true);

running.progress = {phase: 'speech', label: 'Transcribing audio', fraction: .45,
  completed: 45, total: 100, unit: 'percent', eta_seconds: 120, updated: Date.now() / 1000};
tick(); await flush();
assert.ok(list.children[0].textContent.includes('45%'));
assert.ok(list.children[0].textContent.includes('About 2m left for transcription'));
assert.equal(list.children[0].children.find(n => n.className === 'bar').attributes['aria-valuenow'], '45');
running.progress.updated -= 180;
tick(); await flush();
assert.ok(list.children[0].textContent.includes('Estimate paused'));
assert.ok(!list.children[0].textContent.includes('left for transcription'));
delete running.progress;

running.executions = [
  {state: 'running', worker_id: id(41), worker_name: 'RTX 3090', description: 'Step 3 · Clip', kind: 'clip'},
  {state: 'running', worker_id: id(42), worker_name: 'MacBook Air', description: 'Step 4 · Screenshots', kind: 'frame'},
  {state: 'pending', description: 'Step 5 · Clip', kind: 'clip'},
  {state: 'fallback', description: 'Step 6 · Clip', kind: 'clip'},
];
tick(); await flush();
assert.match(list.children[0].textContent, /RTX 3090/);
assert.match(list.children[0].textContent, /MacBook Air/);
assert.match(list.children[0].textContent, /Step 3 · Clip/);
assert.match(list.children[0].textContent, /1 operation ready for a processor/);
assert.match(list.children[0].textContent, /1 operation waiting for server fallback/);
assert.ok(links().some(link => link.href === 'workers.html#worker-' + id(41)));
assert.ok(links().some(link => link.href === 'workers.html#worker-' + id(42)));
delete running.executions;

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
assert.ok(list.textContent.includes('lesson video is retained'));
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

// Home-page counts link to a stable course scope, independently of names.
payload = {videos: [
  {...running, course_id: 'course-a', course_slug: 'a', course: 'Same name'},
  {...failed, state: 'failed', course_id: 'course-a', course_slug: 'a', course: 'Same name'},
  {...queued[0], state: 'queued', course_id: 'course-b', course_slug: 'b', course: 'Same name'},
]};
filter.value = 'active';
new Function('document', 'fetch', 'setInterval', 'location', source)(document, fetch, fn => { tick = fn; }, {search: '?course=course-a&state=all'});
await flush();
assert.equal(list.children.length, 2);
assert.equal(ids['queue-course'].hidden, false);
assert.equal(ids['queue-course-name'].textContent, 'Course: Same name');
assert.ok(ids['queue-summary'].textContent.includes('1 running · 0 waiting · 1 failed'));
select('failed'); assert.equal(list.children.length, 1, 'state filter keeps the course scope');
ids['queue-all-courses'].fire('click');
assert.equal(ids['queue-course'].hidden, true);
select('all'); assert.equal(list.children.length, 3, 'scope can be cleared');
new Function('document', 'fetch', 'setInterval', 'location', source)(document, fetch, fn => { tick = fn; }, {search: '?q=Drawing&state=failed'});
payload = {videos: [{...failed, state: 'failed'}, running]}; await flush(); tick(); await flush();
assert.equal(ids['job-search'].value, 'Drawing');
assert.equal(list.children.length, 1, 'legacy status links use visible search text');

search(''); select('active');
const pause = {id: 'f'.repeat(32), provider: 'anthropic', name: 'Anthropic API', reason: 'credits', message: 'Add credits, then resume requests.'};
payload = {provider_pauses: [pause], videos: [{...running, state: 'blocked', blocked: pause}]};
tick(); await flush();
assert.equal(list.children.length, 1, 'active queue includes paused lessons');
assert.ok(list.textContent.includes('Waiting for API credits'));
assert.ok(!list.textContent.includes('left for transcription'), 'paused lessons do not show an active estimate');
assert.ok(ids['queue-summary'].textContent.includes('1 waiting for API access'));
assert.equal(buttons()[0].textContent, 'Cancel processing', 'waiting workflows remain cancellable');
const resume = () => descendants(ids['provider-pauses']).find(node => node.tagName === 'button');
assert.equal(resume().textContent, 'Resume requests');
resume().focus(); tick(); await flush();
assert.equal(document.activeElement, resume(), 'provider controls retain keyboard focus across polls');
const posts = requests.filter(r => r.method === 'POST').length;
resume().fire('click'); resume().fire('click');
assert.equal(requests.filter(r => r.method === 'POST').length, posts + 1);
assert.equal(requests.at(-1).url, 'api/providers/anthropic/resume');
assert.deepEqual(requests.at(-1).body, {pause_id: pause.id});
completeAction(false, 'The provider paused again. Refresh its status before resuming.'); await flush();
assert.ok(ids['provider-pauses'].textContent.includes('paused again'));
assert.equal(resume().disabled, false);
resume().fire('click'); payload = {provider_pauses: [], videos: [running]}; completeAction(true); await flush();
assert.equal(ids['provider-pauses'].children.length, 0);
payload = {provider_pauses: [pause], videos: [{...running, state: 'blocked', blocked: pause}]};
apiAvailable = false; staticAvailable = true; tick(); await flush();
assert.equal(resume().disabled, true, 'static exports cannot claim to resume the provider');

// Processing settings are a stable dialog while the queue continues polling.
apiAvailable = true; filter.value = 'failed';
const configurable = {id: id(60), build_id: id(70), course: 'polygon_runway', title: 'polygon-runway-katana',
  state: 'failed', error: 'response hit max_tokens before finishing', updated: 1,
  processing: {chunk_minutes: 25, default_chunk_minutes: 25, custom_chunk_minutes: null}};
processingProfile = {id: id(60), build_id: id(70), state: 'failed', updated: 1,
  chunk_minutes: 25, default_chunk_minutes: 25, custom_chunk_minutes: null, duration: 480};
payload = {videos: [configurable]}; tick(); await flush();
const settings = () => buttons().find(node => node.dataset.action === 'settings');
assert.equal(settings().textContent, 'Adjust settings & retry');
settings().fire('click'); await flush();
assert.equal(ids['processing-settings'].open, true);
assert.equal(ids['processing-settings-chunk'].value, '4', 'suggestion accounts for actual duration, not just configured chunk size');
assert.equal(ids['processing-settings-default'].checked, false);
ids['processing-settings-chunk'].value = '3.5'; ids['processing-settings-chunk'].focus();
tick(); await flush();
assert.equal(ids['processing-settings-chunk'].value, '3.5', 'polls do not erase an unsaved edit');
assert.equal(document.activeElement, ids['processing-settings-chunk']);
const beforeSettings = requests.filter(request => request.method === 'POST').length;
ids['processing-settings-form'].fire('submit'); ids['processing-settings-form'].fire('submit');
assert.equal(requests.filter(request => request.method === 'POST').length, beforeSettings + 1);
assert.deepEqual(requests.at(-1).body, {build_id: id(70), chunk_minutes: 3.5});
assert.equal(ids['processing-settings-close'].disabled, true);
completeAction(false, 'This lesson changed. Reload the latest settings.'); await flush();
assert.equal(ids['processing-settings-chunk'].value, '3.5');
assert.equal(ids['processing-settings-reload'].hidden, false);
processingProfile = {...processingProfile, build_id: id(71), updated: 2, chunk_minutes: 10, custom_chunk_minutes: 10};
configurable.build_id = id(71); configurable.updated = 2;
tick(); await flush();
assert.equal(ids['processing-settings-save'].disabled, true, 'a stale edit cannot replace a newer attempt');
ids['processing-settings-reload'].fire('click'); await flush();
assert.equal(ids['processing-settings-chunk'].value, '3.5', 'explicit refresh preserves the proposed value');
ids['processing-settings-form'].fire('submit');
assert.deepEqual(requests.at(-1).body, {build_id: id(71), chunk_minutes: 3.5});
configurable.build_id = id(72); configurable.updated = 3; configurable.state = 'queued'; configurable.error = null;
completeAction(true); await flush();
assert.equal(ids['processing-settings'].open, false);
filter.value = 'active'; filter.fire('change');
processingProfile = {...processingProfile, build_id: id(72), updated: 3, state: 'queued', chunk_minutes: 3.5, custom_chunk_minutes: 3.5};
settings().fire('click'); await flush();
assert.equal(ids['processing-settings-save'].disabled, true, 'active workflows cannot be changed in place');
assert.ok(ids['processing-settings-error'].textContent.includes('Cancel it'));
ids['processing-settings-close'].fire('click');
configurable.state = 'failed'; filter.value = 'failed'; processingProfile.state = 'failed'; tick(); await flush();
settings().fire('click'); await flush();
ids['processing-settings-default'].checked = true; ids['processing-settings-default'].fire('change');
assert.equal(ids['processing-settings-chunk'].disabled, true);
ids['processing-settings-form'].fire('submit');
assert.deepEqual(requests.at(-1).body, {build_id: id(72), chunk_minutes: null}, 'default reset is explicit');
completeAction(true); await flush();
console.log('queue.js runtime checks passed');
