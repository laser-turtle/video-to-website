// Runtime checks for the generated page script, against a minimal DOM stub.
// These cover the behaviours that only show up in a browser: seeking without
// scrolling, the second-click pause, and clip playback, pausing and scrubbing.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(process.argv[2], 'utf8');

// A minimal scrollable document, so paging through a tall step can be checked.
const page = { y: 0, viewport: 800, height: 6000 };

function makeElement(tag, attrs = {}) {
  const el = {
    tagName: tag,
    dataset: { ...(attrs.dataset || {}) },
    classes: new Set(attrs.classes || []),
    listeners: {},
    children: attrs.children || [],
    parentNode: null,
    style: {},
    textContent: '',
    innerHTML: '',
    paused: true,
    currentTime: 0,
    duration: attrs.duration ?? 4,
    readyState: attrs.readyState ?? 1,
    playCount: 0,
    scrolled: false,
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    fire(type, event = {}) { (this.listeners[type] || []).forEach((fn) => fn({ target: this, ...event })); },
    play() { this.paused = false; this.playCount++; this.fire('play'); return { catch() {} }; },
    pause() { this.paused = true; this.fire('pause'); },
    load() { this.loadCalled = true; },
    scrollIntoView() { this.scrolled = true; },
    setPointerCapture() {}, releasePointerCapture() {},
    docTop: attrs.docTop,
    docHeight: attrs.docHeight,
    get offsetHeight() { return this.docHeight === undefined ? 200 : this.docHeight; },
    getBoundingClientRect() {
      if (this.docTop === undefined) return { left: 0, width: 100, top: 0, height: 18 };
      return {
        left: 0,
        width: 100,
        top: this.docTop - page.y,
        bottom: this.docTop + this.docHeight - page.y,
        height: this.docHeight,
      };
    },
    closest(selector) {
      if (selector.startsWith('[data-')) {
        const key = selector.slice(6, -1).replace(/-([a-z])/g, (m, c) => c.toUpperCase());
        let node = this;
        while (node) {
          if (key in node.dataset) return node;
          node = node.parentNode;
        }
        return null;
      }
      let node = this;
      while (node) {
        if (node.classes.has(selector.replace('.', ''))) return node;
        node = node.parentNode;
      }
      return null;
    },
    querySelector(selector) {
      // Enough of a selector engine for what the page script actually asks for:
      // a single class or tag, or a two-part descendant like ".clip-frame video".
      const parts = selector.trim().split(/\s+/);
      const matches = (node, part) => {
        if (part.startsWith('.')) return node.classes.has(part.slice(1));
        if (part.startsWith('[data-')) {
          const key = part.slice(6, -1).replace(/-([a-z])/g, (m, c) => c.toUpperCase());
          return key in node.dataset;
        }
        return node.tagName === part;
      };
      const walk = (node, part) => {
        for (const child of node.children) {
          if (matches(child, part)) return child;
          const deeper = walk(child, part);
          if (deeper) return deeper;
        }
        return null;
      };
      let scope = this;
      for (const part of parts) {
        scope = walk(scope, part);
        if (!scope) return null;
      }
      return scope;
    },
    classList: {
      add: (name) => el.classes.add(name),
      remove: (name) => el.classes.delete(name),
      contains: (name) => el.classes.has(name),
      toggle: (name, on) => (on ? el.classes.add(name) : el.classes.delete(name)),
    },
    get firstElementChild() { return this.children[0] || null; },
  };
  const adopt = (node) => node.children.forEach((child) => { child.parentNode = node; adopt(child); });
  adopt(el);
  return el;
}

const mainVideo = makeElement('video');
const sizeButton = makeElement('button', { classes: ['player-size'] });
const rateReadout = makeElement('span', { classes: ['rate'] });
const playerHead = makeElement('div', {
  classes: ['player-head'],
  children: [makeElement('span', { classes: ['label'] }), makeElement('span', { classes: ['at'] }),
             rateReadout, sizeButton, makeElement('span', { classes: ['chev'] })],
});
const player = makeElement('div', { classes: ['player'], children: [playerHead, mainVideo] });
const backdrop = makeElement('div', { classes: ['player-backdrop'] });

