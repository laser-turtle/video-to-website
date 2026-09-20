"""Static site generation: one page per lesson, plus course and root indexes."""

from __future__ import annotations

import hashlib
import html
import json
import shutil
from pathlib import Path

from .util import hms, human_duration, log

STYLE = """\
:root {
  color-scheme: light dark;
  --bg: #fbfbfa;
  --panel: #ffffff;
  --ink: #1b1b1a;
  --muted: #6b6b68;
  --line: #e3e2de;
  --accent: #b4530a;
  --accent-soft: #fdf1e6;
  --radius: 10px;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --player-h: 0px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17171a;
    --panel: #1f1f23;
    --ink: #e9e8e4;
    --muted: #a0a09b;
    --line: #32323a;
    --accent: #ff9e4a;
    --accent-soft: #2a2119;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  -webkit-text-size-adjust: 100%;
}
a { color: var(--accent); }
header.top {
  border-bottom: 1px solid var(--line);
  background: var(--panel);
  padding: 14px 20px;
}
header.top .crumbs { font-size: 13px; color: var(--muted); }
header.top h1 { margin: 6px 0 2px; font-size: 21px; line-height: 1.25; }
header.top .meta { font-size: 13px; color: var(--muted); }

/* The steps are what you read, so they get the full column. The video is
   reference material and lives in a small player you can collapse. */
.wrap { max-width: 980px; margin: 0 auto; padding: 20px 20px calc(24px + var(--player-h)); }

.summary {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 16px 18px;
  margin-bottom: 22px;
}
.summary p { margin: 0 0 8px; }
.summary h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 14px 0 6px; }
.summary ul { margin: 0; padding-left: 20px; }
.progress { font-size: 13px; color: var(--muted); margin-bottom: 14px; }
.step {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 16px 18px;
  margin-bottom: 14px;
  scroll-margin-top: 16px;
}
.step.current { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.step.current > .step-head .n { background: var(--accent); color: #fff; border-color: var(--accent); }
.step.done { opacity: .58; }
.step-head { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.step-head .n {
  font: 600 12px/1 var(--mono);
  color: var(--muted);
  background: var(--bg);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 5px 9px;
}
.step-head h3 { margin: 0; font-size: 17px; flex: 1 1 260px; line-height: 1.3; }
.ts {
  font: 600 12.5px/1 var(--mono);
  color: var(--accent);
  background: var(--accent-soft);
  border: 1px solid transparent;
  border-radius: 6px;
  padding: 6px 9px;
  cursor: pointer;
}
.ts:hover { border-color: var(--accent); }
.ts.playing { border-color: var(--accent); }
.step ul.actions { margin: 12px 0 0; padding-left: 20px; }
.step ul.actions li { margin: 5px 0; }
.note {
  margin: 12px 0 0;
  padding: 9px 12px;
  border-left: 3px solid var(--line);
  color: var(--muted);
  font-size: 14.5px;
}
.keys { margin-top: 11px; display: flex; gap: 7px; flex-wrap: wrap; }
kbd {
  font: 12px/1 var(--mono);
  background: var(--bg);
  border: 1px solid var(--line);
  border-bottom-width: 2px;
  border-radius: 5px;
  padding: 5px 7px;
}
.shots { margin: 14px 0 0; display: grid; gap: 10px; }
.step-head + .shots, .step-head + .clip { margin-top: 12px; }
.shots + .actions, .clip + .actions { margin-top: 14px; }
.shots.multi { grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
.shots figure { margin: 0; position: relative; }
.zoom {
  position: absolute;
  top: 8px;
  right: 8px;
  font: 600 11px/1 var(--mono);
  color: #fff;
  background: rgba(0, 0, 0, .58);
  border: 1px solid rgba(255, 255, 255, .28);
  border-radius: 5px;
  padding: 5px 7px;
  cursor: zoom-in;
  opacity: 0;
  transition: opacity .12s ease;
}
.shots figure:hover .zoom, .zoom:focus { opacity: 1; }
@media (hover: none) { .zoom { opacity: .85; } }

/* Reading small UI text in a screenshot is its own action, separate from
   jumping the video, so it gets its own control and its own overlay. */
.lightbox {
  position: fixed;
  inset: 0;
  z-index: 80;
  background: rgba(0, 0, 0, .9);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 20px;
}
.lightbox[hidden] { display: none; }
.lightbox img {
  max-width: 97vw;
  max-height: 86vh;
  width: auto;
  height: auto;
  border-radius: 8px;
  cursor: zoom-out;
}
.lightbox-bar {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  justify-content: center;
  font: 600 12px/1 var(--mono);
  color: #c8c8c4;
}
.lightbox-bar button {
  font: 600 12px/1 var(--mono);
  color: var(--accent);
  background: rgba(255, 255, 255, .1);
  border: 1px solid transparent;
  border-radius: 5px;
  padding: 7px 10px;
  cursor: pointer;
}
.lightbox-bar button:hover { border-color: var(--accent); }
.shots img {
  width: 100%;
  height: auto;
  display: block;
  border-radius: 8px;
  border: 1px solid var(--line);
  cursor: pointer;
  background: var(--bg);
}
.shots figcaption { font: 600 11.5px/1.4 var(--mono); color: var(--muted); margin-top: 5px; }
.shots img:hover { border-color: var(--accent); }
.skipped { font-size: 13px; color: var(--muted); margin: 10px 0 0; }

/* Clips: a real control strip under the frame, not a hairline drawn on top of
   a dark screenshot where nobody can find it. */
.clip { margin: 14px 0 0; }
.clip-frame {
  position: relative;
  border-radius: 8px 8px 0 0;
  border: 1px solid var(--line);
  border-bottom: 0;
  overflow: hidden;
  background: #000;
}
.clip-frame video { width: 100%; height: auto; display: block; cursor: pointer; }
.clip-frame.paused::after {
  content: "paused";
  position: absolute;
  top: 8px;
  right: 8px;
  font: 600 10.5px/1 var(--mono);
  letter-spacing: .06em;
  text-transform: uppercase;
  color: #fff;
  background: rgba(0, 0, 0, .62);
  border-radius: 4px;
  padding: 5px 7px;
}
.clip-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 0 0 8px 8px;
}
.clip-play {
  font: 600 11px/1 var(--mono);
  text-transform: uppercase;
  letter-spacing: .05em;
  color: var(--accent);
  background: var(--accent-soft);
  border: 1px solid transparent;
  border-radius: 5px;
  padding: 6px 9px;
  cursor: pointer;
  min-width: 58px;
}
.clip-play:hover { border-color: var(--accent); }
.clip-loop {
  font: 600 11px/1 var(--mono);
  text-transform: uppercase;
  letter-spacing: .05em;
  color: var(--muted);
  background: none;
  border: 1px solid var(--line);
  border-radius: 5px;
  padding: 6px 9px;
  cursor: pointer;
  min-width: 52px;
}
.clip-loop.on { color: var(--accent); background: var(--accent-soft); border-color: transparent; }
.clip-loop:hover { border-color: var(--accent); }
.clip-track {
  position: relative;
  flex: 1;
  height: 18px;
  display: flex;
  align-items: center;
  cursor: pointer;
  touch-action: none;
}
.clip-track::before {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  height: 6px;
  border-radius: 3px;
  background: var(--line);
}
.clip-track i {
  position: relative;
  display: block;
  height: 6px;
  width: 0;
  border-radius: 3px;
  background: var(--accent);
}
.clip-track i::after {
  content: "";
  position: absolute;
  right: -6px;
  top: -3px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 2px var(--panel);
}
.clip-time { font: 600 11px/1 var(--mono); color: var(--muted); min-width: 62px; text-align: right; }
.clip figcaption {
  font: 600 11.5px/1.4 var(--mono);
  color: var(--muted);
  margin-top: 6px;
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}
.tag {
  display: inline-block;
  background: var(--accent-soft);
  color: var(--accent);
  border-radius: 4px;
  padding: 2px 6px;
}
.ts-mini {
  font: 600 11.5px/1 var(--mono);
  color: var(--accent);
  background: var(--accent-soft);
  border: 1px solid transparent;
  border-radius: 5px;
  padding: 4px 7px;
  cursor: pointer;
}
.ts-mini:hover { border-color: var(--accent); }

.done-toggle { font-size: 13px; color: var(--muted); display: inline-flex; gap: 6px; align-items: center; cursor: pointer; margin-top: 12px; }
details.transcript { margin-top: 26px; border-top: 1px solid var(--line); padding-top: 16px; }
details.transcript summary { cursor: pointer; color: var(--muted); font-size: 14px; }
.tline { display: flex; gap: 10px; padding: 3px 0; font-size: 14.5px; }
.tline button {
  font: 600 12px/1.5 var(--mono);
  color: var(--accent);
  background: none; border: 0; padding: 0; cursor: pointer; flex: 0 0 58px; text-align: right;
}

/* Floating player: out of the reading column, collapsible, never in the way. */
.player {
  position: fixed;
  z-index: 40;
  right: 16px;
  bottom: 16px;
  width: min(340px, calc(100vw - 32px));
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: 0 10px 30px rgba(0, 0, 0, .22);
  overflow: hidden;
}
.player-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  font: 600 11.5px/1 var(--mono);
  color: var(--muted);
  cursor: pointer;
  user-select: none;
}
.player-head .label { flex: 1; text-transform: uppercase; letter-spacing: .06em; }
.player-head .at { color: var(--accent); }
.player-head .rate { color: var(--muted); }
.player-head .chev { font-size: 13px; }
.player video { width: 100%; display: block; background: #000; }
.player .hint { font-size: 11.5px; color: var(--muted); margin: 0; padding: 7px 10px 9px; }
.player-size {
  font: 600 12px/1 var(--mono);
  color: var(--muted);
  background: none;
  border: 1px solid var(--line);
  border-radius: 5px;
  padding: 4px 6px;
  cursor: pointer;
}
.player-size:hover { color: var(--accent); border-color: var(--accent); }
.player.collapsed video, .player.collapsed .hint { display: none; }

/* Focus mode: the same player, centred and large, for studying one passage.
   Toggled with the button or the f key, dismissed with Escape or the backdrop. */
.player-backdrop {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(0, 0, 0, .66);
  opacity: 0;
  pointer-events: none;
  transition: opacity .15s ease;
}
.player-backdrop.on { opacity: 1; pointer-events: auto; }
.player.focus {
  z-index: 60;
  left: 50%;
  top: 50%;
  right: auto;
  bottom: auto;
  transform: translate(-50%, -50%);
  width: min(1180px, 94vw);
}
.player.focus video { max-height: 78vh; object-fit: contain; }
/* Collapsed, it shrinks to its label so it covers as little as possible. */
.player.collapsed { width: auto; }
.player.collapsed .player-head .label { flex: 0 0 auto; }
@media (max-width: 720px) {
  .player { left: 10px; right: 10px; bottom: 10px; width: auto; }
  .player.collapsed { left: auto; }
}
.keyhint {
  font: 600 11.5px/1 var(--mono);
  color: var(--muted);
  background: none;
  border: 1px solid var(--line);
  border-radius: 5px;
  padding: 4px 7px;
  cursor: pointer;
  margin-left: 8px;
}
.keyhint:hover { color: var(--accent); border-color: var(--accent); }
.shortcuts {
  position: fixed;
  z-index: 70;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  width: min(520px, 92vw);
  max-height: 84vh;
  overflow: auto;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: 0 10px 34px rgba(0, 0, 0, .28);
  padding: 18px 20px;
}
.shortcuts h2 { margin: 0 0 12px; font-size: 15px; }
.shortcuts dl { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 7px 14px; align-items: baseline; }
.shortcuts dt { text-align: right; white-space: nowrap; }
.shortcuts dd { margin: 0; font-size: 14px; }
.shortcuts .close { margin-top: 14px; font-size: 12.5px; color: var(--muted); }
footer.site { color: var(--muted); font-size: 12.5px; padding: 26px 20px; text-align: center; }
ul.cards { list-style: none; margin: 0; padding: 0; display: grid; gap: 12px; }
@media (min-width: 720px) { ul.cards { grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); } }
ul.cards a {
  display: flex; gap: 12px; text-decoration: none; color: inherit;
  background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px; height: 100%;
}
ul.cards a:hover { border-color: var(--accent); }
ul.cards img { width: 128px; height: 72px; object-fit: cover; border-radius: 6px; border: 1px solid var(--line); flex: none; background: var(--bg); }
ul.cards .t { font-weight: 600; font-size: 15px; line-height: 1.3; }
ul.cards .s { font-size: 12.5px; color: var(--muted); margin-top: 4px; }
"""


