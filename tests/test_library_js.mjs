import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const source = readFileSync(process.argv[2], 'utf8');
const readerSource = readFileSync('src/video_to_website/assets/reader-state.js', 'utf8');
function element(tag) {
  return {tagName: tag, children: [], dataset: {}, listeners: {}, attributes: {}, hidden: false,
    value: '', disabled: false, _text: '',
    get isConnected() { return this.connected || Boolean(this.parent?.isConnected); },
    appendChild(child) { child.parent = this; this.children.push(child); return child; },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    fire(name, event = {}) { this.listeners[name]?.({preventDefault() {}, ...event}); },
    click() { if (!this.disabled) this.fire('click'); },
    focus() { if (this.isConnected) document.activeElement = this; }, select() {},
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); },
    set textContent(value) { this._text = value; this.children.forEach(c => { c.parent = null; }); this.children = []; },
  };
}
const ids = {};
for (const id of ['library-courses', 'library-search', 'library-message', 'library-count', 'library-refresh', 'library-reset', 'library-empty', 'library-group', 'library-density', 'library-sort-courses']) ids[id] = element('div');
Object.values(ids).forEach(node => { node.connected = true; });
const document = {activeElement: null, body: {dataset: {}}, addEventListener() {}, getElementById: id => ids[id], createElement: element};
let data = {revision: '1', courses: [
  {id: 'a', title: 'Blender', source_path: 'Blender', href: 'blender/index.html', videos: [
    {id: 'a1', title: '4.02 - Shape', source_title: '4.02 - Shape', source_name: '4.02 - Shape.mp4', description: 'Build the base form', numbering: [4, 2], duration: 120, state: 'ready', href: 'blender/shape.html'},
    {id: 'a2', title: '4.03 - Detail', source_title: '4.03 - Detail', source_name: '4.03 - Detail.mp4', description: 'Bevel the edges', numbering: [4, 3], duration: 30, state: 'ready', href: 'blender/detail.html'},
  ]},
  {id: 'b', title: 'Drawing', source_path: 'Drawing', videos: [
    {id: 'b1', title: '01 Sketch', source_title: '01 Sketch', source_name: '01 Sketch.mp4', state: 'queued'},
  ]},
]};
let offline = false;
let conflict = false;
const requests = [];
function fetch(url, init = {}) {
  const body = init.body && JSON.parse(init.body);
  requests.push({url, method: init.method || 'GET', body});
  if (offline && url === 'api/catalog') return Promise.resolve({ok: false, status: 503, json: async () => ({})});
  if (url === 'site.json') return Promise.resolve({ok: true, json: async () => [{slug: 'static', title: 'Static course', lessons: [{slug: 'one', title: 'Model description', source_name: '4.02 - Static.mp4', numbering: [4, 2]}]}]});
  if (init.method === 'POST') {
    if (conflict) return Promise.resolve({ok: false, status: 409, json: async () => ({error: 'The library changed. Reload it before applying this change.'})});
    assert.equal(body.revision, data.revision, 'each edit uses the latest catalog revision');
    const parts = url.split('/');
    if (parts.at(-1) === 'title') {
      const item = parts[2] === 'courses' ? data.courses.find(c => c.id === parts[3]) : data.courses.flatMap(c => c.videos).find(v => v.id === parts[3]);
      item.title = body.title === null ? item.source_title : body.title;
      item.custom_title = body.title !== null;
    } else if (['chapter', 'move'].includes(parts.at(-1))) {
      const course = data.courses.find(c => c.id === parts[3]);
      const moving = course.videos.filter(v => body.ids.includes(v.id));
      const remaining = course.videos.filter(v => !body.ids.includes(v.id));
      if (parts.at(-1) === 'chapter') {
        moving.forEach(v => { v.chapter_override = body.chapter; v.chapter = body.chapter === null ? v.numbering?.[0] ?? null : body.chapter === -1 ? null : body.chapter; });
        const groups = new Map();
        [...remaining, ...moving].forEach(v => { const chapter = v.chapter ?? v.numbering?.[0] ?? null; if (!groups.has(chapter)) groups.set(chapter, []); groups.get(chapter).push(v); });
        course.videos = [...groups.values()].flat();
      } else {
        const at = body.before === null ? remaining.length : remaining.findIndex(v => v.id === body.before);
        course.videos = [...remaining.slice(0, at), ...moving, ...remaining.slice(at)];
      }
    } else if (parts.at(-1) === 'order') {
      const course = parts.length === 5 ? data.courses.find(c => c.id === parts[3]) : null;
      const items = course ? course.videos : data.courses;
      let ordered;
      if (body.mode === 'source') ordered = [...items].sort((a, b) => a.id.localeCompare(b.id));
      else if (body.mode) {
        // Return a server-selected order; Python tests cover each comparator.
        ordered = [...items].reverse();
      } else ordered = body.ids.map(id => items.find(item => item.id === id));
      if (course) course.videos = ordered; else data.courses = ordered;
    }
    data.revision = String(Number(data.revision) + 1);
    return Promise.resolve({ok: true, json: async () => ({catalog: structuredClone(data)})});
  }
  return Promise.resolve({ok: true, json: async () => structuredClone(data)});
}
const flush = async () => { for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve)); };
function nodes(root = ids['library-courses']) { return root.children.flatMap(c => [c, ...nodes(c)]); }
function control(key) { return nodes().find(n => n.dataset.focusKey === key); }
function input() { const form = nodes().find(n => n.className === 'library-edit'); return form && nodes(form).find(n => n.tagName === 'input'); }
function titleOrder() { return nodes().filter(n => n.tagName === 'h3').map(n => n.textContent); }
const state = new Map();
const storage = {getItem: key => state.get(key) || null, setItem: (key, value) => state.set(key, value)};
const start = () => new Function('document', 'fetch', 'location', 'localStorage', 'window', readerSource + '\n' + source + '\nreturn V2WReaderState;')(document, fetch, {search: '?course=a'}, storage, {addEventListener() {}});
const readerState = start();
await flush();
assert.equal(ids['library-courses'].children.length, 2);
assert.equal(ids['library-count'].textContent, '2 courses · 3 lessons');
assert.ok(ids['library-courses'].textContent.includes('4.02 - Shape'));
assert.ok(ids['library-courses'].textContent.includes('Build the base form'));
assert.equal(control('lesson-a1-up').disabled, true);
assert.equal(control('course-b-down').disabled, true);
assert.equal(nodes().filter(n => n.className === 'managed-chapter').length, 1);
assert.ok(ids['library-courses'].textContent.includes('Chapter 4'));

