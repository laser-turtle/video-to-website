import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

function element(tag) {
  return {tagName: tag, children: [], dataset: {}, style: {}, attributes: {}, listeners: {}, value: '', hidden: false, disabled: false, _text: '',
    appendChild(child) { this.children.push(child); return child; },
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(key, fn) { this.listeners[key] = fn; },
    fire(key) { this.listeners[key]?.({preventDefault() {}}); },
    click() { if (!this.disabled) this.fire('click'); },
    focus() { document.activeElement = this; }, select() {},
    querySelectorAll() { return nodes(this).filter(n => n.dataset.focusKey); },
    get textContent() { return this._text + this.children.map(n => n.textContent).join(''); },
    set textContent(value) { this._text = String(value); this.children = []; },
  };
}
function nodes(root) { return root.children.flatMap(n => [n, ...nodes(n)]); }
const ids = Object.fromEntries(['worker-list', 'worker-summary', 'worker-notice', 'worker-connect', 'worker-pairing', 'worker-filter', 'worker-empty', 'worker-refresh'].map(id => [id, element('div')]));
const document = {activeElement: null, createElement: element, getElementById: id => ids[id]};
const totals = {accepted_tasks: 10, processing_seconds: 90, media_seconds: 3600, interrupted_tasks: 1};
const task = {id: 'attempt', lesson_id: 'lesson', title: '4.02 - Shape', course: 'Blender', kind: 'transcribe', started: Date.now() / 1000 - 10,
  state: 'running', description: 'Step 3 · Clip', progress: {phase: 'speech', label: 'Transcribing audio', completed: 40, total: 100, updated: Date.now() / 1000}, metrics: {}};
let data = {workers: [
  {id: 'server', name: 'Library server', status: 'available', online: true, capabilities: [], details: {}, totals, active: [], recent: []},
  {id: 'a', name: 'Windows RTX 3090', platform: 'Windows', status: 'busy', online: true, capabilities: ['transcribe', 'clip'], details: {whisper: 'Automatic', clip_encoder: 'h264_nvenc'}, totals, active: [task], recent: []},
  {id: 'b', name: 'MacBook Air', platform: 'Darwin', status: 'offline', online: false, capabilities: ['transcribe'], details: {}, totals, active: [], recent: []},
]};
let unavailable = false;
const requests = [], timers = [];
const info = {filename: 'v2w-worker-123456abcdef.pyz', sha256: 'a'.repeat(64), bytes: 20000,
  guides: Object.fromEntries(['windows','macos','linux'].map(key => [key, {label: key, instructions: 'Native tools setup',
    command: key === 'macos' ? 'brew install ffmpeg whisper.cpp' : null, links: [{label: 'Downloads', url: 'https://example.com/tools'}]}]))};
function fetch(url, options) {
  requests.push({url, ...options});
  if (unavailable) return Promise.reject(new Error('offline'));
  if (url.endsWith('/download-info')) return Promise.resolve({ok: true, json: async () => structuredClone(info)});
  if (url.endsWith('/pairing')) return Promise.resolve({ok: true, json: async () => ({code: 'single-use-code', expires_in: 600})});
  if (options.method === 'POST') {
    const body = JSON.parse(options.body), worker = data.workers.find(w => w.id === url.split('/').at(-1));
    if (body.action === 'pause') { worker.paused = true; worker.status = 'draining'; }
    if (body.action === 'resume') { worker.paused = false; worker.status = worker.active.length ? 'busy' : 'available'; }
    if (body.action === 'stop') { worker.paused = true; worker.status = 'paused'; worker.active = []; }
    if (body.action === 'rename') worker.name = body.name;
    if (body.action === 'revoke') { worker.revoked = true; worker.online = false; worker.status = 'revoked'; }
    if (body.action === 'archive') { worker.archived = true; worker.revoked = true; worker.online = false; worker.active = []; worker.status = 'archived'; }
    if (body.action === 'restore') { worker.archived = false; worker.status = 'revoked'; }
    if (body.action === 'delete') data.workers = data.workers.filter(w => w.id !== worker.id);
    return Promise.resolve({ok: true, json: async () => ({saved: true})});
  }
  return Promise.resolve({ok: true, json: async () => structuredClone(data)});
}
const flush = async () => { for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve)); };
const control = key => nodes(ids['worker-list']).find(n => n.dataset.focusKey === key);
new Function('document', 'fetch', 'location', 'setTimeout', readFileSync(process.argv[2], 'utf8'))(
  document, fetch, {origin: 'http://lessons', pathname: '/workers.html'}, (fn, ms) => timers.push({fn, ms}));
