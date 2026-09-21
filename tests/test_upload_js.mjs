// Runtime checks for the upload page's script, against a DOM stub. The parts
// worth pinning down are the ones a browser only shows you with a 2 GB file in
// hand: one upload at a time, progress, and what a name clash offers.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(process.argv[2], 'utf8');

function makeElement(tag) {
  const el = {
    tagName: tag,
    className: '',
    type: '',
    value: '',
    files: [],
    hidden: false,
    disabled: false,
    style: {},
    children: [],
    parentNode: null,
    listeners: {},
    _text: '',
    focused: false,
    clicked: 0,
    classes: new Set(),
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
    remove() {
      if (!this.parentNode) return;
      this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
      this.parentNode = null;
    },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    fire(type, event = {}) { (this.listeners[type] || []).forEach((fn) => fn({ preventDefault() {}, ...event })); },
    click() { this.clicked++; this.fire('click'); },
    focus() { this.focused = true; },
    get textContent() {
      return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text;
    },
    set textContent(value) { this._text = value; this.children = []; },
  };
  el.classList = {
    add: (n) => el.classes.add(n),
    remove: (n) => el.classes.delete(n),
    contains: (n) => el.classes.has(n),
  };
  return el;
}

const course = makeElement('input');
const list = makeElement('datalist');
const drop = makeElement('div');
const picker = makeElement('input');
const choose = makeElement('button');
const queue = makeElement('ul');
const message = makeElement('p');
const library = makeElement('section');
const libraryList = makeElement('div');
const byId = {
  course, courses: list, drop, picker, choose, queue, message, library, 'library-list': libraryList,
};

const fakeDocument = {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => makeElement(tag),
};
const fakeWindow = { addEventListener(type, fn) { (this.listeners ||= {})[type] = fn; } };

const video = (name, bytes) => ({ name, bytes: bytes ?? 1048576, mtime: 1 });
let libraryPayload = {
  courses: [
    { name: 'course_a', videos: [video('01 intro.mp4'), video('4.02 - reference board.mp4')] },
    { name: 'course_b', videos: [video('only.mp4')] },
  ],
};
let apiOk = true;
let availableSpace = 8 * 1024 ** 3;
let mutationError = null;
let reclaimGate = null, reclaimError = null;
const apiCalls = [];
const fakeFetch = (url, init) => {
  const method = (init && init.method) || 'GET';
  apiCalls.push({ method, url, body: init && init.body });
  if (!apiOk) {
    return Promise.resolve({ ok: false, status: 503, json: () => Promise.reject(new Error('nope')) });
  }
  if (url.startsWith('api/storage') && apiOk) {
    const storage = {id: 'library', label: 'Library', filesystem_id: 'disk', total_bytes: 100 * 1024 ** 3,
      free_bytes: availableSpace + 1024 ** 3, used_bytes: 0, buffer_bytes: 1024 ** 3,
      reserved_bytes: 0, available_bytes: availableSpace, max_upload_bytes: 16 * 1024 ** 3, accepting_uploads: availableSpace > 0};
    return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({locations: [storage], destination: storage})});
  }
  if (method === 'GET') {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(libraryPayload) });
  }
  if (mutationError) {
    return Promise.resolve({
      ok: false, status: 409, json: () => Promise.resolve({ error: mutationError }),
    });
  }
  if (url.endsWith('/reclaim')) {
    const item = libraryPayload.courses.flatMap(c => c.videos).find(v => url.includes('/' + v.lesson_id + '/'));
    const perform = () => {
      if (item.lesson_id === reclaimError) return {ok: false, status: 409, json: async () => ({error: 'The saved video does not match the original.'})};
      const removed_bytes = item.bytes;
      item.bytes = 0; item.source_reclaimed = true; item.reclaimable = false;
      return {ok: true, status: 200, json: async () => ({removed_bytes})};
    };
    if (reclaimGate) return new Promise(resolve => { reclaimGate.release = () => resolve(perform()); });
    return Promise.resolve(perform());
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
};

const rows = () => libraryList.children.flatMap((box) => box.children.filter((c) => c.className.startsWith('lib-row')));
const buttonIn = (row, text) => row.children.find((c) => c.tagName === 'button' && c.textContent === text);