SCRIPT = """\
(function () {
  var player = document.getElementById('player');
  var video = player ? player.querySelector('video') : null;
  var steps = Array.prototype.slice.call(document.querySelectorAll('.step'));
  var clips = Array.prototype.slice.call(document.querySelectorAll('.clip-frame video'));
  var key = 'v2w:' + (document.body.dataset.lesson || 'lesson');
  function noop() {}

  function clock(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    var minutes = Math.floor(seconds / 60);
    var rest = seconds % 60;
    if (minutes < 60) return minutes + ':' + (rest < 10 ? '0' : '') + rest;
    var hours = Math.floor(minutes / 60);
    minutes = minutes % 60;
    return hours + ':' + (minutes < 10 ? '0' : '') + minutes + ':' + (rest < 10 ? '0' : '') + rest;
  }

  // ---- the floating player --------------------------------------------------
  var setFocus = noop;
  var expandPlayer = noop;
  var isFocused = function () { return false; };

  function syncPlayerHeight() {
    if (!player) return;
    // Centred in focus mode, so the column needs no room reserved at the bottom.
    var height = player.classList.contains('focus') ? 0 : player.offsetHeight;
    document.documentElement.style.setProperty('--player-h', height + 'px');
  }

  if (player) {
    var head = player.querySelector('.player-head');
    var readout = player.querySelector('.at');
    var chevron = player.querySelector('.chev');
    var backdrop = document.getElementById('player-backdrop');
    var sizeButton = player.querySelector('.player-size');
    var focusHint = player.querySelector('.focus-hint');

    var setCollapsed = function (collapsed) {
      player.classList.toggle('collapsed', collapsed);
      if (chevron) chevron.innerHTML = collapsed ? '&#9650;' : '&#9660;';
      try { localStorage.setItem(key + ':player', collapsed ? '1' : '0'); } catch (e) {}
      syncPlayerHeight();
    };

    var stored = null;
    try { stored = localStorage.getItem(key + ':player'); } catch (e) {}
    // Collapsed until asked for: the steps are the page, and clicking any
    // timestamp opens the player anyway.
    setCollapsed(stored === null ? true : stored === '1');

    if (head) {
      head.addEventListener('click', function () {
        setCollapsed(!player.classList.contains('collapsed'));
      });
    }

    isFocused = function () { return player.classList.contains('focus'); };
    setFocus = function (on) {
      if (on) setCollapsed(false);
      player.classList.toggle('focus', on);
      if (backdrop) backdrop.classList.toggle('on', on);
      if (sizeButton) sizeButton.innerHTML = on ? '&#10529;' : '&#10530;';
      if (focusHint) {
        focusHint.innerHTML = on
          ? 'Press <kbd>f</kbd> or <kbd>Esc</kbd> to shrink it again.'
          : 'Press <kbd>f</kbd> for a larger view.';
      }
      syncPlayerHeight();
    };
    expandPlayer = function () {
      if (player.classList.contains('collapsed')) setCollapsed(false);
    };

    if (sizeButton) {
      sizeButton.addEventListener('click', function (event) {
        // The header itself collapses, so this must not bubble to it.
        if (event.stopPropagation) event.stopPropagation();
        setFocus(!isFocused());
      });
    }
    if (backdrop) backdrop.addEventListener('click', function () { setFocus(false); });

    if (video && readout) {
      video.addEventListener('timeupdate', function () {
        readout.textContent = clock(video.currentTime);
      });
      video.addEventListener('loadedmetadata', syncPlayerHeight);
    }
    window.addEventListener('resize', syncPlayerHeight);
    syncPlayerHeight();

    player.expand = expandPlayer;
    player.setFocus = setFocus;
  }

  // ---- playback speed -------------------------------------------------------
  // Kept outside the per-lesson key on purpose: a reading speed is a property
  // of the reader, not of one lesson.
  var RATE_KEY = 'v2w:rate';
  var RATES = [0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3];

  function storedRate() {
    var raw = null;
    try { raw = localStorage.getItem(RATE_KEY); } catch (e) {}
    var value = parseFloat(raw);
    return RATES.indexOf(value) >= 0 ? value : 1;
  }

  var rate = storedRate();
  var rateReadout = player ? player.querySelector('.rate') : null;

  function applyRate() {
    if (video) video.playbackRate = rate;
    // Clips are playback too, so they run at the speed you chose.
    clips.forEach(function (clip) { clip.playbackRate = rate; });
    if (rateReadout) rateReadout.textContent = rate + 'x';
  }
  function nudgeRate(direction) {
    var at = RATES.indexOf(rate);
    if (at < 0) at = RATES.indexOf(1);
    rate = RATES[Math.max(0, Math.min(RATES.length - 1, at + direction))];
    try { localStorage.setItem(RATE_KEY, String(rate)); } catch (e) {}
    applyRate();
  }
  applyRate();
  // Loading a new source resets playbackRate, so reassert it.
  if (video) {
    video.addEventListener('loadedmetadata', applyRate);
    video.addEventListener('ratechange', function () {
      // Respect a change made through the native controls.
      if (video.playbackRate !== rate && RATES.indexOf(video.playbackRate) >= 0) {
        rate = video.playbackRate;
        try { localStorage.setItem(RATE_KEY, String(rate)); } catch (e) {}
        if (rateReadout) rateReadout.textContent = rate + 'x';
      }
    });
  }

  // ---- looping --------------------------------------------------------------
  // Like the speed, this is a property of the reader rather than of a lesson,
  // so it is stored globally. Each clip's control doubles as the indicator.
  var LOOP_KEY = 'v2w:loop';
  var loopButtons = Array.prototype.slice.call(document.querySelectorAll('.clip-loop'));
  var looping = true;
  try { looping = localStorage.getItem(LOOP_KEY) !== '0'; } catch (e) {}

  function applyLoop() {
    clips.forEach(function (clip) { clip.loop = looping; });
    loopButtons.forEach(function (button) {
      button.classList.toggle('on', looping);
      button.textContent = looping ? 'loop' : 'once';
    });
  }
  function toggleLoop() {
    looping = !looping;
    try { localStorage.setItem(LOOP_KEY, looping ? '1' : '0'); } catch (e) {}
    applyLoop();
  }
  applyLoop();
  loopButtons.forEach(function (button) { button.addEventListener('click', toggleLoop); });

  // ---- seeking --------------------------------------------------------------
  function seek(seconds) {
    if (!video) return;
    var apply = function () {
      video.currentTime = seconds;
      var playing = video.play();
      if (playing && playing.catch) playing.catch(noop);
    };
    if (video.readyState === 0) {
      video.addEventListener('loadedmetadata', apply, { once: true });
    } else {
      apply();
    }
    following = true;   // going somewhere on purpose: let the page keep up
    if (history.replaceState) history.replaceState(null, '', '#t=' + Math.floor(seconds));
  }

  // Clicking the same timestamp or screenshot again toggles playback, so a
  // second click is how you stop to look at something.
  var lastJump = null;
  function jump(target, seconds) {
    if (!video) return;
    expandPlayer();
    if (target && target === lastJump) {
      if (video.paused) {
        var playing = video.play();
        if (playing && playing.catch) playing.catch(noop);
      } else {
        video.pause();
      }
      return;
    }
    if (lastJump && lastJump.classList) lastJump.classList.remove('playing');
    lastJump = target;
    if (target && target.classList) target.classList.add('playing');
    seek(seconds);
  }

  // ---- the step cursor ------------------------------------------------------
  // A step can be taller than the window once it carries a clip and a
  // screenshot. Navigation therefore works a page at a time within a step and
  // only moves to the next one at its end, and it aligns a step's top rather
  // than its middle, because the text worth reading is at the top.
  var TOP_INSET = 16;    // matches .step scroll-margin-top
  var EDGE = 24;         // close enough to an edge to count as reaching it
  var PAGE = 0.85;       // of a window height, leaving some overlap for context

  var desired = null;    // where we have asked the page to scroll to
  function currentScroll() {
    return desired === null ? window.scrollY : desired;
  }
  var smooth = !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var raf = window.requestAnimationFrame ? window.requestAnimationFrame.bind(window) : null;
  var cancelRaf = window.cancelAnimationFrame ? window.cancelAnimationFrame.bind(window) : null;

  // The browser's own smooth scrolling has a fixed duration and no way to ask
  // for a shorter one, and it is slow enough to be in the way when you are
  // stepping through a lesson. This is the same motion, briefer. Change
  // SCROLL_MS to taste.
  var SCROLL_MS = 200;
  var scrolling = null;

  function stopScrolling() {
    if (scrolling !== null && cancelRaf) cancelRaf(scrolling);
    scrolling = null;
  }

  function animateScroll(to) {
    stopScrolling();
    var from = window.scrollY;
    var delta = to - from;
    if (!smooth || !raf || Math.abs(delta) < 2) {
      window.scrollTo(0, to);
      return;
    }
    var span = SCROLL_MS;
    var began = performance.now();
    var frame = function (now) {
      var progress = Math.min(1, (now - began) / span);
      var eased = 1 - Math.pow(1 - progress, 3);   // quick off the mark, gentle landing
      window.scrollTo(0, from + delta * eased);
      scrolling = progress < 1 ? raf(frame) : null;
    };
    scrolling = raf(frame);
  }

  function scrollPage(y) {
    var limit = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    desired = Math.max(0, Math.min(y, limit));
    animateScroll(desired);
  }
  // Any scroll of their own makes our idea of the target stale.
  function forgetTarget() {
    desired = null;
    stopScrolling();
  }
  window.addEventListener('wheel', forgetTarget, { passive: true });
  window.addEventListener('touchmove', forgetTarget, { passive: true });

  function docTop(element) {
    return element.getBoundingClientRect().top + window.scrollY;
  }

  function stepClip(index) {
    var step = steps[index];
    return step ? step.querySelector('.clip-frame video') : null;
  }

  // A clip belongs to one step, so it runs while that step is the one being
  // worked on. Visibility alone used to start it, which meant several clips
  // looping at once for a step nobody was on. Still on screen is kept as a
  // second condition: there is no point animating something out of view.
  function syncClipPlayback() {
    var wanted = stepClip(cursor);
    clips.forEach(function (clip) {
      // A clip that has run to its end is finished, not waiting: restarting it
      // here would loop it even with looping turned off.
      var shouldPlay =
        clip === wanted && clip.dataset.onScreen && !clip.dataset.userPaused && !clip.ended;
      if (shouldPlay) {
        if (clip.paused) {
          var playing = clip.play();
          if (playing && playing.catch) playing.catch(noop);
        }
        return;
      }
      if (!clip.paused) clip.pause();
      // Leaving a step rewinds its clip, so returning shows the action from
      // the start rather than halfway through a loop.
      if (clip !== wanted && !clip.dataset.userPaused) clip.currentTime = 0;
    });
  }

  // The page follows the video only after a deliberate jump. Pressing space
  // plays from wherever the playhead happens to sit, which is no statement
  // about which step you are working on, and moving the cursor there would
  // take the clip and the done-marker with it.
  var following = false;

  var cursor = -1;
  function setCursor(index, scroll) {
    if (!steps.length) return;
    index = Math.max(0, Math.min(steps.length - 1, index));
    if (index !== cursor) {
      if (steps[cursor]) steps[cursor].classList.remove('current');
      cursor = index;
      steps[cursor].classList.add('current');
      syncClipPlayback();
    }
    if (scroll) scrollPage(docTop(steps[cursor]) - TOP_INSET);
  }
  setCursor(0, false);

  // After scrolling by hand the cursor can be nowhere near the screen; start
  // from whatever is actually in front of them instead.
  function resyncCursor() {
    var step = steps[cursor];
    if (!step) return;
    var rect = step.getBoundingClientRect();
    if (rect.bottom >= 0 && rect.top <= window.innerHeight) return;
    for (var i = 0; i < steps.length; i++) {
      if (steps[i].getBoundingClientRect().bottom > EDGE) {
        setCursor(i, false);
        return;
      }
    }
  }

  function pageDown() {
    var step = steps[cursor];
    if (!step) return false;
    var bottom = docTop(step) + step.offsetHeight;
    var y = currentScroll();
    var remaining = bottom - (y + window.innerHeight);
    if (remaining <= EDGE) return false;
    scrollPage(y + Math.min(remaining, window.innerHeight * PAGE));
    return true;
  }

  function pageUp() {
    var step = steps[cursor];
    if (!step) return false;
    var y = currentScroll();
    var above = y - (docTop(step) - TOP_INSET);
    if (above <= EDGE) return false;
    scrollPage(y - Math.min(above, window.innerHeight * PAGE));
    return true;
  }

  function goForward() {
    following = false;
    resyncCursor();
    if (pageDown()) return;                       // more of this step to read
    if (cursor < steps.length - 1) setCursor(cursor + 1, true);
  }

  function goToStart() {
    following = false;
    setCursor(0, false);
    scrollPage(0);        // the summary and prerequisites live above step one
  }

  function goToEnd() {
    following = false;
    var last = steps.length - 1;
    if (last < 0) return;
    setCursor(last, false);
    var step = steps[last];
    var top = docTop(step) - TOP_INSET;
    var lastPage = docTop(step) + step.offsetHeight - window.innerHeight;
    scrollPage(Math.max(top, lastPage));
  }

  function goBack() {
    following = false;
    resyncCursor();
    if (pageUp()) return;                         // back up within this step
    if (cursor === 0) return;
    // The exact inverse of going forward: land on the previous step's last
    // page, which is where you were standing when you left it.
    var previous = steps[cursor - 1];
    setCursor(cursor - 1, false);
    var top = docTop(previous) - TOP_INSET;
    var lastPage = docTop(previous) + previous.offsetHeight - window.innerHeight;
    scrollPage(Math.max(top, lastPage));
  }

  document.addEventListener('click', function (event) {
    var step = event.target.closest ? event.target.closest('.step') : null;
    if (step) {
      var at = steps.indexOf(step);
      if (at >= 0) {
        following = false;      // a jump on this same click turns it back on
        setCursor(at, false);
      }
    }
    var target = event.target.closest('[data-t]');
    if (!target) return;
    event.preventDefault();
    jump(target, parseFloat(target.dataset.t));
  });

  // ---- clips ----------------------------------------------------------------
  clips.forEach(function (clip) {
    var figure = clip.closest('.clip');
    var frame = clip.parentNode;
    var controls = figure ? figure.querySelector('.clip-controls') : null;
    var button = controls ? controls.querySelector('.clip-play') : null;
    var track = controls ? controls.querySelector('.clip-track') : null;
    var fill = track ? track.firstElementChild : null;
    var readout = controls ? controls.querySelector('.clip-time') : null;
    var scrubbing = false;

    function paint() {
      var total = clip.duration;
      if (!total || !isFinite(total)) return;
      if (fill) fill.style.width = (100 * clip.currentTime / total) + '%';
      if (readout) readout.textContent = clip.currentTime.toFixed(1) + ' / ' + total.toFixed(1) + 's';
    }
    function showState() {
      frame.classList.toggle('paused', clip.paused);
      if (button) button.textContent = clip.paused ? 'play' : 'pause';
    }
    clip.addEventListener('timeupdate', paint);
    clip.addEventListener('seeked', paint);
    clip.addEventListener('loadedmetadata', paint);
    // A clip loads lazily, and loading resets the rate.
    clip.addEventListener('loadedmetadata', function () { clip.playbackRate = rate; });
    clip.addEventListener('play', showState);
    clip.addEventListener('pause', showState);
    clip.addEventListener('ended', showState);
    showState();

    function toggle() {
      if (clip.paused) {
        delete clip.dataset.userPaused;
        var playing = clip.play();
        if (playing && playing.catch) playing.catch(noop);
      } else {
        clip.pause();
        clip.dataset.userPaused = '1';
      }
      showState();
    }
    clip.v2wToggle = toggle;
    clip.addEventListener('click', toggle);
    if (button) button.addEventListener('click', toggle);

    if (track) {
      var scrubTo = function (event) {
        if (!clip.duration || !isFinite(clip.duration)) return;
        var box = track.getBoundingClientRect();
        var ratio = (event.clientX - box.left) / box.width;
        clip.currentTime = Math.max(0, Math.min(1, ratio)) * clip.duration;
        paint();
      };
      track.addEventListener('pointerdown', function (event) {
        event.preventDefault();
        scrubbing = true;
        // Scrubbing means you want to look, so hold the frame still.
        clip.pause();
        clip.dataset.userPaused = '1';
        showState();
        if (track.setPointerCapture) track.setPointerCapture(event.pointerId);
        scrubTo(event);
      });
      track.addEventListener('pointermove', function (event) {
        if (scrubbing) scrubTo(event);
      });
      var release = function (event) {
        if (!scrubbing) return;
        scrubbing = false;
        if (track.releasePointerCapture && event.pointerId !== undefined) {
          try { track.releasePointerCapture(event.pointerId); } catch (e) {}
        }
      };
      track.addEventListener('pointerup', release);
      track.addEventListener('pointercancel', release);
    }
  });

  // The observer only records what is on screen; whether a clip runs is
  // decided by which step is current. play() loads what it needs on its own,
  // so there is no load() call to abort the request.
  if (clips.length && 'IntersectionObserver' in window) {
    var watcher = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.dataset.onScreen = '1';
        } else {
          delete entry.target.dataset.onScreen;
        }
      });
      syncClipPlayback();
    }, { rootMargin: '150px 0px', threshold: 0.2 });
    clips.forEach(function (clip) { watcher.observe(clip); });
  } else {
    // No observer: fall back to treating every clip as on screen.
    clips.forEach(function (clip) { clip.dataset.onScreen = '1'; });
    syncClipPlayback();
  }

  // Playback moves the cursor too, but never yanks the page while you read.
  if (video && steps.length) {
    video.addEventListener('timeupdate', function () {
      if (!following) return;
      var now = video.currentTime;
      for (var i = 0; i < steps.length; i++) {
        if (now >= parseFloat(steps[i].dataset.start) && now < parseFloat(steps[i].dataset.end)) {
          setCursor(i, false);
          return;
        }
      }
    });
  }

  // ---- done tracking --------------------------------------------------------
  var done = {};
  try { done = JSON.parse(localStorage.getItem(key) || '{}'); } catch (e) { done = {}; }

  function paintDone() {
    var count = 0;
    steps.forEach(function (step) {
      var box = step.querySelector('input[type=checkbox]');
      var isDone = !!done[step.id];
      if (box) box.checked = isDone;
      step.classList.toggle('done', isDone);
      if (isDone) count++;
    });
    var meter = document.querySelector('.progress');
    if (meter && steps.length) {
      meter.textContent = count + ' of ' + steps.length + ' steps done';
    }
  }

  function setDone(index, value) {
    var step = steps[index];
    if (!step) return;
    done[step.id] = value;
    try { localStorage.setItem(key, JSON.stringify(done)); } catch (e) {}
    paintDone();
  }

  steps.forEach(function (step, index) {
    var box = step.querySelector('input[type=checkbox]');
    if (!box) return;
    box.addEventListener('change', function () { setDone(index, box.checked); });
  });
  paintDone();

  // ---- enlarging a screenshot -----------------------------------------------
  // The picture itself still seeks the video; reading the small print in it is
  // a different intent, so it gets its own button and its own overlay.
  var lightbox = document.getElementById('lightbox');
  var zoomImage = lightbox ? lightbox.querySelector('img') : null;
  var zoomPlay = lightbox ? lightbox.querySelector('.lb-play') : null;
  var zoomAt = lightbox ? lightbox.querySelector('.lb-at') : null;
  var zoomTime = 0;

  function zoomOpen() { return !!(lightbox && !lightbox.hidden); }
  function closeZoom() { if (lightbox) lightbox.hidden = true; }
  function openZoom(src, at, alt) {
    if (!lightbox || !src) return;
    zoomImage.src = src;
    zoomImage.alt = alt || '';
    zoomTime = at;
    if (zoomAt) zoomAt.textContent = clock(at);
    if (zoomPlay) zoomPlay.hidden = !video;
    lightbox.hidden = false;
  }

  if (lightbox) {
    lightbox.addEventListener('click', closeZoom);
    if (zoomPlay) {
      zoomPlay.addEventListener('click', function (event) {
        if (event.stopPropagation) event.stopPropagation();
        closeZoom();
        jump(null, zoomTime);
      });
    }
  }

  document.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('[data-zoom]') : null;
    if (!button) return;
    event.preventDefault();
    openZoom(button.dataset.zoom, parseFloat(button.dataset.at), button.title);
  });

  function zoomCursorShot() {
    var step = steps[cursor];
    var button = step ? step.querySelector('[data-zoom]') : null;
    if (button) openZoom(button.dataset.zoom, parseFloat(button.dataset.at), button.title);
  }

  // ---- quick seeking --------------------------------------------------------
  // Home row rather than the arrow keys, which are a reach when the other hand
  // is somewhere else. A clip moves in smaller steps, being seconds not minutes.
  var SEEK_STEP = 10;
  var CLIP_SEEK_STEP = 2;

  var CLIP_EDGE = 0.05;   // near enough to an end to count as being there

  function holdClip(clip) {
    clip.pause();
    clip.dataset.userPaused = '1';
  }
  function runClip(clip) {
    delete clip.dataset.userPaused;
    var playing = clip.play();
    if (playing && playing.catch) playing.catch(noop);
  }

  // Stopping at an end is what you want the first time, since overshooting
  // loses the frame you were heading for. Pressing again at that end is a
  // clear second intent, and only then does it come round to the other side.
  // Reaching an end pauses: the clip loops, so without that the last frame
  // would be gone before it could be seen.
  function nudgeClip(by) {
    var clip = stepClip(cursor);
    if (!clip || !clip.duration || !isFinite(clip.duration)) return;
    var span = clip.duration;
    var at = clip.currentTime;

    if (by > 0) {
      if (at >= span - CLIP_EDGE) {
        clip.currentTime = 0;
        runClip(clip);
      } else if (at + by >= span) {
        clip.currentTime = span;
        holdClip(clip);
      } else {
        clip.currentTime = at + by;
      }
      return;
    }

    if (at <= CLIP_EDGE) {
      clip.currentTime = Math.max(0, span + by);
      runClip(clip);
    } else if (at + by <= 0) {
      clip.currentTime = 0;
      holdClip(clip);
    } else {
      clip.currentTime = at + by;
    }
  }

  // ---- the shortcut list ----------------------------------------------------
  var overlay = document.getElementById('shortcuts');
  function setHelp(on) {
    if (overlay) overlay.hidden = !on;
  }
  function helpOpen() { return overlay && !overlay.hidden; }
  var keyhint = document.getElementById('keyhint');
  if (keyhint) keyhint.addEventListener('click', function () { setHelp(!helpOpen()); });
  if (overlay) overlay.addEventListener('click', function () { setHelp(false); });

  // ---- keyboard -------------------------------------------------------------
  document.addEventListener('keydown', function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    var target = event.target || {};
    var tag = target.tagName || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable) return;
    var stop = function () { if (event.preventDefault) event.preventDefault(); };

    switch (event.key) {
      case '?':
        stop();
        setHelp(!helpOpen());
        return;
      case 'Escape':
        if (zoomOpen()) { stop(); closeZoom(); }
        else if (helpOpen()) { stop(); setHelp(false); }
        else if (isFocused()) { stop(); setFocus(false); }
        return;
      case 'j':
      case 'ArrowDown':
        stop();
        goForward();
        return;
      case 'k':
      case 'ArrowUp':
        stop();
        goBack();
        return;
      case 'Home':
        stop();
        goToStart();
        return;
      case 'End':
        stop();
        goToEnd();
        return;
      case ',':
      case '<':
        stop();
        nudgeRate(-1);
        return;
      case '.':
      case '>':
        stop();
        nudgeRate(1);
        return;
      case 'Enter':
        stop();
        setDone(cursor, true);
        following = false;
        setCursor(cursor + 1, true);
        return;
      case 'x':
        stop();
        setDone(cursor, !done[steps[cursor] && steps[cursor].id]);
        return;
      case 'g': {
        stop();
        var step = steps[cursor];
        if (step) jump(step.querySelector('.ts'), parseFloat(step.dataset.start));
        return;
      }
      case 'c': {
        stop();
        var clip = stepClip(cursor);
        if (clip && clip.v2wToggle) clip.v2wToggle();
        return;
      }
      case 'r':
        stop();
        toggleLoop();
        return;
      case 'z':
        stop();
        if (zoomOpen()) closeZoom();
        else zoomCursorShot();
        return;
      case 'f':
      case 'F':
        stop();
        setFocus(!isFocused());
        return;
      case ' ':
        if (tag === 'BUTTON' || tag === 'LABEL') return;  // space activates those
        if (!video) return;
        stop();
        if (video.paused) {
          var playing = video.play();
          if (playing && playing.catch) playing.catch(noop);
        } else {
          video.pause();
        }
        return;
      case 'h':
      case 'ArrowLeft':
        if (!video) return;
        stop();
        video.currentTime = Math.max(0, video.currentTime - SEEK_STEP);
        return;
      case 'l':
      case 'ArrowRight':
        if (!video) return;
        stop();
        video.currentTime = video.currentTime + SEEK_STEP;
        return;
      case '[':
        stop();
        nudgeClip(-CLIP_SEEK_STEP);
        return;
      case ']':
        stop();
        nudgeClip(CLIP_SEEK_STEP);
        return;
      default:
        return;
    }
  });

  var match = /[#&]t=(\d+(?:\.\d+)?)/.exec(location.hash);
  if (match && video) {
    video.addEventListener('loadedmetadata', function () {
      video.currentTime = parseFloat(match[1]);
    }, { once: true });
  }
})();
"""