control('course-a-rename').click();
input().value = 'Modelling essentials';
control('edit-save').click(); await flush();
assert.equal(data.courses[0].title, 'Modelling essentials');
assert.equal(data.courses[0].source_path, 'Blender', 'display rename does not move the folder');
assert.ok(ids['library-message'].textContent.includes('saved'));

control('lesson-a1-rename').click();
input().value = 'My starting point';
control('edit-save').click(); await flush();
assert.equal(data.courses[0].videos[0].title, 'My starting point');
assert.ok(ids['library-courses'].textContent.includes('4.02 - Shape.mp4'), 'original filename stays visible');
control('lesson-a1-rename').click();
control('edit-reset').click(); await flush();
assert.equal(data.courses[0].videos[0].title, '4.02 - Shape');

control('lesson-a1-down').click(); await flush();
assert.deepEqual(data.courses[0].videos.map(v => v.id), ['a2', 'a1']);
assert.deepEqual(titleOrder().slice(0, 2), ['4.03 - Detail', '4.02 - Shape']);
control('lesson-a1-move').click(); input().value = '1';
control('edit-save').click(); await flush();
assert.deepEqual(data.courses[0].videos.map(v => v.id), ['a1', 'a2']);
control('course-a-down').click(); await flush();
assert.deepEqual(data.courses.map(c => c.id), ['b', 'a']);
ids['library-reset'].click(); await flush();
assert.deepEqual(data.courses.map(c => c.id), ['a', 'b']);