const stepTimestamp = makeElement('button', { classes: ['ts'], dataset: { t: '120.5' } });
const shot = makeElement('img', { dataset: { t: '131.0' } });
const zoomButton = makeElement('button', {
  classes: ['zoom'], dataset: { zoom: 'frames/lesson-one/step-001-1.jpg', at: '131.0' },
});
const zoomImage = makeElement('img');
const zoomAt = makeElement('span', { classes: ['lb-at'] });
const zoomPlay = makeElement('button', { classes: ['lb-play'] });
const lightbox = makeElement('div', {
  classes: ['lightbox'],
  children: [zoomImage, makeElement('div', { classes: ['lightbox-bar'], children: [zoomPlay, zoomAt] })],
});
lightbox.hidden = true;

// The clip lives inside the first step, as it does on a real page.
const clipVideo = makeElement('video', { duration: 4 });
const frame = makeElement('div', { classes: ['clip-frame'], children: [clipVideo] });
const fill = makeElement('i');
const track = makeElement('div', { classes: ['clip-track'], children: [fill] });
const playButton = makeElement('button', { classes: ['clip-play'] });
const loopButton = makeElement('button', { classes: ['clip-loop', 'on'] });
const timeLabel = makeElement('span', { classes: ['clip-time'] });
const controls = makeElement('div', { classes: ['clip-controls'], children: [playButton, loopButton, track, timeLabel] });
const figure = makeElement('figure', { classes: ['clip'], children: [frame, controls] });

const step = makeElement('article', {
  classes: ['step'], dataset: { start: '100', end: '140' },
  children: [stepTimestamp, figure, shot, zoomButton],
  docTop: 0, docHeight: 2400,   // taller than the window, like a step with a clip
});
step.id = 'step-1';
const secondTimestamp = makeElement('button', { classes: ['ts'], dataset: { t: '150' } });
const stepTwo = makeElement('article', {
  classes: ['step'], dataset: { start: '140', end: '180' }, children: [secondTimestamp],
  docTop: 2400, docHeight: 1000,
});
stepTwo.id = 'step-2';
const overlay = makeElement('div', { classes: ['shortcuts'] });
overlay.hidden = true;

const progressMeter = makeElement('div', { classes: ['progress'] });
let observerCallback = null;
const documentListeners = {};
const fakeDocument = {
  body: { dataset: { lesson: 'lesson-one' } },
  documentElement: { style: { setProperty() {} }, scrollHeight: page.height },
  getElementById: (id) =>
    id === 'player' ? player
      : id === 'player-backdrop' ? backdrop
        : id === 'shortcuts' ? overlay
          : id === 'lightbox' ? lightbox
            : null,
  querySelector: (selector) => (selector === '.progress' ? progressMeter : null),
  querySelectorAll: (selector) => {
    if (selector === '.step') return [step, stepTwo];
    if (selector === '.clip-frame video') return [clipVideo];
    if (selector === '.clip-loop') return [loopButton];
    return [];
  },
  addEventListener: (type, fn) => { (documentListeners[type] ||= []).push(fn); },
};
const saved = {};
const storage = { getItem: (k) => (k in saved ? saved[k] : null), setItem: (k, v) => { saved[k] = v; } };
const windowListeners = {};
const fakeWindow = {
  addEventListener(type, fn) { (windowListeners[type] ||= []).push(fn); },
  innerHeight: page.viewport,
  get scrollY() { return page.y; },
  // Both call shapes: scrollTo({top}) and scrollTo(x, y).
  scrollTo(first, second) {
    page.y = first && typeof first === 'object' ? first.top : second;
  },
  // Run the animation to completion in one frame so assertions see the result.
  requestAnimationFrame(fn) { fn(1e12); return 1; },
  cancelAnimationFrame() {},
};
// Scrolling by hand, which the script learns about from a wheel event.
const scrollByHand = (y) => {
  page.y = y;
  (windowListeners.wheel || []).forEach((fn) => fn({}));
};
class FakeObserver {
  constructor(callback) { observerCallback = callback; }
  observe() {}
}
fakeWindow.IntersectionObserver = FakeObserver;

