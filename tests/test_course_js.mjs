import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(process.argv[2], 'utf8');
const reading = ['reader-state.js', 'reading.js'].map(name => fs.readFileSync('src/video_to_website/assets/' + name, 'utf8')).join('\n');
const flush = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor(tag, dataset = {}) {
    this.tagName = tag; this.dataset = dataset; this.children = []; this.listeners = {};
    this.hidden = false; this.value = ''; this.textContent = ''; this.className = ''; this._open = false;
  }
  get isConnected() { return this.connected || Boolean(this.parent?.isConnected); }
  set open(value) {
    if (this._open === value) return;
    this._open = value;
    if (!this.pendingToggle) {
      this.pendingToggle = true;
      queueMicrotask(() => { this.pendingToggle = false; this.fire('toggle'); });
    }
  }
  get open() { return this._open; }
  append(...nodes) {
    for (const node of nodes) {
      if (node.parent) node.parent.children = node.parent.children.filter(item => item !== node);
      node.parent = this; this.children.push(node);
    }
  }
  prepend(node) { node.parent = this; this.children.unshift(node); }
  setAttribute(name, value) { this[name] = value; }
  replaceChildren(...nodes) { this.children.forEach(node => { node.parent = null; }); this.children = []; this.append(...nodes); }
  addEventListener(event, callback) { (this.listeners[event] ||= []).push(callback); }
  fire(event) { (this.listeners[event] || []).forEach(callback => callback({target: this})); }
  click() { this.fire('click'); }
  focus() { this.focused = true; }
  closest() { return null; }
  querySelectorAll(selector) {
    const key = selector.startsWith('[data-') ? selector.slice(6, -1).replace(/-([a-z])/g, (_, c) => c.toUpperCase()) : null;
    const matches = node => key ? Object.hasOwn(node.dataset, key) : node.className.split(' ').includes(selector.slice(1));
    return this.children.flatMap(node => [...(matches(node) ? [node] : []), ...node.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function fixture({course = 'stable-course', reader = false, current = '', items} = {}) {
  const root = new Element('nav', {course, context: reader ? 'reader' : 'course'}); root.connected = true;
  function node(tag, selector, data = {}) {
    const el = new Element(tag, data);
    if (selector.startsWith('.')) el.className = selector.slice(1);
    root.append(el); return el;
  }
  const search = node('input', '', {courseSearch: ''});
  const sort = node('select', '', {courseSort: ''});
  const view = node('select', '', {courseView: ''});
  const density = node('select', '', {courseDensity: ''});
  const progressFilter = node('select', '', {courseProgress: ''});
  const hideCompleted = node('input', '', {courseHideCompleted: ''});
  const manage = reader ? null : node('button', '', {manageProgress: ''});
  const bulk = reader ? null : node('section', '', {readingBulk: ''});
  if (bulk) {
    bulk.hidden = true;
    for (const name of ['Selected', 'SelectAll', 'Deselect', 'Complete', 'Reset', 'Undo', 'Message']) {
      bulk.append(new Element('button', {['reading' + name]: ''}));
    }
  }
  const clear = node('button', '', {courseClear: ''});
  const expand = node('button', '', {courseExpand: ''});
  const collapse = node('button', '', {courseCollapse: ''});
  const controls = node('div', '', {courseControls: ''}); controls.hidden = true;
  const count = node('span', '.course-results');
  const empty = node('p', '.course-empty');
  const container = node('div', '.course-groups');
  const rows = (items || [
    ['intro', '', '', 'Welcome', 20, 'Setup instructions'],
    ['two', '4', '2', '4.02 - Shape', 120, 'Make a rounded object'],
    ['ten', '4', '10', '4-10 - Export', 60, 'Export settings'],
    ['end', '10', '1', '10.01 - End', 90, 'Final exercise'],
    ['one', '4', '1', 'Renamed introduction', 30, '4.01 - Original.mp4'],
  ]).map(([id, chapter, number, title, duration, description], index) => {
    const row = new Element('li', {courseLesson: id, chapter, lessonNumber: number, title,
      duration: String(duration), position: String(index + 1), current: String(id === current),
      search: `${title} ${description} ${chapter === '' ? 'Other lessons' : 'Chapter ' + chapter}`});
    row.dataset.reading = JSON.stringify({key: 'v2w:' + course + ':' + id, steps: id === 'intro' ? [] : ['step-1', 'step-2']});
    row.append(new Element('span', {lessonProgress: ''}));
    container.append(row); return row;
  });
  return {root, search, sort, view, density, progressFilter, hideCompleted, manage, bulk, clear, expand, collapse, controls, count, empty, container, rows};
}
const state = new Map();
const storage = {getItem: key => state.get(key) || null, setItem: (key, value) => state.set(key, value)};
function run(f, localStorage = storage) {
  const document = {body: {dataset: {}}, addEventListener() {},
    querySelectorAll: selector => selector === '.course-browser' ? [f.root] : [], createElement: tag => new Element(tag)};
  f.state = new Function('document', 'localStorage', 'window', reading + '\n' + source + '\nreturn V2WReaderState;')(document, localStorage, {addEventListener() {}});
}
const groups = f => f.container.querySelectorAll('.course-chapter');
const order = f => f.container.querySelectorAll('[data-course-lesson]').map(row => row.dataset.courseLesson);
const visible = f => f.rows.filter(row => !row.hidden).map(row => row.dataset.courseLesson);
const change = (el, value) => { el.value = value; el.fire('change'); };
const search = (f, value) => { f.search.value = value; f.search.fire('input'); };
const key = 'v2w:course:stable-course:navigation';

const f = fixture(); run(f); await flush();
assert.deepEqual(order(f), ['intro', 'two', 'ten', 'one', 'end'], 'scattered chapter lessons are collected');
assert.deepEqual(groups(f).map(g => g.dataset.chapterKey), ['other:1', '4:1', '10:1']);
assert.equal(groups(f)[1].querySelector('.chapter-meta').textContent, '3 lessons · 3m 30s');
assert.deepEqual(groups(f).map(g => g.open), [true, false, false]);
assert.equal(f.controls.hidden, false);
assert.equal(f.root.dataset.density, 'detailed');

groups(f)[1].open = true; await flush();
assert.equal(JSON.parse(state.get(key)).opened['4:1'], true, 'native disclosure saves');
search(f, 'FINAL exercise'); await flush();
assert.deepEqual(visible(f), ['end']);
assert.equal(groups(f)[2].open, true, 'search reveals matches inside collapsed chapters');
assert.equal(groups(f)[1].hidden, true);
assert.equal(f.count.textContent, '1 of 5 lessons');
assert.equal(JSON.parse(state.get(key)).opened['10:1'], undefined, 'search opening is temporary');
f.clear.click(); await flush();
assert.deepEqual(groups(f).map(g => g.open), [true, true, false], 'clear restores disclosure choices');
assert.equal(f.search.focused, true);
search(f, 'original.mp4'); assert.deepEqual(visible(f), ['one'], 'renamed source filenames are searchable');
search(f, 'Chapter 4'); assert.deepEqual(visible(f), ['two', 'ten', 'one']);
search(f, '<script>'); assert.equal(f.empty.hidden, false); assert.equal(f.count.textContent, '0 of 5 lessons');
f.clear.click(); await flush();
f.collapse.click(); await flush(); assert.ok(groups(f).every(g => !g.open));
search(f, 'export'); await flush();
assert.equal(groups(f)[1].open, true);
f.collapse.click(); await flush();
f.clear.click(); await flush(); assert.ok(groups(f).every(g => !g.open));
f.expand.click(); await flush(); assert.ok(groups(f).every(g => g.open));

change(f.sort, 'number'); await flush();
assert.deepEqual(order(f), ['one', 'two', 'ten', 'end', 'intro'], 'mixed separators and renamed lessons sort numerically');
assert.deepEqual(f.rows.map(row => row.dataset.position), ['1', '2', '3', '4', '5'], 'view sort never changes saved positions');
assert.equal(groups(f).length, 3);
change(f.view, 'flat'); await flush(); assert.equal(groups(f).length, 0);
assert.equal(f.expand.hidden, true); assert.equal(f.collapse.hidden, true);
change(f.sort, 'shortest'); assert.deepEqual(order(f), ['intro', 'one', 'ten', 'end', 'two']);
change(f.sort, 'title'); assert.deepEqual(order(f), ['ten', 'two', 'end', 'one', 'intro']);
change(f.sort, 'saved'); assert.deepEqual(order(f), ['intro', 'two', 'ten', 'end', 'one']);
change(f.density, 'compact'); assert.equal(f.root.dataset.density, 'compact');
const restored = fixture(); run(restored); await flush();
assert.equal(restored.view.value, 'flat'); assert.equal(restored.density.value, 'compact');
assert.equal(restored.search.value, '', 'search does not carry across lesson navigation');
const other = fixture({course: 'other'}); run(other); await flush();
assert.equal(other.view.value, 'chapters'); assert.equal(other.density.value, 'detailed');

change(f.view, 'chapters'); f.collapse.click(); await flush();
const reader = fixture({reader: true, current: 'end'}); run(reader); await flush();
assert.equal(groups(reader)[2].open, true, 'reader reveals current chapter even if collapsed on course index');
assert.equal(reader.density.value, 'compact');
change(reader.density, 'detailed');
const afterReader = fixture(); run(afterReader);
assert.equal(afterReader.density.value, 'compact', 'reader density is independent of index density');

for (const corrupt of ['null', '[]', '"hello"', '{broken', '{"sort":"bogus","density":42,"opened":[]}']) {
  state.set(key, corrupt);
  const fresh = fixture(); run(fresh); await flush();
  assert.equal(fresh.sort.value, 'saved'); assert.equal(fresh.density.value, 'detailed');
  assert.deepEqual(order(fresh), ['intro', 'two', 'ten', 'one', 'end']);
}
const blocked = fixture();
run(blocked, {getItem() { throw Error('blocked'); }, setItem() { throw Error('full'); }});
change(blocked.sort, 'number'); search(blocked, 'rounded');
assert.deepEqual(visible(blocked), ['two'], 'storage failure does not break navigation');
const plain = fixture({items: [['intro', '', '', 'Welcome', 20, 'Introduction']]}); run(plain);
assert.equal(plain.view.disabled, true); assert.equal(plain.view.value, 'flat');
assert.equal(plain.count.textContent, '1 lesson');
const empty = fixture({items: []}); run(empty); assert.equal(empty.empty.hidden, false);

state.clear();
state.set('v2w:stable-course:two', JSON.stringify({'step-1': true}));
const tracked = fixture(); run(tracked); await flush();
assert.equal(tracked.rows[1].querySelector('[data-lesson-progress]').textContent, '1 of 2 steps · 50%');
assert.equal(groups(tracked)[1].querySelector('.chapter-reading').textContent, '0 of 3 lessons complete · 17%');
change(tracked.progressFilter, 'in-progress'); await flush();
assert.deepEqual(visible(tracked), ['two']);
tracked.manage.click();
assert.equal(tracked.bulk.hidden, false);
const action = name => tracked.bulk.querySelector('[data-reading-' + name + ']');
action('select-all').click(); assert.equal(action('selected').textContent, '1 selected');
tracked.clear.click(); await flush();
const chapterBox = groups(tracked)[1].querySelector('.chapter-selection').children[0];
assert.equal(chapterBox.indeterminate, true);
chapterBox.checked = true; chapterBox.fire('change');
assert.equal(action('selected').textContent, '3 selected', 'chapter selection includes scattered lessons');
search(tracked, 'welcome');
assert.equal(action('selected').textContent, '3 selected (3 hidden by filters)');
action('complete').click(); await flush();
assert.equal(action('message').textContent, 'Completed 3 lessons.');
assert.ok(['two', 'ten', 'one'].every(id => JSON.parse(state.get('v2w:stable-course:' + id))['step-2']));
action('undo').click(); await flush();
assert.equal(JSON.parse(state.get('v2w:stable-course:two'))['step-1'], true, 'undo restores partial progress');
assert.equal(JSON.parse(state.get('v2w:stable-course:two'))['step-2'], false);
tracked.clear.click();
change(tracked.view, 'flat');
action('select-all').click(); action('complete').click(); await flush();
assert.equal(JSON.parse(state.get('v2w:stable-course:intro')).__lessonComplete, true, 'video-only lessons can complete');
change(tracked.progressFilter, 'unfinished'); assert.deepEqual(visible(tracked), []);
tracked.clear.click(); action('select-all').click(); action('reset').click(); await flush();
assert.ok(tracked.rows.every(row => row.dataset.progress === 'not-started'));
change(tracked.view, 'chapters'); await flush();
const beforeFilter = groups(tracked).map(g => g.open);
change(tracked.progressFilter, 'complete'); await flush();
tracked.clear.click(); await flush();
assert.deepEqual(groups(tracked).map(g => g.open), beforeFilter, 'progress filters do not overwrite chapter disclosure preferences');
console.log('course.js runtime checks passed');
