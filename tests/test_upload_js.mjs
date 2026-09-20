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
const byId = {
  course, courses: list, drop, picker, choose, queue, message,
};

const fakeDocument = {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => makeElement(tag),
};
const fakeWindow = { addEventListener(type, fn) { (this.listeners ||= {})[type] = fn; } };

let coursesPayload = { courses: ['course_a', 'course_b'] };
let coursesOk = true;
const fakeFetch = () => (coursesOk
  ? Promise.resolve({ ok: true, json: () => Promise.resolve(coursesPayload) })
  : Promise.resolve({ ok: false, status: 503 }));

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

// 2. Nothing uploads without somewhere to put it.
picker.files = [file('a.mp4', 1048576)];
picker.fire('change');
assert.equal(sent.length, 0, 'no course name means no upload');
assert.ok(message.textContent.includes('name'), 'and it says why');
assert.equal(message.className, 'message bad');
assert.ok(course.focused, 'and puts the cursor where the fix is');

// 3. With a course, files upload one at a time.
course.value = 'cgboost launch pad 2';
picker.files = [file('01 car body.mp4', 2147483648), file('02 wheels.mp4', 1048576)];
picker.fire('change');
assert.equal(sent.length, 1, 'only the first upload starts');
assert.equal(queue.children.length, 2, 'but both are listed');
assert.equal(sent[0].method, 'PUT');
assert.equal(sent[0].url, 'api/library/cgboost%20launch%20pad%202/01%20car%20body.mp4',
  'course and file name are both encoded');
assert.equal(sent[0].body.name, '01 car body.mp4', 'the file itself is the body');

// 4. Progress reaches the bar and the label.
sent[0].upload.fire('progress', { lengthComputable: true, loaded: 1073741824, total: 2147483648 });
const firstRow = queue.children[0];
assert.equal(firstRow.children[2].children[0].style.width, '50%');
assert.ok(firstRow.children[1].textContent.includes('2.0 GB'), 'the size is readable');
assert.ok(firstRow.children[1].textContent.includes('50%'));

// 5. Finishing one starts the next, and only then.
sent[0].finish(201);
assert.equal(sent.length, 2, 'the second upload follows the first');
assert.ok(firstRow.children[1].textContent.includes('uploaded to cgboost launch pad 2'));

// 6. A name clash offers to replace rather than silently doing either.
sent[1].finish(409, JSON.stringify({ error: 'already there' }));
const secondRow = queue.children[1];
assert.equal(secondRow.className, 'failed');
assert.ok(secondRow.textContent.includes('already there'), 'the server reason is shown');
const again = secondRow.children.find((c) => c.className === 'again');
assert.ok(again, 'and a way to go ahead anyway');

again.click();
assert.equal(sent.length, 3, 'which retries');
assert.ok(sent[2].url.endsWith('?overwrite=1'), 'this time saying to overwrite');
assert.equal(secondRow.className, '', 'and the row stops looking failed');
sent[2].finish(201);
assert.ok(message.textContent.includes('builder'), 'finishing points at the home page');

// 7. A dropped connection is reported, not swallowed.
course.value = 'c';
picker.files = [file('x.mp4', 10)];
picker.fire('change');
sent[3].fire('error');
assert.equal(queue.children[2].className, 'failed');
assert.ok(queue.children[2].textContent.includes('connection'));

// 8. Dragging over the target highlights it.
drop.fire('dragover');
assert.ok(drop.classes.has('over'), 'the drop target lights up');
drop.fire('dragleave');
assert.ok(!drop.classes.has('over'));

// 9. A site with no API behind it says so rather than failing silently.
coursesOk = false;
const freshChoose = makeElement('button');
byId.choose = freshChoose;
byId.queue = makeElement('ul');
byId.message = makeElement('p');
run();
await flush();
await flush();
assert.equal(freshChoose.disabled, true, 'a static copy cannot take uploads');
assert.ok(byId.message.textContent.includes('cannot take uploads'));

console.log('upload.js runtime checks passed');