new Function('document', 'window', 'IntersectionObserver', 'history', 'location', 'localStorage', source)(
  fakeDocument, fakeWindow, FakeObserver,
  { replaceState() {} }, { hash: '' },
  storage,
);

const click = (target) => documentListeners.click.forEach((fn) => fn({ target, preventDefault() {} }));
const collapsedAtStart = player.classes.has('collapsed');

// 1. A timestamp seeks the player and leaves the page where it was.
click(stepTimestamp);
assert.equal(mainVideo.currentTime, 120.5, 'timestamp should seek the player');
assert.ok(mainVideo.playCount > 0, 'timestamp should start playback');
assert.equal(mainVideo.scrolled, false, 'timestamp must not scroll the page');

// 2. Clicking the same thing again pauses; a third click resumes.
click(stepTimestamp);
assert.equal(mainVideo.paused, true, 'a second click on the same target pauses');
click(stepTimestamp);
assert.equal(mainVideo.paused, false, 'a third click resumes');

// 3. A different target seeks rather than pausing.
click(shot);
assert.equal(mainVideo.currentTime, 131, 'a different target seeks to its own time');
assert.equal(mainVideo.paused, false, 'seeking a new target keeps playing');
click(shot);
assert.equal(mainVideo.paused, true, 'the new target now owns the toggle');

// 4. A clip runs while its step is the current one and is on screen.
observerCallback([{ target: clipVideo, isIntersecting: true }]);
assert.ok(step.classes.has('current'), 'its step is the one being worked on');
assert.equal(clipVideo.paused, false, 'so the clip plays');
assert.ok(!clipVideo.loadCalled, 'load() would abort the play request');
observerCallback([{ target: clipVideo, isIntersecting: false }]);
assert.equal(clipVideo.paused, true, 'and stops once off screen');

// 4b. Being on screen is not enough: the step has to be the current one.
observerCallback([{ target: clipVideo, isIntersecting: true }]);
assert.equal(clipVideo.paused, false, 'playing again');
clipVideo.currentTime = 2;
click(secondTimestamp);
assert.ok(stepTwo.classes.has('current'), 'moving to another step');
assert.equal(clipVideo.paused, true, 'stops the clip though it is still on screen');
assert.equal(clipVideo.currentTime, 0, 'and rewinds it, so returning starts from the top');
click(stepTimestamp);
assert.equal(clipVideo.paused, false, 'coming back starts it again');

// 5. The clip's own controls pause it, and it stays paused when scrolled back.
playButton.fire('click');
assert.equal(clipVideo.paused, true, 'the pause button stops the clip');
assert.equal(playButton.textContent, 'play', 'the button shows the next action');
observerCallback([{ target: clipVideo, isIntersecting: true }]);
assert.equal(clipVideo.paused, true, 'a clip paused by hand stays paused');
playButton.fire('click');
assert.equal(clipVideo.paused, false, 'pressing play resumes');
assert.equal(playButton.textContent, 'pause', 'the button label follows state');

// 6. The scrubber seeks within the clip and holds the frame still.
track.fire('pointerdown', { clientX: 25, preventDefault() {}, pointerId: 1 });
assert.equal(clipVideo.currentTime, 1, 'a quarter along a 4s clip is 1s');
assert.equal(clipVideo.paused, true, 'scrubbing pauses so the frame can be read');
track.fire('pointermove', { clientX: 75, pointerId: 1 });
assert.equal(clipVideo.currentTime, 3, 'dragging scrubs');
assert.equal(fill.style.width, '75%', 'the bar tracks the drag');
assert.equal(timeLabel.textContent, '3.0 / 4.0s', 'the readout follows the scrub');

// 7. The player starts collapsed, opens on a jump, and its header toggles it.
assert.ok(collapsedAtStart, 'the player should start out of the way');
assert.ok(!player.classes.has('collapsed'), 'a jump opens a collapsed player');
playerHead.fire('click');
assert.ok(player.classes.has('collapsed'), 'the header collapses the player');
playerHead.fire('click');
assert.ok(!player.classes.has('collapsed'), 'and opens it again');

