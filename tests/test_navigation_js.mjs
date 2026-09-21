import assert from 'node:assert/strict';
import fs from 'node:fs';
const source = fs.readFileSync(process.argv[2], 'utf8');

function fixture(current = 'five-b') {
  let document;
  class Element {
    constructor(tag, attrs = {}) {
      this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {};
      this.listeners = {}; this.hidden = false; this.open = false; this.value = ''; this.textContent = '';
      this.className = ''; this.isConnected = true; this.scrollTop = 0;
      Object.assign(this, attrs);
    }
    appendChild(child) { child.parent = this; this.children.push(child); return child; }
    replaceChildren() { this.children = []; }
    setAttribute(key, value) { this.attributes[key] = value; }
    getAttribute(key) { return key === 'href' ? this.href : this.attributes[key]; }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(event, fn) { (this.listeners[event] ||= []).push(fn); }
    fire(event, args = {}) { const e = {target: this, preventDefault() { this.prevented = true; }, ...args}; (this.listeners[event] || []).forEach(fn => fn(e)); return e; }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; this.fire('close'); }
    getBoundingClientRect() { return {top: 0, bottom: 100, left: 0, right: 600}; }
    querySelectorAll(selector) {
      const key = selector.startsWith('[data-') ? selector.slice(6, -1).replace(/-([a-z])/g, (_, c) => c.toUpperCase()) : null;
      const match = n => key ? key in n.dataset : selector.startsWith('.') ? n.className === selector.slice(1) : n.tagName === selector.toUpperCase();
      return this.children.flatMap(child => [...(match(child) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  }
  const root = new Element('nav');
  const ids = {};
  for (const id of ['lesson-picker', 'lesson-picker-search', 'lesson-picker-results', 'lesson-picker-count', 'lesson-picker-open', 'lesson-picker-close', 'lesson-navigation-status', 'shortcuts', 'lightbox']) ids[id] = new Element('div');
  ids['lesson-picker-search'].tagName = 'INPUT'; ids['shortcuts'].hidden = ids['lightbox'].hidden = true;
  const rows = [
    ['welcome', '', 'Welcome', 'Setup'],
    ['four-a', '4', '4.01 - Reference', 'Prepare the reference'],
    ['four-b', '4', '4.02 - Base form', 'Shape a rounded surface'],
    ['five-a', '5', '5.01 - Materials', 'Paint the model'],
    ['five-b', '5', '5.02 - Café lighting', 'Set up lamps'],
    ['six', '6', '6.01 - Export <test>', 'Save the finished model'],
  ].map(([id, chapter, title, description], index) => {
    const row = new Element('li', {dataset: {courseLesson: id, chapter, title, search: title + ' ' + description + ' Chapter ' + chapter + ' original-' + id + '.mp4', readingPosition: String(index + 1), current: String(id === current)}});
    row.appendChild(new Element('a', {href: id + '.html'}));
    row.appendChild(new Element('p', {className: 'lesson-description', textContent: description}));
    return row;
  });
  rows.toReversed().forEach(row => root.appendChild(row));
  const listeners = {};
  document = {activeElement: new Element('button'), getElementById: id => ids[id], querySelector: () => root,
    createElement: tag => new Element(tag), addEventListener: (key, fn) => { (listeners[key] ||= []).push(fn); }};
  const navigated = [];
  new Function('document', 'location', source)(document, {assign: href => navigated.push(href)});
  function press(key, extra = {}) { const event = {key, target: document.activeElement, preventDefault() { this.prevented = true; }, ...extra}; listeners.keydown.forEach(fn => fn(event)); return event; }
  function query(text) { ids['lesson-picker-search'].value = text; ids['lesson-picker-search'].fire('input'); }
  return {ids, document, navigated, press, query};
}
const f = fixture();
const {ids, document, navigated, press, query} = f;
press('J', {shiftKey: true}); press('K', {shiftKey: true}); press('L', {shiftKey: true}); press('H', {shiftKey: true});
assert.deepEqual(navigated, ['six.html', 'five-a.html', 'six.html', 'four-a.html'], 'shortcuts follow grouped reading order, not current DOM/view sort');
for (const extra of [{target: {tagName: 'INPUT'}}, {target: {isContentEditable: true}}, {repeat: true}, {ctrlKey: true}, {metaKey: true}, {altKey: true}, {isComposing: true}]) press('J', {shiftKey: true, ...extra});
press('J'); assert.equal(navigated.length, 4, 'typing, repeats, modifiers and Caps Lock do not navigate');
ids.shortcuts.hidden = false; press('/'); assert.equal(ids['lesson-picker'].open, false);
ids.shortcuts.hidden = true;
const previousFocus = document.activeElement;
assert.equal(press('/').prevented, true);
assert.equal(ids['lesson-picker'].open, true);
assert.equal(document.activeElement, ids['lesson-picker-search']);
assert.equal(ids['lesson-picker-results'].children.length, 6);
assert.equal(ids['lesson-picker-search'].attributes['aria-activedescendant'], 'lesson-choice-4', 'picker starts on current lesson');
press('Enter'); assert.equal(ids['lesson-picker'].open, false, 'choosing current lesson returns to the same reading position');
assert.equal(navigated.length, 4); assert.equal(document.activeElement, previousFocus);
document.activeElement = ids['lesson-picker-search'];
press('/'); assert.equal(ids['lesson-picker'].open, true, 'fast reopening works before native focus restoration finishes');
press('Escape'); assert.equal(document.activeElement, previousFocus);
ids['lesson-picker-open'].fire('click'); query('bse frm');
ids['lesson-picker'].fire('close');
assert.equal(document.activeElement, ids['lesson-picker-search'], 'a deferred close from an earlier opening cannot steal focus');
assert.equal(ids['lesson-picker-results'].children[0].href, 'four-b.html', 'ordered letters find partial names');
press('Enter'); assert.equal(navigated.at(-1), 'four-b.html');
query('cafe'); assert.equal(ids['lesson-picker-results'].children[0].href, 'five-b.html', 'accent-insensitive matching');
query('rounded surface'); assert.equal(ids['lesson-picker-results'].children[0].href, 'four-b.html', 'descriptions are searchable');
query('original-five-a.mp4'); assert.equal(ids['lesson-picker-results'].children[0].href, 'five-a.html');
query('chapter 4'); assert.equal(ids['lesson-picker-results'].children.length, 2);
press('ArrowDown'); assert.equal(ids['lesson-picker-search'].attributes['aria-activedescendant'], 'lesson-choice-1');
press('ArrowDown'); assert.equal(ids['lesson-picker-search'].attributes['aria-activedescendant'], 'lesson-choice-1');
press('ArrowUp'); press('Enter'); assert.equal(navigated.at(-1), 'four-a.html');
press('Enter', {target: {tagName: 'DIV'}}); assert.equal(navigated.at(-1), 'four-a.html', 'Enter also works when focus is on the dialog container');
query('unmatchable-query'); assert.equal(ids['lesson-picker-results'].children.length, 0);
assert.equal(ids['lesson-picker-search'].attributes['aria-activedescendant'], undefined);
const before = navigated.length; press('Enter'); assert.equal(navigated.length, before);
query('<test>'); assert.equal(ids['lesson-picker-results'].children[0].children[0].textContent, '6.01 - Export <test>');
assert.equal(ids['lesson-picker-results'].children[0].children[0].children.length, 0, 'titles are inserted as text');
press('Escape'); assert.equal(ids['lesson-picker'].open, false); assert.equal(document.activeElement, previousFocus);
const start = fixture('welcome'); start.press('K', {shiftKey: true}); start.press('H', {shiftKey: true});
assert.equal(start.navigated.length, 0);
const end = fixture('six'); end.press('J', {shiftKey: true}); end.press('L', {shiftKey: true});
assert.equal(end.navigated.length, 0); assert.ok(end.ids['lesson-navigation-status'].textContent.includes('last chapter'));
const index = fixture(null); index.press('J', {shiftKey: true}); index.press('/');
assert.equal(index.navigated.length, 0); assert.equal(index.ids['lesson-picker'].open, true);
console.log('navigation.js runtime checks passed');