ids['library-search'].value = 'Bevel'; ids['library-search'].fire('input');
assert.equal(ids['library-courses'].children.length, 1);
assert.deepEqual(titleOrder(), ['4.03 - Detail']);
assert.equal(control('lesson-a2-up').disabled, true, 'filtered views cannot submit a partial reorder');
assert.equal(control('course-a-sort-apply').disabled, true);
assert.equal(ids['library-sort-courses'].disabled, true);
assert.ok(ids['library-courses'].textContent.includes('Clear search to reorder'));
ids['library-search'].value = ''; ids['library-search'].fire('input');

control('lesson-a1-rename').click(); input().value = '';
let before = requests.length;
control('edit-save').click();
assert.equal(requests.length, before, 'empty titles fail validation before the request');
input().value = 'Unsaved title'; conflict = true;
control('edit-save').click(); await flush();
assert.ok(ids['library-message'].textContent.includes('library changed'));
assert.equal(input().value, 'Unsaved title', 'a conflict preserves the edit for review');
conflict = false; input().fire('keydown', {key: 'Escape'});
assert.equal(input(), undefined);

// Selecting a sort is local; only Save sorted order mutates the full course.
let sort = control('course-a-sort');
before = requests.length;
sort.value = 'heuristic'; sort.fire('change');
assert.equal(requests.length, before);
control('course-a-sort-apply').click(); await flush();
assert.deepEqual(requests.at(-1).body, {revision: String(Number(data.revision) - 1), mode: 'heuristic'});
assert.equal(requests.at(-1).url, 'api/catalog/courses/a/order');
assert.ok(ids['library-message'].textContent.includes('Reading order saved'));
assert.equal(document.activeElement.dataset.focusKey, 'course-a-sort-apply');
for (const mode of ['title', 'filename', 'shortest', 'longest']) {
  sort = control('course-a-sort'); sort.value = mode; sort.fire('change');
  control('course-a-sort-apply').click(); await flush();
  assert.equal(requests.at(-1).body.mode, mode);
  assert.equal(control('course-a-sort').value, mode, 'chosen method survives the updated catalog');
}
conflict = true; sort = control('course-a-sort'); sort.value = 'filename'; sort.fire('change');
control('course-a-sort-apply').click(); await flush();
assert.ok(ids['library-message'].textContent.includes('library changed'));
assert.equal(control('course-a-sort').value, 'filename');
assert.equal(control('course-a-sort-apply').disabled, false);
conflict = false;
ids['library-sort-courses'].click(); await flush();
assert.equal(requests.at(-1).url, 'api/catalog/courses/order');
assert.equal(requests.at(-1).body.mode, 'title');

// Search opens chapters temporarily, and grouping never changes the order.
control('course-a-collapse').click();
assert.equal(nodes().find(n => n.className === 'managed-chapter').open, false);
ids['library-search'].value = 'chapter 4'; ids['library-search'].fire('input');
assert.equal(nodes().find(n => n.className === 'managed-chapter').open, true);
assert.equal(control('course-a-sort-apply').disabled, true);
ids['library-search'].value = ''; ids['library-search'].fire('input');
assert.equal(nodes().find(n => n.className === 'managed-chapter').open, false);
const readingOrder = [...titleOrder()];
before = requests.length;
ids['library-group'].value = 'flat'; ids['library-group'].fire('change');
ids['library-density'].value = 'compact'; ids['library-density'].fire('change');
assert.equal(requests.length, before, 'view preferences never submit a reorder');
assert.equal(nodes().filter(n => n.className === 'managed-chapter').length, 0);
assert.deepEqual(titleOrder(), readingOrder);
assert.equal(ids['library-courses'].dataset.density, 'compact');
assert.equal(readerState.view('library').density, 'compact');
ids['library-group'].value = 'chapters'; ids['library-group'].fire('change');
control('course-a-expand').click();
let chapter = nodes().find(n => n.className === 'managed-chapter');
chapter.open = false; chapter.fire('toggle');
assert.equal(readerState.view('library')['chapter:a|4:1'], false);
control('lesson-a1-rename').click();
assert.equal(nodes().find(n => n.className === 'managed-chapter').open, true, 'editing reveals the lesson');
assert.equal(control('course-a-sort-apply').disabled, true, 'sorting cannot discard an unsaved title');
assert.equal(ids['library-group'].disabled, true);
control('edit-cancel').click();