// 8. Focus mode: a bigger centred player, quick to toggle and quick to leave.
const press = (key, target = {}, extra = {}) =>
  documentListeners.keydown.forEach((fn) => fn({ key, target, preventDefault() {}, ...extra }));

press('f');
assert.ok(player.classes.has('focus'), 'f should open the larger view');
assert.ok(backdrop.classes.has('on'), 'the backdrop comes with it');
press('f');
assert.ok(!player.classes.has('focus'), 'f again returns to the corner');
assert.ok(!backdrop.classes.has('on'), 'and takes the backdrop with it');

press('f');
press('Escape');
assert.ok(!player.classes.has('focus'), 'Escape leaves the larger view');

press('f');
backdrop.fire('click');
assert.ok(!player.classes.has('focus'), 'clicking the backdrop leaves it too');

let stopped = false;
sizeButton.fire('click', { stopPropagation() { stopped = true; } });
assert.ok(player.classes.has('focus'), 'the size button opens the larger view');
assert.ok(stopped, 'the click must not reach the header, which collapses');
sizeButton.fire('click', { stopPropagation() {} });
assert.ok(!player.classes.has('focus'), 'and closes it again');

// Typing somewhere should never trigger the shortcut.
press('f', { tagName: 'INPUT' });
assert.ok(!player.classes.has('focus'), 'f in a text field is just an f');

// Opening the larger view from collapsed should also uncollapse it.
playerHead.fire('click');
assert.ok(player.classes.has('collapsed'), 'collapsed first');
press('f');
assert.ok(!player.classes.has('collapsed'), 'focus implies open');
assert.ok(player.classes.has('focus'), 'and focused');

// Visibility has its own shortcut, preserving playback and clearing the
// backdrop if the player is collapsed from the larger view.
const playbackBeforeHide = [mainVideo.paused, mainVideo.currentTime];
press('v');
assert.ok(player.classes.has('collapsed'), 'v collapses the player');
assert.ok(!player.classes.has('focus') && !backdrop.classes.has('on'), 'no backdrop remains over the lesson');
assert.deepEqual([mainVideo.paused, mainVideo.currentTime], playbackBeforeHide);
assert.equal(saved['v2w:' + fakeDocument.body.dataset.lesson + ':player'], '1');
press('v', { tagName: 'INPUT' });
assert.ok(player.classes.has('collapsed'), 'typing does not change player visibility');
press('V');
assert.ok(!player.classes.has('collapsed') && !player.classes.has('focus'), 'v reopens the floating size');
press('v', {}, { repeat: true });
press('v', {}, { ctrlKey: true });
assert.ok(!player.classes.has('collapsed'), 'holding v or using a browser shortcut does not toggle it');
press('v');
assert.ok(player.classes.has('collapsed'), 'v also collapses from the floating size');
press('v');
assert.equal(saved['v2w:' + fakeDocument.body.dataset.lesson + ':player'], '0');
press('f');

// 9. There is a cursor over the steps, and it starts at the first one.
// How j and k move it through tall steps is covered in 15 to 17.
assert.ok(step.classes.has('current'), 'the first step starts as the cursor');

// 10. Marking a step done and moving on is one key, whatever the step's height.
press('Enter', { tagName: 'A' });
press('Enter', { tagName: 'BUTTON' });
press('Enter', { tagName: 'SUMMARY' });
press('x', { tagName: 'A', closest: selector => selector === '.course-contents' });
assert.ok(!step.classes.has('done'), 'Enter on navigation links and buttons keeps their native action');
press('Enter');
assert.ok(step.classes.has('done'), 'Enter marks the step done');
assert.ok(stepTwo.classes.has('current'), 'and advances to the next');
assert.equal(progressMeter.textContent, '1 of 2 steps done', 'progress follows');
press('x');
assert.ok(stepTwo.classes.has('done'), 'x marks the cursor step done');
press('x');
assert.ok(!stepTwo.classes.has('done'), 'and x again undoes it');