SHORTCUTS = [
    ("j / \u2193", "Further down this step, then the next one"),
    ("k / \u2191", "Back up this step, then the previous one"),
    ("Home", "Top of the lesson, with the summary"),
    ("End", "End of the lesson"),
    ("Enter", "Mark the step done and move on"),
    ("x", "Mark the step done or not done"),
    ("g", "Play the video from this step"),
    ("Space", "Play or pause the video"),
    ("h / l", "Back or forward ten seconds (or \u2190 / \u2192)"),
    ("[ / ]", "Nudge this step's clip back or forward"),
    (", / .", "Slower or faster, remembered across lessons"),
    ("c", "Play or pause this step's clip"),
    ("z", "Enlarge this step's screenshot"),
    ("r", "Loop clips, or play them once"),
    ("f", "Larger video, centred"),
    ("Esc", "Close the larger video or this list"),
    ("?", "Show this list"),
]


def _lightbox() -> str:
    return (
        '<div class="lightbox" id="lightbox" hidden>'
        '<img alt="">'
        '<div class="lightbox-bar">'
        '<button class="lb-play" type="button">play from <span class="lb-at">0:00</span></button>'
        "<span>click the picture or press Esc to close</span>"
        "</div></div>"
    )


def _shortcut_overlay() -> str:
    rows = "".join(
        f"<dt><kbd>{_esc(keys)}</kbd></dt><dd>{_esc(what)}</dd>"
        for keys, what in SHORTCUTS
    )
    return (
        '<div class="shortcuts" id="shortcuts" hidden>'
        "<h2>Keyboard</h2>"
        f"<dl>{rows}</dl>"
        '<p class="close">Press <kbd>?</kbd> or <kbd>Esc</kbd> to close.</p>'
        "</div>"
    )