// Scattered chapter members merge; bulk selection survives filtering and conflicts.
const managed = data.courses.find(c => c.id === 'a');
const a1 = managed.videos.find(v => v.id === 'a1'), a2 = managed.videos.find(v => v.id === 'a2');
managed.videos = [a1, {id: 'a3', title: '5.01 - Another chapter', source_name: '5.01 - Another chapter.mp4', numbering: [5, 1], state: 'queued'}, a2];
data.revision = String(Number(data.revision) + 1);
ids['library-refresh'].click(); await flush();
assert.equal(nodes().filter(n => n.dataset.chapterKey?.startsWith('a|')).length, 2);
assert.deepEqual(titleOrder().filter(t => t.startsWith('4.') || t.startsWith('5.')), [a1.title, a2.title, '5.01 - Another chapter']);
control('course-a-select-4:1').click();
assert.ok(ids['library-courses'].textContent.includes('2 selected'));
ids['library-search'].value = 'Another'; ids['library-search'].fire('input');
assert.ok(ids['library-courses'].textContent.includes('2 hidden by search'));
let target = control('course-a-bulk-target'); target.value = '5'; target.fire('change');
conflict = true; control('course-a-bulk-apply').click(); await flush();
assert.deepEqual(requests.at(-1).body.ids, ['a1', 'a2'], 'the explicit selection includes hidden lessons');
assert.equal(requests.at(-1).body.chapter, 5);
assert.ok(ids['library-courses'].textContent.includes('2 selected'), 'failed saves keep the selection');
assert.equal(control('course-a-bulk-target').value, '5');
conflict = false; control('course-a-bulk-apply').click(); await flush();
assert.ok(ids['library-courses'].textContent.includes('0 selected'));
ids['library-search'].value = ''; ids['library-search'].fire('input');
control('course-a-bulk-select').click();
target = control('course-a-bulk-target'); target.value = 'new'; target.fire('change');
let chapterNumber = control('course-a-bulk-number');
before = requests.length; chapterNumber.value = ''; control('course-a-bulk-apply').click();
assert.equal(requests.length, before, 'blank chapter numbers do not accidentally become chapter zero');
chapterNumber.value = '7'; chapterNumber.fire('input'); control('course-a-bulk-apply').click(); await flush();
assert.ok(data.courses.find(c => c.id === 'a').videos.every(v => v.chapter_override === 7));
assert.ok(ids['library-courses'].textContent.includes('Assigned to Chapter 7'));
control('course-a-bulk-select').click();
target = control('course-a-bulk-target'); target.value = 'auto'; target.fire('change');
control('course-a-bulk-apply').click(); await flush();
assert.ok(data.courses.find(c => c.id === 'a').videos.every(v => v.chapter_override === null));
const checkbox = control('lesson-a1-select'); checkbox.checked = true; checkbox.fire('change');
const mode = control('course-a-bulk-mode'); mode.value = 'position'; mode.fire('change');
target = control('course-a-bulk-target'); target.value = 'start'; target.fire('change');
control('course-a-bulk-apply').click(); await flush();
assert.equal(requests.at(-1).url, 'api/catalog/courses/a/move');
assert.deepEqual(requests.at(-1).body.ids, ['a1']);
assert.equal(data.courses.find(c => c.id === 'a').videos[0].id, 'a1');
assert.equal(control('lesson-a1-select').checked, false, 'successful moves clear checkboxes');

offline = true; ids['library-refresh'].click(); await flush();
assert.ok(ids['library-message'].textContent.includes('Read-only'));
assert.ok(ids['library-courses'].textContent.includes('4.02 - Static'));
assert.ok(ids['library-courses'].textContent.includes('Model description'));
assert.equal(nodes().filter(n => n.tagName === 'button' && n.textContent === 'Rename').length, 0);
assert.equal(ids['library-reset'].disabled, true);
assert.equal(ids['library-sort-courses'].disabled, true);
assert.equal(control('course-static-sort-apply'), undefined);
assert.ok(ids['library-courses'].textContent.includes('Chapter 4'), 'static exports carry numbering metadata');
console.log('library.js runtime checks passed');