// 11. g plays the video from the current step.
scrollByHand(0);
press('k'); press('k'); press('k');
click(shot);   // move the toggle target off the step's own timestamp button
press('g');
assert.equal(mainVideo.currentTime, 100, "g plays from the cursor step's start");

// 12. Space and the arrows drive playback without touching the player.
mainVideo.paused = false;
press(' ');
assert.equal(mainVideo.paused, true, 'space pauses');
press(' ');
assert.equal(mainVideo.paused, false, 'space plays');
mainVideo.currentTime = 50;
press('ArrowRight');
assert.equal(mainVideo.currentTime, 60, 'right goes forward ten seconds');
press('ArrowLeft');
assert.equal(mainVideo.currentTime, 50, 'left goes back ten seconds');
mainVideo.currentTime = 2;
press('ArrowLeft');
assert.equal(mainVideo.currentTime, 0, 'and never goes below zero');

// 13. c drives the clip belonging to the step under the cursor.
step.children.push(frame);
frame.parentNode = step;
scrollByHand(0);
press('k'); press('k'); press('k');
const before = clipVideo.paused;
press('c');
assert.notEqual(clipVideo.paused, before, "c toggles the cursor step's clip");

// 14. The shortcut list is reachable and dismissable.
assert.equal(overlay.hidden, true, 'the list starts hidden');
press('?');
assert.equal(overlay.hidden, false, '? opens it');
press('Escape');
assert.equal(overlay.hidden, true, 'Escape closes it');

// 15. A tall step is paged through before navigation moves on, and the top of
// a step is what gets aligned, since that is where its text is.
scrollByHand(0);
press('k'); press('k');
assert.ok(step.classes.has('current'), 'back at the first step');
assert.equal(page.y, 0, 'and at the top of the page');

press('j');
assert.equal(page.y, 680, 'j scrolls one window (less overlap) into the tall step');
assert.ok(step.classes.has('current'), 'without leaving the step');
press('j');
assert.equal(page.y, 1360, 'and again');
press('j');
assert.equal(page.y, 1600, 'the last page stops at the step, not past it');
assert.ok(step.classes.has('current'), 'still the same step');

press('j');
assert.ok(stepTwo.classes.has('current'), 'only at the end does it move on');
assert.equal(page.y, 2384, "and it aligns the next step's top, not its middle");

// 16. Going back is the exact inverse: you land where you left.
press('k');
assert.ok(step.classes.has('current'), 'k returns to the previous step');
assert.equal(page.y, 1600, 'at the page you were on when you left it');
press('k');
assert.equal(page.y, 920, 'then pages back up through it');

// 17. Scrolling by hand is respected: navigation resumes from what is on screen.
scrollByHand(2384);
press('j');
assert.ok(stepTwo.classes.has('current'), 'j moves on from where they actually are');
press('j');
assert.equal(page.y, 2600, 'paging to the last page of the final step');
press('j');
assert.equal(page.y, 2600, 'and there is nothing past the end to go to');

// 18. Home and End reach the ends of the lesson.
scrollByHand(1500);
press('Home');
assert.equal(page.y, 0, 'Home goes to the very top, where the summary is');
assert.ok(step.classes.has('current'), 'and the cursor follows');
press('End');
assert.ok(stepTwo.classes.has('current'), 'End goes to the last step');
assert.equal(page.y, 2600, 'landing on its last page');

// 19. Playback speed steps through a ladder and is remembered globally.
assert.equal(mainVideo.playbackRate, 1, 'starts at normal speed');
assert.equal(clipVideo.playbackRate, 1, 'clips start at normal speed too');
press('.');
assert.equal(mainVideo.playbackRate, 1.25, '. speeds up');
assert.equal(clipVideo.playbackRate, 1.25, 'and clips follow the same speed');
press('.');
assert.equal(mainVideo.playbackRate, 1.5, 'and again');
assert.equal(rateReadout.textContent, '1.5x', 'the player shows the speed');
assert.equal(saved['v2w:rate'], '1.5', 'stored under a key with no lesson in it');
press(',');
assert.equal(mainVideo.playbackRate, 1.25, ', slows down');
for (let i = 0; i < 10; i++) press(',');
assert.equal(mainVideo.playbackRate, 0.75, 'and stops at the slowest rung');
for (let i = 0; i < 20; i++) press('.');
assert.equal(mainVideo.playbackRate, 3, 'and at the fastest');