const sent = [];
function FakeXHR() {
  this.listeners = {};
  this.upload = {
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    fire(type, event) { (this.listeners[type] || []).forEach((fn) => fn(event)); },
  };
  sent.push(this);
}
FakeXHR.prototype.open = function (method, url) { this.method = method; this.url = url; };
FakeXHR.prototype.addEventListener = function (type, fn) { (this.listeners[type] ||= []).push(fn); };
FakeXHR.prototype.send = function (body) { this.body = body; };
FakeXHR.prototype.fire = function (type) { (this.listeners[type] || []).forEach((fn) => fn({})); };
FakeXHR.prototype.finish = function (status, text) {
  this.status = status;
  this.responseText = text || '{}';
  this.fire('load');
};

const flush = () => new Promise((resolve) => setImmediate(resolve));
const file = (name, size) => ({ name, size });

function run() {
  new Function('document', 'window', 'fetch', 'XMLHttpRequest', source)(
    fakeDocument, fakeWindow, fakeFetch, FakeXHR,
  );
}

run();
await flush();
await flush();

// 1. The course box offers what is already there.
assert.deepEqual(list.children.map((o) => o.value), ['course_a', 'course_b'],
  'existing courses fill the datalist');

// 2. The library lists every course with its lessons, numbered in build order.
assert.equal(library.hidden, false, 'the library section shows');
assert.equal(libraryList.children.length, 2, 'one box per course');
assert.equal(libraryList.children[0].children[0].textContent, 'course_a');
assert.ok(libraryList.children[0].children[1].textContent.includes('reading order in Library'),
  'and says why the order matters');
assert.deepEqual(rows().map((r) => r.children[0].textContent), ['1.', '2.', '1.'],
  'lessons are numbered within their course');
assert.ok(rows()[1].children[1].textContent.includes('4.02'), 'and named');

// 3. Nothing uploads without somewhere to put it.
picker.files = [file('a.mp4', 1048576)];
picker.fire('change');
await flush();
assert.equal(sent.length, 0, 'no course name means no upload');
assert.ok(message.textContent.includes('name'), 'and it says why');
assert.equal(message.className, 'message bad');
assert.ok(course.focused, 'and puts the cursor where the fix is');

// 4. With a course, files upload one at a time.
course.value = 'cgboost launch pad 2';
picker.files = [file('01 car body.mp4', 2147483648), file('02 wheels.mp4', 1048576)];
picker.fire('change');
await flush();
assert.equal(sent.length, 1, 'only the first upload starts');
assert.equal(queue.children.length, 2, 'but both are listed');
assert.equal(sent[0].method, 'PUT');
assert.equal(sent[0].url, 'api/library/cgboost%20launch%20pad%202/01%20car%20body.mp4',
  'course and file name are both encoded');
assert.equal(sent[0].body.name, '01 car body.mp4', 'the file itself is the body');

// 5. Progress reaches the bar and the label.
sent[0].upload.fire('progress', { lengthComputable: true, loaded: 1073741824, total: 2147483648 });
const firstRow = queue.children[0];
assert.equal(firstRow.children[2].children[0].style.width, '50%');
assert.ok(firstRow.children[1].textContent.includes('2.0 GB'), 'the size is readable');
assert.ok(firstRow.children[1].textContent.includes('50%'));

// 6. Finishing one starts the next, and only then.
sent[0].finish(201);
await flush();
assert.equal(sent.length, 2, 'the second upload follows the first');
assert.ok(firstRow.children[1].textContent.includes('uploaded to cgboost launch pad 2'));

// 7. A name clash offers to replace rather than silently doing either.
sent[1].finish(409, JSON.stringify({ error: 'already there' }));
const secondRow = queue.children[1];
assert.equal(secondRow.className, 'failed');
assert.ok(secondRow.textContent.includes('already there'), 'the server reason is shown');
const again = secondRow.children.find((c) => c.className === 'again');
assert.ok(again, 'and a way to go ahead anyway');

again.click();
await flush();
assert.equal(sent.length, 3, 'which retries');
assert.ok(sent[2].url.endsWith('?overwrite=1'), 'this time saying to overwrite');
assert.equal(secondRow.className, '', 'and the row stops looking failed');
sent[2].finish(201);
assert.ok(message.textContent.includes('builder'), 'finishing points at the home page');

