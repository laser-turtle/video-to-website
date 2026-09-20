// Per-course activity and publication reloads on the All courses page.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source = readFileSync(process.argv[2], 'utf8');
function element(tag, className = '') {
  return {tagName: tag, className, children: [], dataset: {}, attributes: {}, hidden: false, textContent: '',
    appendChild(child) { child.parent = this; this.children.push(child); return child; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); },
    setAttribute(name, value) { this.attributes[name] = value; },
    querySelectorAll(selector) {
      const matches = node => selector === '[data-course-slug]' ? 'courseSlug' in node.dataset : node.className === selector.slice(1);
      return this.children.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
  };
}
const list = element('ul');
function card(id, slug, title) {
  const node = element('li'); node.dataset = {courseId: id, courseSlug: slug, courseTitle: title};
  const main = element('a', 'course-card-main'); main.href = slug + '/index.html'; node.appendChild(main);
  const badge = element('a', 'course-activity'); badge.hidden = true; node.appendChild(badge);
  list.appendChild(node); return node;
}
const first = card('a', 'course-a', 'Same name');
const second = card('b', 'course-b', 'Same name');
const legacy = card('', 'legacy', 'Legacy course');
const activity = card => card.querySelector('.course-activity');
const listeners = {};
const count = element('div'); count.textContent = '3 courses';
const document = {hidden: false, getElementById: id => ({'course-cards': list, 'course-count': count})[id] || null,
  createElement: element, addEventListener: (event, fn) => { listeners[event] = fn; }};
let payload = {built: 0, videos: []};
let ok = true, pendingFetch = null, tick, reloads = 0;
const requests = [];
const fetch = (url, init) => {
  requests.push({url, init});
  if (pendingFetch) return pendingFetch;
  return Promise.resolve({ok, json: async () => structuredClone(payload)});
};
const flush = async () => { for (let i = 0; i < 3; i++) await new Promise(r => setImmediate(r)); };
const poll = async () => { tick(); await flush(); };
const video = (course_id, state, extra = {}) => ({course_id, course_slug: 'course-' + course_id, course: 'Same name', state, title: 'Individual job detail', ...extra});
new Function('document', 'fetch', 'setInterval', 'location', source)(document, fetch, fn => { tick = fn; }, {reload() { reloads++; }});
await flush();
assert.ok(requests[0].url.startsWith('status.json?t='));
assert.equal(activity(first).hidden, true);
assert.equal(list.children.length, 3);

payload.videos = [video('a', 'working'), video('a', 'queued'), video('a', 'queued'), video('b', 'working'),
  video('b', 'working'), video('b', 'failed'), video('b', 'done'), video('a', 'cancelled')];
await poll();
assert.equal(activity(first).textContent, '1 video processing · 2 waiting');
assert.equal(activity(second).textContent, '2 videos processing · 1 failed', 'identical course names remain separate');
assert.equal(activity(first).href, 'queue.html?course=a');
assert.equal(activity(second).href, 'queue.html?course=b&state=all');
assert.equal(activity(legacy).hidden, true);
assert.equal(first.querySelector('.course-card-main').href, 'course-a/index.html', 'reading link remains distinct');
assert.equal(activity(first).children.length, 0, 'only a count, no job rows or action controls');
payload.videos[0].course = 'Renamed course';
await poll();
assert.ok(activity(first).attributes['aria-label'].startsWith('Renamed course:'), 'matching uses stable identity after a rename');

payload.videos = [video('new', 'queued', {course: '<img src=x onerror=alert(1)>'}), video('another', 'queued')];
await poll();
assert.equal(list.children.length, 5, 'courses appear while their first lesson is processing');
assert.equal(count.textContent, '5 courses');
const pendingCard = list.children[3];
assert.equal(pendingCard.querySelector('.t').textContent, '<img src=x onerror=alert(1)>');
assert.equal(pendingCard.querySelector('.t').children.length, 0, 'course names are text');
assert.equal(activity(pendingCard).textContent, '1 waiting');
assert.equal(pendingCard.querySelector('.course-card-main').href, 'queue.html?course=new');
await poll(); assert.equal(list.children[3], pendingCard, 'polling keeps existing card/link nodes');
payload.videos = [video('new', 'failed')]; await poll();
assert.equal(list.children.length, 4);
assert.equal(activity(pendingCard).href, 'queue.html?course=new&state=failed');
payload.videos = [video('new', 'cancelled')]; await poll();
assert.equal(list.children.length, 3, 'inactive placeholder cards are removed');
assert.equal(count.textContent, '3 courses');
assert.equal(activity(first).hidden, true);

payload.videos = [{course: 'Legacy course', state: 'working'}, {course: 'A & B', state: 'queued'}]; await poll();
assert.equal(activity(legacy).textContent, '1 video processing');
assert.equal(activity(legacy).href, 'queue.html?q=Legacy+course');
assert.equal(activity(list.children[3]).href, 'queue.html?q=A+%26+B');
ok = false; await poll();
assert.equal(list.children.length, 3); assert.equal(activity(legacy).hidden, true, 'unavailable status does not leave stale counts');
ok = true; payload = {unexpected: true}; await poll(); assert.equal(list.children.length, 3);
payload = {built: 0, videos: [video('a', 'working')]}; await poll();
assert.equal(activity(first).hidden, false);
let before = requests.length;
document.hidden = true; await poll(); assert.equal(requests.length, before, 'background tabs do not poll');
document.hidden = false; listeners.visibilitychange(); await flush(); assert.equal(requests.length, before + 1);

let resolveFetch;
pendingFetch = new Promise(resolve => { resolveFetch = resolve; });
tick(); before = requests.length; tick(); assert.equal(requests.length, before, 'polls cannot overlap');
resolveFetch({ok: true, json: async () => payload}); await flush(); pendingFetch = null;
payload = {built: 25, videos: []}; await poll(); assert.equal(reloads, 1, 'publication still refreshes course cards');
await poll(); assert.equal(reloads, 1, 'a publication triggers one reload');
assert.ok(requests.every(request => !request.init.method), 'the home page has no task mutation actions');
console.log('status.js runtime checks passed');