// 20. Enlarging a screenshot is its own action; the picture still seeks.
scrollByHand(0);
press('k'); press('k');
assert.equal(lightbox.hidden, true, 'no overlay to begin with');
mainVideo.currentTime = 0;
click(zoomButton);
assert.equal(lightbox.hidden, false, 'the zoom button opens the overlay');
assert.equal(zoomImage.src, 'frames/lesson-one/step-001-1.jpg', 'showing that screenshot');
assert.equal(zoomAt.textContent, '2:11', 'labelled with its timestamp');
assert.equal(mainVideo.currentTime, 0, 'and enlarging does not seek the video');

// Closing, and the way back to playback from inside the overlay.
lightbox.fire('click');
assert.equal(lightbox.hidden, true, 'clicking the overlay closes it');
click(zoomButton);
zoomPlay.fire('click', { stopPropagation() {} });
assert.equal(lightbox.hidden, true, 'playing from the overlay closes it');
assert.equal(mainVideo.currentTime, 131, 'and seeks to that screenshot');

// The key does the same for the step under the cursor.
press('z');
assert.equal(lightbox.hidden, false, 'z enlarges the current step shot');
press('z');
assert.equal(lightbox.hidden, true, 'and z again closes it');

// Escape reaches the overlay before anything else it might close.
if (!player.classes.has('focus')) press('f');
assert.ok(player.classes.has('focus'), 'larger video open');
click(zoomButton);
assert.ok(player.classes.has('focus'), 'still open behind the picture');
press('Escape');
assert.equal(lightbox.hidden, true, 'Escape closes the picture first');
assert.ok(player.classes.has('focus'), 'leaving the video as it was');
press('Escape');
assert.ok(!player.classes.has('focus'), 'a second Escape then closes that');

// The screenshot itself keeps seeking, which is the point of the separate button.
click(shot);
assert.equal(mainVideo.currentTime, 131, 'clicking the picture still jumps the video');
assert.equal(lightbox.hidden, true, 'without opening the overlay');

// 20b. Quick seeking sits on the home row, with the arrows still working.
scrollByHand(0);
press('k'); press('k');
mainVideo.currentTime = 100;
press('h');
assert.equal(mainVideo.currentTime, 90, 'h goes back ten seconds');
press('l');
assert.equal(mainVideo.currentTime, 100, 'l goes forward ten seconds');
press('ArrowLeft');
assert.equal(mainVideo.currentTime, 90, 'the arrows do the same');
press('ArrowRight');
assert.equal(mainVideo.currentTime, 100, 'in both directions');
mainVideo.currentTime = 4;
press('h');
assert.equal(mainVideo.currentTime, 0, 'and never seek before the start');

// 20c. A clip nudges in smaller steps, stops at an end, and only comes round
// to the other side when the key is pressed again there.
assert.ok(step.classes.has('current'), 'the step with the clip is current');
clipVideo.currentTime = 3;   // of a 4s clip, nudging by 2
clipVideo.paused = false;

press('[');
assert.equal(clipVideo.currentTime, 1, '[ nudges back two seconds');
assert.equal(clipVideo.paused, false, 'and leaves it running');

press('[');
assert.equal(clipVideo.currentTime, 0, 'going past the start stops at the start');
assert.equal(clipVideo.paused, true, 'paused there, so the first frame can be seen');

press('[');
assert.equal(clipVideo.currentTime, 2, 'pressing again at the start comes round to the end');
assert.equal(clipVideo.paused, false, 'and plays from there');

press(']');
assert.equal(clipVideo.currentTime, 4, 'going past the end stops at the end');
assert.equal(clipVideo.paused, true, 'and holds, since a loop would lose that frame');