def _clip_note(clip: dict) -> str:
    """Say when a clip is shorter than the stretch of video it covers."""
    source = clip.get("source_seconds")
    length = clip.get("seconds") or 0
    if source and source - length > 0.75:
        return f"{length:.0f}s of {source:.0f}s, still frames trimmed"
    return "drag the bar to scrub"


def _fingerprint(text: str) -> str:
    """Short content hash, so a changed stylesheet or script gets a new URL."""
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _size_attrs(asset: dict) -> str:
    """width/height attributes, so the browser reserves the right box up front."""
    width, height = asset.get("width"), asset.get("height")
    if not width or not height:
        return ""
    return f' width="{int(width)}" height="{int(height)}"'


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def _page(title: str, body: str, *, depth: int, lesson_slug: str | None = None) -> str:
    up = "../" * depth
    body_attrs = f' data-lesson="{_esc(lesson_slug)}"' if lesson_slug else ""
    # The asset filenames never change, so without a content hash in the URL a
    # browser will happily keep running the script it cached last week.
    script = (
        f'<script src="{up}assets/app.js?v={_fingerprint(SCRIPT)}" defer></script>'
        if lesson_slug
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="{up}assets/style.css?v={_fingerprint(STYLE)}">
{script}
</head>
<body{body_attrs}>
{body}
<footer class="site">Built with video-to-website</footer>
</body>
</html>
"""


def _render_step(step: dict, *, has_video: bool) -> str:
    actions = "".join(f"<li>{_esc(action)}</li>" for action in step["actions"])
    keys = "".join(f"<kbd>{_esc(key)}</kbd>" for key in step.get("shortcuts") or [])
    keys_html = f'<div class="keys">{keys}</div>' if keys else ""
    note_html = f'<p class="note">{_esc(step["note"])}</p>' if step.get("note") else ""

    shots = step.get("frames") or (
        [{"time": step["start"], "src": step["frame"]}] if step.get("frame") else []
    )
    cells = [
        f'<figure><img src="{_esc(shot["src"])}" alt="Step {step["index"]} at '
        f'{hms(shot["time"])}" loading="lazy"{_size_attrs(shot)} '
        f'data-t="{float(shot["time"]):.2f}">'
        f'<button class="zoom" type="button" title="Enlarge (z)" '
        f'data-zoom="{_esc(shot["src"])}" data-at="{float(shot["time"]):.2f}">&#10530;</button>'
        f'<figcaption>{hms(shot["time"])}</figcaption></figure>'
        for shot in shots
    ]

    def shots_block(group: list[str]) -> str:
        if not group:
            return ""
        css_class = "shots multi" if len(group) > 1 else "shots"
        return f'<div class="{css_class}">{"".join(group)}</div>'

    clip = step.get("clip")
    clip_html = ""
    if clip:
        # No data-t on the video itself: clicking it pauses the loop, and the
        # caption button is what jumps the main player.
        jump = (
            f'<button class="ts-mini" data-t="{float(clip["time"]):.2f}">{hms(clip["time"])}</button>'
            if has_video
            else f'<span class="ts-mini">{hms(clip["time"])}</span>'
        )
        clip_html = (
            '<figure class="clip"><div class="clip-frame">'
            f'<video src="{_esc(clip["src"])}" muted loop playsinline preload="none"'
            f'{_size_attrs(clip)}></video></div>'
            '<div class="clip-controls">'
            '<button class="clip-play" type="button">pause</button>'
            '<button class="clip-loop on" type="button" title="Loop clips (r)">loop</button>'
            '<div class="clip-track"><i></i></div>'
            f'<span class="clip-time">0.0 / {clip["seconds"]:.1f}s</span>'
            "</div>"
            f'<figcaption><span class="tag">motion</span>{jump}'
            f"<span>{_clip_note(clip)}</span>"
            "</figcaption></figure>"
        )

    time_attr = f' data-t="{step["start"]:.2f}"' if has_video else ""
    time_tag = (
        f'<button class="ts"{time_attr}>{hms(step["start"])}</button>'
        if has_video
        else f'<span class="ts">{hms(step["start"])}</span>'
    )

    # The instructions sit directly under the first clip or screenshot rather
    # than above everything: while you are comparing a picture against your own
    # screen, what to do should be next to it, not a scroll away at the top.
    if clip_html:
        lead, rest = clip_html, shots_block(cells)
    else:
        lead, rest = shots_block(cells[:1]), shots_block(cells[1:])

    text_html = f'<ul class="actions">{actions}</ul>{keys_html}{note_html}'

    return f"""<article class="step" id="step-{step["index"]}" data-start="{step["start"]:.2f}" data-end="{step["end"]:.2f}">
  <div class="step-head">
    <span class="n">{step["index"]}</span>
    <h3>{_esc(step["title"])}</h3>
    {time_tag}
  </div>
  {lead}
  {text_html}
  {rest}
  <label class="done-toggle"><input type="checkbox"> done</label>
</article>"""


def render_lesson_page(lesson: dict, course: dict) -> str:
    has_video = bool(lesson.get("video_href"))
    steps_html = "\n".join(_render_step(step, has_video=has_video) for step in lesson["steps"])

    prereq_html = ""
    if lesson.get("prerequisites"):
        items = "".join(f"<li>{_esc(p)}</li>" for p in lesson["prerequisites"])
        prereq_html = f"<h2>Before you start</h2><ul>{items}</ul>"

    skipped_html = ""
    if lesson.get("skip"):
        items = ", ".join(
            f"{hms(entry['start'])}-{hms(entry['end'])}"
            + (f" ({_esc(entry['reason'])})" if entry.get("reason") else "")
            for entry in lesson["skip"]
        )
        skipped_html = f'<p class="skipped">Left out: {items}</p>'

    summary_html = ""
    if lesson.get("summary") or prereq_html or skipped_html:
        body = f"<p>{_esc(lesson['summary'])}</p>" if lesson.get("summary") else ""
        summary_html = f'<section class="summary">{body}{prereq_html}{skipped_html}</section>'

    if has_video:
        player = (
            '<div class="player-backdrop" id="player-backdrop"></div>'
            '<div class="player" id="player">'
            '<div class="player-head"><span class="label">video</span>'
            '<span class="at">0:00</span><span class="rate">1x</span>'
            '<button class="player-size" type="button" title="Larger view (f)">&#10530;</button>'
            '<span class="chev">&#9660;</span></div>'
            f'<video controls preload="metadata" playsinline '
            f'src="{_esc(lesson["video_href"])}"></video>'
            '<p class="hint">Click a timestamp to jump here, again to pause. '
            '<span class="focus-hint">Press <kbd>f</kbd> for a larger view.</span></p>'
            "</div>"
        )
    else:
        player = ""

    transcript_html = ""
    if lesson.get("transcript"):
        lines = "".join(
            f'<div class="tline"><button data-t="{seg["start"]:.2f}">{hms(seg["start"])}</button>'
            f"<span>{_esc(seg['text'])}</span></div>"
            for seg in lesson["transcript"]
        )
        transcript_html = (
            "<details class=\"transcript\"><summary>Full transcript</summary>"
            f"<div>{lines}</div></details>"
        )

    body = f"""<header class="top">
  <div class="crumbs"><a href="../index.html">All courses</a> / <a href="index.html">{_esc(course["title"])}</a></div>
  <h1>{_esc(lesson["title"])}</h1>
  <div class="meta">{len(lesson["steps"])} steps &middot; {human_duration(lesson["duration"])} of video &middot; {_esc(lesson["source_name"])}<button class="keyhint" type="button" id="keyhint">? keys</button></div>
</header>
<div class="wrap">
  <main>
    {summary_html}
    <div class="progress"></div>
    {steps_html}
    {transcript_html}
  </main>
</div>
{player}
{_lightbox()}
{_shortcut_overlay()}"""
    return _page(lesson["title"], body, depth=1, lesson_slug=lesson["slug"])


def render_course_page(course: dict) -> str:
    cards = []
    for lesson in course["lessons"]:
        thumb = (
            f'<img src="{_esc(lesson["poster"])}" alt="" loading="lazy">'
            if lesson.get("poster")
            else ""
        )
        cards.append(
            f'<li><a href="{_esc(lesson["slug"])}.html">{thumb}<div>'
            f'<div class="t">{_esc(lesson["title"])}</div>'
            f'<div class="s">{len(lesson["steps"])} steps &middot; {human_duration(lesson["duration"])}</div>'
            f"</div></a></li>"
        )
    total = sum(lesson["duration"] for lesson in course["lessons"])
    body = f"""<header class="top">
  <div class="crumbs"><a href="../index.html">All courses</a></div>
  <h1>{_esc(course["title"])}</h1>
  <div class="meta">{len(course["lessons"])} lessons &middot; {human_duration(total)}</div>
</header>
<div class="wrap"><ul class="cards">{"".join(cards)}</ul></div>"""
    return _page(course["title"], body, depth=1)


def render_root_index(courses: list[dict], *, note: str | None = None) -> str:
    cards = []
    for course in courses:
        lessons = course["lessons"]
        poster = next((lesson["poster"] for lesson in lessons if lesson.get("poster")), None)
        thumb = (
            f'<img src="{_esc(course["slug"])}/{_esc(poster)}" alt="" loading="lazy">'
            if poster
            else ""
        )
        total = sum(lesson["duration"] for lesson in lessons)
        cards.append(
            f'<li><a href="{_esc(course["slug"])}/index.html">{thumb}<div>'
            f'<div class="t">{_esc(course["title"])}</div>'
            f'<div class="s">{len(lessons)} lessons &middot; {human_duration(total)}</div>'
            f"</div></a></li>"
        )
    meta = note if note and not courses else f"{len(courses)} courses"
    body = f"""<header class="top">
  <h1>Courses</h1>
  <div class="meta">{_esc(meta)}</div>
</header>
<div class="wrap"><ul class="cards">{"".join(cards)}</ul></div>"""
    return _page("Courses", body, depth=0)


def render_lesson_markdown(lesson: dict) -> str:
    out = [f"# {lesson['title']}", ""]
    if lesson.get("summary"):
        out += [lesson["summary"], ""]
    if lesson.get("prerequisites"):
        out += ["**Before you start**", ""]
        out += [f"- {p}" for p in lesson["prerequisites"]]
        out += [""]
    for step in lesson["steps"]:
        out += [f"## {step['index']}. {step['title']}  `{hms(step['start'])}`", ""]
        out += [f"- {action}" for action in step["actions"]]
        if step.get("shortcuts"):
            out += ["", f"Shortcuts: {', '.join('`' + s + '`' for s in step['shortcuts'])}"]
        if step.get("note"):
            out += ["", f"> {step['note']}"]
        if step.get("clip"):
            clip = step["clip"]
            out += ["", f"[{clip['seconds']:.0f}s clip from {hms(clip['time'])}]({clip['src']})"]
        for shot in step.get("frames") or ([{"time": step["start"], "src": step["frame"]}] if step.get("frame") else []):
            out += ["", f"![step {step['index']} at {hms(shot['time'])}]({shot['src']})"]
        out += [""]
    return "\n".join(out) + "\n"


def write_assets(site_dir: Path) -> None:
    assets = site_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "style.css").write_text(STYLE)
    (assets / "app.js").write_text(SCRIPT)


def write_placeholder(site_dir: Path, note: str) -> None:
    """Give the web server something to serve before the first build lands.

    An empty root is worse than it sounds: nginx answers a directory with no
    index file and no autoindex with 403, which reads as a permissions problem
    rather than as "nothing built yet". The first real build overwrites this.
    """
    site_dir.mkdir(parents=True, exist_ok=True)
    write_assets(site_dir)
    (site_dir / "index.html").write_text(render_root_index([], note=note))


def write_site(site_dir: Path, courses: list[dict], *, write_markdown: bool = True) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    write_assets(site_dir)
    (site_dir / "index.html").write_text(render_root_index(courses))

    for course in courses:
        course_dir = site_dir / course["slug"]
        course_dir.mkdir(parents=True, exist_ok=True)
        (course_dir / "index.html").write_text(render_course_page(course))
        for lesson in course["lessons"]:
            (course_dir / f"{lesson['slug']}.html").write_text(render_lesson_page(lesson, course))
            if write_markdown:
                md_dir = course_dir / "md"
                md_dir.mkdir(parents=True, exist_ok=True)
                (md_dir / f"{lesson['slug']}.md").write_text(render_lesson_markdown(lesson))

    (site_dir / "site.json").write_text(json.dumps(courses, indent=2))
    log(f"site written to {site_dir}")


def link_video(source: Path, target: Path, mode: str) -> str | None:
    """Make the source video reachable from the site. Returns the href, or None."""
    if mode == "none":
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    if mode == "copy":
        shutil.copy2(source, target)
    else:
        target.symlink_to(source.resolve())
    return f"videos/{target.name}"