// 8. A dropped connection is reported, not swallowed.
course.value = 'c';
picker.files = [file('x.mp4', 10)];
picker.fire('change');
await flush();
sent[3].fire('error');
assert.equal(queue.children[2].className, 'failed');
assert.ok(queue.children[2].textContent.includes('connection'));

// 9. Dragging over the target highlights it.
drop.fire('dragover');
assert.ok(drop.classes.has('over'), 'the drop target lights up');
drop.fire('dragleave');
assert.ok(!drop.classes.has('over'));

// 10. Renaming asks the server to move the file, which is how ordering is fixed.
apiCalls.length = 0;
const target = rows()[1];
buttonIn(target, 'Rename file').click();
const input = target.children[1].children[0];
assert.equal(input.tagName, 'input', 'the name turns into a field');
assert.equal(input.value, '4.02 - reference board.mp4', 'pre-filled with the current name');
assert.ok(input.focused, 'and focused, so you can just type');
input.value = '02 - reference board.mp4';
buttonIn(target, 'Save').click();
await flush();
await flush();
const renamed = apiCalls.find((c) => c.method === 'POST');
assert.ok(renamed, 'a rename is a POST');
assert.equal(renamed.url, 'api/library/course_a/4.02%20-%20reference%20board.mp4');
assert.deepEqual(JSON.parse(renamed.body), { to_name: '02 - reference board.mp4' });
assert.ok(apiCalls.some((c) => c.method === 'GET'), 'and the listing is reloaded after');

// 11. Deleting takes two clicks, because it removes the source video for good.
apiCalls.length = 0;
const doomed = rows()[0];
buttonIn(doomed, 'Delete').click();
assert.equal(apiCalls.length, 0, 'the first click deletes nothing');
assert.ok(doomed.textContent.includes('Delete the original and remove this lesson'), 'it explains lesson removal');
assert.ok(buttonIn(doomed, 'Cancel'), 'and offers a way out');
buttonIn(doomed, 'Yes, delete').click();
await flush();
await flush();
const deleted = apiCalls.find((c) => c.method === 'DELETE');
assert.ok(deleted, 'the second click does it');
assert.equal(deleted.url, 'api/library/course_a/01%20intro.mp4');

// 12. A refused change says why and leaves the row alone.
mutationError = 'course_a/taken.mp4 is already there';
const clash = rows()[0];
buttonIn(clash, 'Rename file').click();
clash.children[1].children[0].value = 'taken.mp4';
buttonIn(clash, 'Save').click();
await flush();
await flush();
await flush();
assert.ok(message.textContent.includes('already there'), 'the server reason is surfaced');
assert.equal(message.className, 'message bad');
mutationError = null;

// A replacement waits behind an active upload and keeps the navigation guard.
const requestStart = sent.length;
const rowStart = queue.children.length;
course.value = 'course';
picker.files = [file('existing.mp4', 100), file('next.mp4', 100)];
picker.fire('change');
await flush();
sent[requestStart].finish(409);
await flush();
queue.children[rowStart].children.find(c => c.className === 'again').click();
assert.equal(sent.length, requestStart + 2, 'replacement is queued behind the next file');
sent[requestStart + 1].finish(201);
await flush();
assert.equal(sent.length, requestStart + 3, 'replacement starts when the active file finishes');
let guarded = false;
fakeWindow.listeners.beforeunload({preventDefault() { guarded = true; }});
assert.equal(guarded, true, 'replacement is included in the navigation guard');
sent[requestStart + 2].finish(201);
await flush(); await flush();
assert.equal(message.hidden, false, 'library refresh preserves the outcome message');

// Insufficient space is rejected before an XHR sends any video bytes.
const beforeCapacityCheck = sent.length;
availableSpace = 5;
picker.files = [file('too-large.mp4', 10)];
picker.fire('change'); await flush();
assert.equal(sent.length, beforeCapacityCheck, 'no transfer starts when preflight fails');
const rejected = queue.children[queue.children.length - 1];
assert.ok(rejected.textContent.includes('Not enough disk space'));
availableSpace = 100;
rejected.children.find(c => c.className === 'again').click(); await flush();
assert.equal(sent.length, beforeCapacityCheck + 1, 'retry checks the newly available space');
sent[sent.length - 1].finish(201); await flush();