press(']');
assert.equal(clipVideo.currentTime, 0, 'pressing again at the end restarts it');
assert.equal(clipVideo.paused, false, 'playing');

// A clip stopped this way stays stopped when its step is re-synced.
press(']');
press(']');
assert.equal(clipVideo.paused, true, 'held at the end');
observerCallback([{ target: clipVideo, isIntersecting: true }]);
assert.equal(clipVideo.paused, true, 'and not restarted behind your back');

// 20d. Looping is a preference with the control as its indicator.
scrollByHand(0);
press('k'); press('k');
assert.equal(clipVideo.loop, true, 'clips loop to begin with');
assert.equal(loopButton.textContent, 'loop', 'and the control says so');
assert.ok(loopButton.classes.has('on'), 'lit up');

press('r');
assert.equal(clipVideo.loop, false, 'r turns looping off');
assert.equal(loopButton.textContent, 'once', 'the control follows');
assert.ok(!loopButton.classes.has('on'), 'and dims');
assert.equal(saved['v2w:loop'], '0', 'remembered across lessons, like the speed');

loopButton.fire('click');
assert.equal(clipVideo.loop, true, 'the control toggles it as well as the key');
assert.equal(saved['v2w:loop'], '1', 'and is remembered either way');

// A clip that has played to its end is finished, not waiting to be restarted.
press('r');
assert.equal(clipVideo.loop, false, 'playing once');
clipVideo.currentTime = 4;
clipVideo.ended = true;
clipVideo.pause();
observerCallback([{ target: clipVideo, isIntersecting: true }]);
assert.equal(clipVideo.paused, true, 'so nothing restarts it behind your back');
clipVideo.ended = false;
press('r');

// 20e. The page follows the video only after a deliberate jump.
scrollByHand(0);
press('Home');
assert.ok(step.classes.has('current'), 'starting on the first step');

click(stepTimestamp);                 // a deliberate jump
mainVideo.currentTime = 150;          // playing on into the next step's range
mainVideo.fire('timeupdate');
assert.ok(stepTwo.classes.has('current'), 'after a jump the page keeps up with the video');

press('Home');
press('j');                           // moving by hand takes it off autopilot
assert.ok(step.classes.has('current'), 'back on the first step');
mainVideo.currentTime = 150;
mainVideo.fire('timeupdate');
assert.ok(step.classes.has('current'), 'and the video no longer drags the cursor about');

// 20f. The scenario that found this: pause a clip, play the video, resume the
// clip. Space must not move the cursor, or c would reach a different step.
press('Home');
observerCallback([{ target: clipVideo, isIntersecting: true }]);
delete clipVideo.dataset.userPaused;
clipVideo.ended = false;
clipVideo.play();
assert.equal(clipVideo.paused, false, 'the clip is running');

press('c');
assert.equal(clipVideo.paused, true, 'c pauses it');

mainVideo.pause();
press(' ');
assert.equal(mainVideo.paused, false, 'space plays the lesson video');
mainVideo.currentTime = 150;          // the playhead is under another step
mainVideo.fire('timeupdate');
assert.ok(step.classes.has('current'), 'and the cursor stays where it was put');

press('c');
assert.equal(clipVideo.paused, false, 'so c resumes the same clip');

// 21. Last of all, because it runs the script a second time: a fresh page
// picks the stored speed back up. Everything it touches keeps its listeners,
// so nothing may rely on the page state after this point.
saved['v2w:rate'] = '1.75';
const second = new Function('document', 'window', 'IntersectionObserver', 'history', 'location', 'localStorage', source);
const isolatedDocument = Object.assign({}, fakeDocument, { addEventListener() {} });
const isolatedWindow = Object.assign({}, fakeWindow, { addEventListener() {} });
mainVideo.playbackRate = 1;
second(isolatedDocument, isolatedWindow, FakeObserver, { replaceState() {} }, { hash: '' }, storage);
assert.equal(mainVideo.playbackRate, 1.75, 'the speed is restored on the next lesson');

console.log('app.js runtime checks passed');