await flush();
assert.equal(ids['worker-list'].children.length, 3);
assert.match(ids['worker-summary'].textContent, /1 helper connected/);
assert.match(ids['worker-list'].textContent, /40%/);
assert.match(ids['worker-list'].textContent, /Step 3 · Clip/);
assert.match(ids['worker-list'].textContent, /1h 0m/);
assert.ok(!control('server-pause'), 'CPU fallback cannot be paused from helper controls');
assert.ok(!control('server-delete'), 'CPU fallback cannot be deleted');
control('a-pause').click(); await flush();
assert.match(ids['worker-list'].textContent, /Finishing current task/);
assert.ok(control('a-resume'));
control('a-stop').click(); await flush();
assert.match(ids['worker-notice'].textContent, /restart on the server/);
assert.ok(!control('a-stop'));
control('a-rename').click();
const input = control('a-name'); input.value = '<img src=x> my desktop'; input.fire('input'); input.focus();
ids['worker-refresh'].click(); await flush();
assert.equal(control('a-name').value, '<img src=x> my desktop', 'polling preserves an edit in progress');
assert.equal(document.activeElement.dataset.focusKey, 'a-name', 'polling preserves focus');
control('a-save').click(); await flush();
assert.match(ids['worker-list'].textContent, /<img src=x> my desktop/);
assert.ok(!nodes(ids['worker-list']).some(n => n.tagName === 'img'), 'worker names are text, never HTML');
ids['worker-connect'].click(); await flush();
const setup = () => nodes(ids['worker-pairing']);
assert.equal(requests.filter(r => r.url.endsWith('/pairing')).length, 0, 'setup does not start the pairing timer');
assert.equal(setup().find(n => n.id === 'worker-download').download, info.filename);
assert.ok(setup().find(n => n.id === 'worker-download').href.includes(info.sha256));
const target = setup().find(n => n.id === 'worker-platform');
target.value = 'windows'; target.fire('change');
assert.equal(setup().find(n => n.id === 'worker-check-command').value, "py -3 'v2w-worker-123456abcdef.pyz' --check");
setup().find(n => n.dataset.focusKey === 'generate-pairing').click(); await flush();
const connectionCommand = setup().find(n => n.id === 'worker-connect-command');
assert.ok(connectionCommand.value.includes("--server 'http://lessons' --pairing-code 'single-use-code'"));
const gpu = setup().find(n => n.id === 'worker-nvidia'); gpu.checked = true; gpu.fire('change');
assert.ok(connectionCommand.value.endsWith('--clip-encoder h264_nvenc'));
target.value = 'macos'; target.fire('change');
assert.ok(connectionCommand.value.startsWith('python3 '));
assert.ok(!connectionCommand.value.includes('h264_nvenc'));
assert.ok(connectionCommand.value.endsWith('--clip-encoder libx264'), 'unchecking GPU overrides a previously saved NVENC setting');
assert.match(ids['worker-pairing'].textContent, /brew install ffmpeg whisper.cpp/);
assert.equal(requests.filter(r => r.url.endsWith('/pairing')).length, 1, 'changing platforms reuses the same code');
timers.find(t => t.ms === 600000).fn();
assert.equal(connectionCommand.value, '');
assert.equal(connectionCommand.hidden, true);
assert.ok(setup().find(n => n.id === 'worker-download'), 'expiry keeps the download/setup steps');
ids['worker-filter'].value = 'connected'; ids['worker-filter'].fire('change');
assert.equal(ids['worker-list'].children.length, 2);
unavailable = true; ids['worker-refresh'].click(); await flush();
assert.match(ids['worker-summary'].textContent, /Last known status/);
assert.match(ids['worker-notice'].textContent, /controls are disabled/);
assert.equal(control('a-resume').disabled, true);
assert.equal(ids['worker-connect'].disabled, true);
assert.equal(setup().find(n => n.dataset.focusKey === 'generate-pairing').disabled, true);
const before = requests.length; control('a-resume').click(); assert.equal(requests.length, before);
unavailable = false; ids['worker-refresh'].click(); await flush();
control('a-revoke').click(); await flush();
assert.equal(ids['worker-list'].children.length, 1);
assert.match(ids['worker-notice'].textContent, /Access revoked/);
ids['worker-filter'].value = 'current'; ids['worker-filter'].fire('change');
assert.ok(control('a-archive'), 'revoked workers still have cleanup controls');
control('a-archive').click(); await flush();
assert.equal(ids['worker-list'].children.length, 2, 'archiving hides the computer from the default list');
ids['worker-filter'].value = 'archived'; ids['worker-filter'].fire('change');
assert.equal(ids['worker-list'].children.length, 1);
assert.match(ids['worker-list'].textContent, /Archived/);
assert.match(ids['worker-list'].textContent, /10accepted tasks/, 'archiving preserves contribution history');
control('a-restore').click(); await flush();
assert.equal(ids['worker-list'].children.length, 0);
assert.equal(ids['worker-empty'].textContent, 'No archived computers.');
ids['worker-filter'].value = 'current'; ids['worker-filter'].fire('change');
assert.ok(control('a-delete'));
assert.ok(!control('a-resume'), 'restoring does not reenable revoked access');
data.workers.find(w => w.id === 'a').deletion_blocked_reason = 'Results are needed by an unfinished lesson. Archive it instead.';
ids['worker-refresh'].click(); await flush();
control('a-delete').click();
assert.equal(control('a-delete-confirm').disabled, true);
assert.match(ids['worker-list'].textContent, /unfinished lesson/);
control('a-delete-cancel').click();
delete data.workers.find(w => w.id === 'a').deletion_blocked_reason;
ids['worker-refresh'].click(); await flush();
const beforeDelete = requests.filter(r => r.method === 'POST').length;
control('a-delete').click();
assert.equal(requests.filter(r => r.method === 'POST').length, beforeDelete, 'deletion requires an explicit confirmation click');
control('a-delete-cancel').click();
assert.ok(control('a-delete'));
control('a-delete').click(); control('a-delete-confirm').click(); await flush();
assert.equal(ids['worker-list'].children.length, 2);
assert.ok(!data.workers.some(w => w.id === 'a'));
assert.match(ids['worker-notice'].textContent, /contribution history deleted/);
console.log('Workers UI passed: pairing, controls, archive/restore/delete, checkpoint protection, focus, safe text and stale status.');