// Reclamation is explicit, disables duplicate actions during verification, and
// preserves visible lessons; course cleanup reports partial failures accurately.
libraryPayload.courses[0].videos.forEach((v, index) => Object.assign(v, {
  lesson_id: 'lesson-' + index, build_id: 'build-' + index, reclaimable: true,
  href: 'course/lesson-' + index + '.html',
}));
libraryPayload.courses[1].videos[0].lesson_id = 'processing';
run(); await flush(); await flush();
apiCalls.length = 0;
const ready = rows()[0];
buttonIn(ready, 'Reclaim space').click();
assert.equal(apiCalls.length, 0, 'opening the cleanup confirmation does not delete anything');
assert.ok(ready.textContent.includes('lesson and saved video stay available'));
assert.ok(ready.textContent.includes('reprocessing'));
const confirmReclaim = buttonIn(ready, 'Reclaim original');
reclaimGate = {};
confirmReclaim.click(); await flush();
assert.ok(message.textContent.includes('Verifying saved video'));
assert.equal(confirmReclaim.disabled, true);
confirmReclaim.click(); await flush();
assert.equal(apiCalls.filter(c => c.url.endsWith('/reclaim')).length, 1, 'duplicate clicks cannot repeat cleanup');
assert.deepEqual(JSON.parse(apiCalls.find(c => c.url.endsWith('/reclaim')).body), {build_id: 'build-0'});
reclaimGate.release(); reclaimGate = null; await flush(); await flush();
assert.ok(rows()[0].textContent.includes('Original reclaimed'));
assert.equal(rows()[0].children[2].textContent, '0 B', 'removed copies do not claim to occupy any upload space');
assert.ok(rows()[0].children.some(c => c.tagName === 'a' && c.textContent === 'Open lesson'));
assert.equal(buttonIn(rows()[0], 'Rename file'), undefined);
assert.equal(buttonIn(rows()[0], 'Reclaim space'), undefined);
assert.ok(message.textContent.includes('duplicate uploads'));
assert.equal(buttonIn(rows()[2], 'Reclaim space').disabled, true, 'unfinished lessons cannot be reclaimed');

// Set up two ready originals to exercise sequential bulk cleanup and a refusal.
Object.assign(libraryPayload.courses[0].videos[0], {source_reclaimed: false, reclaimable: true, bytes: 1048576});
run(); await flush(); await flush();
const toolbar = libraryList.children[0].children.find(c => c.className === 'source-tools');
apiCalls.length = 0;
toolbar.children[0].click();
assert.equal(apiCalls.length, 0);
assert.ok(toolbar.textContent.includes('All lessons and saved videos stay available'));
reclaimError = 'lesson-0';
buttonIn(toolbar, 'Reclaim 2 originals').click(); await flush(); await flush(); await flush();
assert.deepEqual(apiCalls.filter(c => c.url.endsWith('/reclaim')).map(c => c.url), ['api/lessons/lesson-0/reclaim', 'api/lessons/lesson-1/reclaim']);
assert.ok(message.textContent.includes('does not match'));
assert.equal(libraryPayload.courses[0].videos[0].source_reclaimed, false);
assert.equal(libraryPayload.courses[0].videos[1].source_reclaimed, true);
assert.ok(rows()[0].children.every(c => c.tagName !== 'button' || !c.disabled), 'controls recover after a partial failure');
reclaimError = null;
const retainedRow = rows()[1];
buttonIn(retainedRow, 'Delete').click();
assert.ok(retainedRow.textContent.includes('Remove this lesson from the library?'));

// 13. A site with no API behind it says so rather than failing silently.
apiOk = false;
const freshChoose = makeElement('button');
const freshLibrary = makeElement('section');
byId.choose = freshChoose;
byId.queue = makeElement('ul');
byId.message = makeElement('p');
byId.library = freshLibrary;
run();
await flush();
await flush();
assert.equal(freshChoose.disabled, true, 'a static copy cannot take uploads');
assert.equal(freshLibrary.hidden, true, 'and shows no library it cannot manage');
assert.ok(byId.message.textContent.includes('cannot take uploads'));

console.log('upload.js runtime checks passed');
