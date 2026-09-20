import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source = readFileSync(process.argv[2], 'utf8');
const gib = 1024 ** 3;
function element() {
  return {children: [], listeners: {}, attributes: {}, style: {}, _text: '', value: '', hidden: false,
    appendChild(child) { this.children.push(child); },
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(key, fn) { this.listeners[key] = fn; },
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); },
    set textContent(value) { this._text = value; this.children = []; },
  };
}
const ids = {'storage-panel': element(), 'storage-locations': element(), 'storage-message': element(), course: element()};
const document = {getElementById: id => ids[id], createElement: element, addEventListener() {}};
const location = {id: 'library', label: 'Main library', filesystem_id: 'same-disk', total_bytes: 100 * gib,
  used_bytes: 90 * gib, free_bytes: 10 * gib, buffer_bytes: gib, reserved_bytes: 2 * gib,
  available_bytes: 7 * gib, accepting_uploads: true, low_space: true, max_upload_bytes: 16 * gib};
let data = {locations: [location, {...location, id: 'archive', label: 'Archive'}], destination: location};
let online = true;
const urls = [];
const fetch = (url) => {
  urls.push(url);
  return Promise.resolve({ok: online, status: online ? 200 : 503, json: async () => online ? structuredClone(data) : {error: 'Disk unavailable'}});
};
let tick;
const storage = new Function('document', 'fetch', 'setInterval', source + '\nreturn V2WStorage;')(document, fetch, fn => { tick = fn; });
const flush = async () => { for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve)); };
await flush();
const text = ids['storage-locations'].textContent;
assert.ok(text.includes('10.0 GiB free'));
assert.ok(text.includes('1.0 GiB kept free'));
assert.ok(text.includes('2.0 GiB reserved for uploads in progress'));
assert.ok(text.includes('7.0 GiB available for new uploads'));
assert.ok(text.includes('Shares disk space with Archive'), 'shared folders do not imply extra disk capacity');
assert.equal(ids['storage-locations'].children.length, 2);
assert.equal(ids['storage-message'].hidden, true);
await storage.check('My course', 6 * gib);
assert.ok(urls.includes('api/storage?course=My%20course'));
await assert.rejects(storage.check('My course', 8 * gib), /Not enough disk space/);
await assert.rejects(storage.check('My course', 17 * gib), /no larger than/);

location.available_bytes = 0; location.accepting_uploads = false;
tick(); await flush();
assert.ok(ids['storage-locations'].textContent.includes('Uploads paused'));
await assert.rejects(storage.check('My course', 1), /Not enough disk space/);

online = false;
await storage.refresh();
assert.ok(ids['storage-message'].textContent.includes('Showing the last known capacity'));
await assert.rejects(storage.check('My course', 1), /Disk unavailable/, 'uploads do not bypass failed capacity checks');
online = true; location.available_bytes = 7 * gib; location.accepting_uploads = true;
await storage.refresh();
assert.equal(ids['storage-message'].hidden, true);
console.log('storage.js runtime checks passed');
