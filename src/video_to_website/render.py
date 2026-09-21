"""Static site generation: one page per lesson, plus course and root indexes."""

from __future__ import annotations

import hashlib
import html
import json
import shutil
from pathlib import Path

from .chapters import chapter_groups, lesson_chapter, lesson_numbering
from .util import atomic_write, hms, human_duration, log

PROGRESS_SCRIPT = (Path(__file__).parent / "assets" / "progress.js").read_text(encoding="utf-8")
QUEUE_SCRIPT = PROGRESS_SCRIPT + "\n" + (Path(__file__).parent / "assets" / "queue.js").read_text(encoding="utf-8")
STORAGE_SCRIPT = (Path(__file__).parent / "assets" / "storage.js").read_text(encoding="utf-8")
LIBRARY_SCRIPT = STORAGE_SCRIPT + "\n" + (Path(__file__).parent / "assets" / "library.js").read_text(encoding="utf-8")
WORKERS_SCRIPT = (Path(__file__).parent / "assets" / "workers.js").read_text(encoding="utf-8")
COURSE_SCRIPT = (Path(__file__).parent / "assets" / "course.js").read_text(encoding="utf-8")
NAVIGATION_SCRIPT = (Path(__file__).parent / "assets" / "navigation.js").read_text(encoding="utf-8")

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
#reader-wrap { padding-bottom: calc(24px + var(--player-h) + var(--reading-tail, 0px)); }

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
.video-lesson { margin-bottom: 22px; }
.video-lesson video { display: block; width: 100%; height: auto; max-height: 75vh; background: #000; border-radius: var(--radius); }
.video-lesson .hint { margin: 8px 0 0; font-size: 13px; color: var(--muted); }
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
ul.cards > li {
  display: flex; flex-direction: column; background: var(--panel);
  border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden;
}
ul.cards a.course-card-main {
  display: flex; gap: 12px; text-decoration: none; color: inherit;
  padding: 12px; flex: 1;
}
ul.cards > li:hover { border-color: var(--accent); }
ul.cards .course-card-main > div { min-width: 0; overflow-wrap: anywhere; }
ul.cards .course-activity { display: block; padding: 8px 12px; border-top: 1px solid var(--line); font-size: 12.5px; text-decoration: none; }
ul.cards .course-activity:hover { background: var(--accent-soft); }
ul.cards .course-activity[hidden] { display: none; }
ul.cards a:focus-visible { outline: 2px solid var(--accent); outline-offset: -3px; }
ul.cards img { width: 128px; height: 72px; object-fit: cover; border-radius: 6px; border: 1px solid var(--line); flex: none; background: var(--bg); }
ul.cards .t { font-weight: 600; font-size: 15px; line-height: 1.3; }
ul.cards .s { font-size: 12.5px; color: var(--muted); margin-top: 4px; }
.lesson-list { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: 1fr; gap: 10px; }
.lesson-list a {
  display: flex; align-items: center; gap: 16px; padding: 14px; border: 1px solid var(--line);
  border-radius: var(--radius); background: var(--panel); color: inherit; text-decoration: none;
}
.lesson-list a:hover { border-color: var(--accent); }
.lesson-number, .library-number { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; min-width: 1.5em; }
.lesson-list img { width: 128px; height: 72px; object-fit: cover; border-radius: 6px; flex: none; }
.lesson-list .t { font-size: 16px; font-weight: 600; overflow-wrap: anywhere; }
.lesson-list .s, .lesson-source { margin-top: 5px; color: var(--muted); font-size: 12.5px; overflow-wrap: anywhere; }
.lesson-description { margin: 5px 0; font-size: 14px; color: var(--muted); line-height: 1.45; }
.lesson-nav { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; padding: 18px 0; }
.lesson-nav a { color: var(--accent); max-width: 100%; overflow-wrap: anywhere; }
.course-browser [hidden] { display: none !important; }
.course-tools { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; margin-bottom: 12px; }
.course-tools label { display: grid; gap: 4px; font-size: 12.5px; color: var(--muted); }
.course-tools .course-search-label { flex: 1 1 220px; }
.course-tools input, .course-tools select, .course-browser button {
  font: inherit; font-size: 14px; color: var(--ink); background: var(--panel);
  border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; min-height: 40px;
}
.course-tools input { width: 100%; min-width: 0; }
.course-browser button { cursor: pointer; color: var(--accent); }
.course-browser button:hover { border-color: var(--accent); }
.course-browser :is(a, button, input, select, summary):focus-visible, .course-contents > summary:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 3px;
}
.course-tools-note, .course-results { color: var(--muted); font-size: 12.5px; margin: 8px 0; }
.course-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 12px 0; }
.course-actions .course-results { flex: 1 1 140px; }
.course-chapter { margin-bottom: 14px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); }
.course-chapter > summary { cursor: pointer; padding: 12px 16px; font-size: 15px; font-weight: 600; }
.chapter-meta { display: inline-block; margin-left: 10px; color: var(--muted); font-weight: 400; font-size: 12.5px; }
.course-chapter > .lesson-list { padding: 0 10px 10px; }
.course-browser .lesson-list a > div { min-width: 0; flex: 1; }
.course-browser .lesson-list a[aria-current="page"] { border-color: var(--accent); background: var(--accent-soft); }
.course-browser .current-lesson { color: var(--accent); font-size: 12px; font-weight: 500; margin-left: 8px; }
.course-browser[data-density="compact"] .lesson-list :is(img, .lesson-description, .lesson-source) { display: none; }
.course-browser[data-density="compact"] .lesson-list { gap: 5px; }
.course-browser[data-density="compact"] .lesson-list a { padding: 9px 12px; }
.course-browser[data-density="compact"] .lesson-list .t { font-size: 14px; }
.course-contents { margin-bottom: 22px; border-bottom: 1px solid var(--line); }
.course-contents > summary { cursor: pointer; color: var(--accent); padding: 0 0 12px; font-size: 14px; }
.course-contents .course-browser { padding-bottom: 14px; }
.course-contents .course-groups { max-height: 60vh; overflow-y: auto; overscroll-behavior: contain; padding: 4px; }
.keyhint[hidden] { display: none; }
.visually-hidden { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.lesson-picker { width: min(680px, calc(100vw - 28px)); max-height: 85vh; padding: 20px; color: var(--ink); background: var(--panel); border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 16px 60px #0004; }
.lesson-picker::backdrop { background: #0007; }
.lesson-picker[open] { display: flex; flex-direction: column; }
.lesson-picker > :not(#lesson-picker-results) { flex-shrink: 0; }
.picker-heading { display: flex; gap: 12px; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.picker-heading h2 { margin: 0; font-size: 18px; }
.picker-heading button { font: inherit; font-size: 13px; padding: 6px 10px; color: var(--muted); background: var(--panel); border: 1px solid var(--line); border-radius: 6px; cursor: pointer; }
.lesson-picker label, .picker-count, .picker-hint { font-size: 12.5px; color: var(--muted); }
.lesson-picker input { width: 100%; margin: 6px 0 0; padding: 10px 12px; font: inherit; color: var(--ink); background: var(--bg); border: 1px solid var(--line); border-radius: 7px; }
.lesson-picker :is(input, button, a):focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.picker-count { margin: 10px 0; }
#lesson-picker-results { max-height: 48vh; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
#lesson-picker-results a { display: grid; gap: 3px; padding: 10px 12px; border: 1px solid transparent; border-radius: 7px; color: var(--ink); text-decoration: none; overflow-wrap: anywhere; }
#lesson-picker-results a:hover, #lesson-picker-results a[aria-selected="true"] { background: var(--accent-soft); border-color: var(--accent); }
.picker-title { font-size: 14px; font-weight: 600; }
.picker-meta, .picker-description { font-size: 12.5px; color: var(--muted); }
.picker-hint { margin: 12px 0 0; }
@media (max-width: 520px) {
  .course-tools { gap: 8px; }
  .course-tools .course-search-label { flex-basis: 100%; }
  .course-tools label:not(.course-search-label) { flex: 1 1 85px; min-width: 0; }
  .course-tools label.course-sort-label { flex: 1.4 1 115px; }
  .course-tools select { width: 100%; padding-left: 6px; }
  .course-chapter > summary { padding: 10px; }
  .chapter-meta { font-size: 12px; }
}
.library-toolbar { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; margin: 0 0 16px; }
.library-toolbar label { display: grid; gap: 5px; flex: 1; min-width: 180px; font-size: 13px; }
.library-toolbar input, .library-edit input {
  width: 100%; font: inherit; font-size: 14px; color: var(--ink); padding: 8px 10px;
  background: var(--panel); border: 1px solid var(--line); border-radius: 7px;
}
.library-toolbar select, .managed-sort select {
  font: inherit; font-size: 14px; color: var(--ink); background: var(--panel);
  padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; min-height: 40px; max-width: 100%;
}
.library-toolbar label.library-view-choice { flex: 0 1 130px; min-width: 100px; }
.library-toolbar button, .library-actions button, .library-actions a, .managed-order button, .managed-sort button, .library-edit button {
  font: inherit; font-size: 13px; cursor: pointer; color: var(--accent); background: var(--panel);
  border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px; text-decoration: none;
}
.library-actions button:hover, .library-actions a:hover { border-color: var(--accent); }
.library-actions button:disabled, .library-toolbar button:disabled, .managed-order button:disabled, .managed-sort button:disabled, .library-edit button:disabled { opacity: .45; cursor: default; }
.library-message { margin: 0 0 16px; font-size: 14px; }
.library-message.bad { color: #c0392b; }
.storage-panel { border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); padding: 14px 16px; margin: 0 0 18px; }
.storage-panel h2 { margin: 0 0 12px; font-size: 14px; }
.storage-location + .storage-location { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--line); }
.storage-heading { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 6px; font-size: 14px; }
.storage-meter { margin: 9px 0; }
.storage-detail, .storage-note { margin: 5px 0 0; font-size: 12.5px; color: var(--muted); line-height: 1.45; }
.storage-available { margin: 7px 0 0; font-size: 13px; font-weight: 600; }
.storage-location.low .storage-available { color: var(--accent); }
.storage-location.full .storage-available, #storage-message { color: #b03026; }
.storage-location.full .storage-meter i { background: #b03026; }
#library-courses { margin-top: 12px; }
.managed-course { border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); margin: 0 0 14px; }
.managed-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; padding: 16px; }
.managed-heading h2 { flex: 1; min-width: 160px; font-size: 17px; margin: 0; }
.managed-heading h2 button { border: none; padding: 0; background: none; color: inherit; font: inherit; font-weight: 650; cursor: pointer; text-align: left; overflow-wrap: anywhere; }
.library-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.managed-contents { padding: 0 16px 6px; }
.managed-order { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; padding: 0 0 12px; }
.managed-order > .library-hint { flex: 1 1 180px; }
.managed-sort { display: flex; flex-wrap: wrap; align-items: end; gap: 8px; padding-bottom: 16px; }
.managed-sort label { display: grid; gap: 4px; font-size: 12.5px; color: var(--muted); min-width: 0; }
.managed-sort > .library-hint { flex-basis: 100%; }
.managed-sort button, .managed-order button { min-height: 36px; }
.managed-chapter { border: 1px solid var(--line); border-radius: 7px; margin-bottom: 12px; }
.managed-chapter > summary { cursor: pointer; padding: 10px 12px; font-weight: 600; font-size: 14px; }
.managed-chapter > .managed-lessons { padding: 0 12px; }
.managed-chapter-tools { padding: 0 12px 8px; }
.lesson-selection { display: flex; flex-direction: column; align-items: center; gap: 5px; cursor: pointer; }
.lesson-selection input { width: 17px; height: 17px; accent-color: var(--accent); cursor: pointer; }
.managed-bulk { display: grid; gap: 8px; padding: 12px; margin-bottom: 14px; background: var(--panel); border: 1px solid var(--line); border-radius: 7px; }
.managed-bulk.has-selection { position: sticky; top: 8px; z-index: 2; border-color: var(--accent); box-shadow: 0 3px 12px #0001; }
.managed-bulk-form { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; }
.managed-bulk-form label { display: grid; gap: 4px; font-size: 12.5px; color: var(--muted); flex: 1 1 140px; min-width: 0; }
.managed-bulk-form :is(input, select) { width: 100%; min-width: 0; font: inherit; font-size: 14px; padding: 8px; color: var(--ink); background: var(--panel); border: 1px solid var(--line); border-radius: 6px; }
.managed-bulk-form button { font: inherit; font-size: 13px; padding: 8px 10px; color: var(--accent); background: var(--panel); border: 1px solid var(--line); border-radius: 6px; cursor: pointer; }
.managed-bulk-form button:disabled { opacity: .45; cursor: default; }
#library-courses[data-density="compact"] .lesson-description { display: none; }
#library-courses[data-density="compact"] .managed-lesson { padding: 8px 0; }
#library-courses [hidden] { display: none !important; }
#library-courses :is(button, select, summary):focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.library-hint { margin: 0; color: var(--muted); font-size: 12.5px; line-height: 1.45; overflow-wrap: anywhere; }
.managed-lessons { list-style: none; margin: 0; padding: 0; }
.managed-lesson { display: grid; grid-template-columns: 22px minmax(0, 1fr); gap: 6px 10px; align-items: baseline; padding: 14px 0; border-top: 1px solid var(--line); }
.managed-copy h3 { margin: 0 0 5px; font-size: 15px; line-height: 1.45; overflow-wrap: anywhere; }
.managed-lesson > .library-actions { grid-column: 2; }
.library-edit { display: flex; flex-wrap: wrap; align-items: end; gap: 8px; margin: 0 16px 14px; padding: 12px; border-radius: 7px; background: var(--bg); }
.library-edit label { display: grid; gap: 5px; flex: 1; min-width: 140px; font-size: 13px; }
.library-edit > p { flex-basis: 100%; }
.managed-lesson > .library-edit { grid-column: 2; margin: 4px 0 0; }
@media (max-width: 520px) {
  .lesson-list a { gap: 10px; padding: 12px; }
  .lesson-list img { width: 72px; height: 48px; }
  .lesson-list .t { font-size: 14px; }
  .lesson-list .lesson-number { display: none; }
  .managed-heading > .library-actions { flex-basis: 100%; }
  .library-toolbar label[for="library-search"] { flex-basis: 100%; }
  .library-toolbar label.library-view-choice { flex: 1 1 100px; }
}
.building {
  margin: 0 0 18px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 12px 14px;
}
.building h2 {
  margin: 0 0 10px;
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: .06em;
  color: var(--muted);
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.building h2 .count { font-weight: 400; text-transform: none; letter-spacing: 0; }
.building ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 11px; }
.building li { display: grid; gap: 5px; }
.building .t { font-size: 14px; font-weight: 600; line-height: 1.3; }
.building .s { font-size: 12.5px; color: var(--muted); }
.bar { height: 4px; border-radius: 2px; background: var(--line); overflow: hidden; }
.bar i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .4s ease; }
.bar.indeterminate i { width: 30%; animation: v2w-progress 1.8s ease-in-out infinite; }
.bar.progress-paused i { animation: none; opacity: .5; }
.job-estimate, .building .progress-estimate { font-size: 13px; font-weight: 600; color: var(--ink); margin: 0; }
.job-work { font-size: 13px; color: var(--muted); line-height: 1.5; margin: 0; overflow-wrap: anywhere; }
@keyframes v2w-progress { 0% { transform: translateX(-100%); } 100% { transform: translateX(435%); } }
.building .dot {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--line); margin-right: 7px; vertical-align: middle;
}
.building li.working .dot { background: var(--accent); animation: v2w-pulse 1.3s ease-in-out infinite; }
.building li.failed .dot { background: #c0392b; }
.building li.failed .s { color: #c0392b; }
.building .more { font-size: 12.5px; color: var(--muted); }
.building .more a { color: var(--accent); }
.task-queue { padding: 18px; }
.queue-summary { font-size: 14px; font-weight: 600; margin: 0 0 16px; }
.queue-tools { display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 18px; }
.queue-tools label { display: grid; gap: 5px; font-size: 12.5px; color: var(--muted); }
.queue-tools label:last-child { flex: 1; min-width: 180px; }
.queue-tools input, .queue-tools select {
  width: 100%; font: inherit; font-size: 14px; color: var(--ink);
  background: var(--bg); border: 1px solid var(--line); border-radius: 7px; padding: 8px 10px;
}
.queue-tools input:focus, .queue-tools select:focus { outline: 2px solid var(--accent); outline-offset: 2px; }
.task-queue .job { gap: 8px; padding: 16px 0; border-top: 1px solid var(--line); }
.job-heading { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.task-queue .job h2 { margin: 0; font-size: 16px; color: inherit; letter-spacing: 0; text-transform: none; overflow-wrap: anywhere; }
.job-state { font-size: 12px; font-weight: 600; border-radius: 5px; padding: 3px 7px; background: var(--bg); color: var(--muted); }
.job.working .job-state { color: var(--accent); background: var(--accent-soft); }
.job.failed .job-state, .job-error { color: #c0392b; }
.job-source, .job-detail, .job-error, .queue-notice, .queue-updated, .queue-empty {
  margin: 0; font-size: 13px; line-height: 1.5; overflow-wrap: anywhere;
}
.job-source, .job-detail, .queue-updated { color: var(--muted); }
.queue-notice { margin-bottom: 12px; }
.provider-pause { margin: 0 0 16px; padding: 14px; border: 1px solid var(--accent); border-radius: 8px; background: var(--accent-soft); }
.provider-pause h2 { margin: 0 0 6px; font-size: 16px; }
.provider-pause p { margin: 6px 0; font-size: 13px; overflow-wrap: anywhere; }
.provider-pause button { margin-top: 8px; font: inherit; font-size: 13px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 6px; color: var(--accent); background: var(--panel); cursor: pointer; }
.provider-pause button:disabled { opacity: .5; cursor: default; }
.processing-settings { width: min(520px, calc(100vw - 28px)); max-height: 85vh; padding: 20px; color: var(--ink); background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
.processing-settings::backdrop { background: #0007; }
.processing-settings h2 { margin: 0 0 8px; font-size: 18px; }
.processing-settings p { font-size: 13px; margin: 10px 0; overflow-wrap: anywhere; }
.processing-settings label { display: block; font-size: 14px; margin: 12px 0; }
.processing-settings input[type="number"] { display: block; width: 160px; margin-top: 6px; padding: 8px 10px; font: inherit; color: var(--ink); background: var(--bg); border: 1px solid var(--line); border-radius: 6px; }
.processing-settings input[type="checkbox"] { accent-color: var(--accent); }
.processing-settings [hidden] { display: none; }
.job.blocked .job-state { color: var(--accent); background: var(--accent-soft); }
.queue-course { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; font-size: 13px; margin: 0 0 14px; }
.queue-course[hidden] { display: none; }
.queue-course button { font: inherit; color: var(--accent); background: none; border: 1px solid var(--line); border-radius: 6px; padding: 5px 10px; cursor: pointer; }
.queue-updated { margin-top: 16px; font-size: 12px; }
.job-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.job-actions button, .job-actions a, .building > ul > li > button {
  justify-self: start; font: inherit; font-size: 13px; cursor: pointer;
  color: var(--accent); background: none; border: 1px solid var(--line);
  border-radius: 6px; padding: 5px 10px; text-decoration: none;
}
.job-actions button:hover, .job-actions a:hover { border-color: var(--accent); }
.job-actions button:disabled { opacity: .6; cursor: wait; }
@keyframes v2w-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }
@media (prefers-reduced-motion: reduce) {
  .building li.working .dot { animation: none; }
  .bar i { transition: none; }
  .bar.indeterminate i { animation: none; }
}
.upload { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 16px 18px; }
.upload label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.upload input[type="text"] {
  width: 100%; font: inherit; font-size: 15px; color: inherit;
  background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 9px 11px;
}
.upload input[type="text"]:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.upload .hint { font-size: 12.5px; color: var(--muted); margin: 6px 0 0; }
.drop {
  margin-top: 16px; padding: 24px 18px; text-align: center;
  border: 1.5px dashed var(--line); border-radius: var(--radius); background: var(--bg);
}
.drop.over { border-color: var(--accent); background: var(--accent-soft); }
.drop button {
  font: inherit; font-size: 14px; font-weight: 600; cursor: pointer;
  color: #fff; background: var(--accent); border: 1px solid var(--accent);
  border-radius: 8px; padding: 9px 16px;
}
.drop button:disabled { opacity: .5; cursor: default; }
.queue { list-style: none; margin: 16px 0 0; padding: 0; display: grid; gap: 11px; }
.queue li { display: grid; gap: 5px; }
.queue .t { font-size: 14px; font-weight: 600; line-height: 1.3; word-break: break-word; }
.queue .s { font-size: 12.5px; color: var(--muted); }
.queue li.failed .s { color: #c0392b; }
.queue .again {
  justify-self: start; font: inherit; font-size: 12.5px; cursor: pointer;
  color: var(--accent); background: none; border: 1px solid var(--line); border-radius: 6px; padding: 3px 9px;
}
.upload .message { font-size: 13.5px; margin: 16px 0 0; }
.upload .message.bad { color: #c0392b; }
.library { margin-top: 20px; }
.library > h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 10px; }
.library > .hint { font-size: 13px; color: var(--muted); }
#source-refresh { margin-bottom: 10px; }
.lib-course { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 14px; margin-bottom: 12px; }
.lib-course > h3 { margin: 0 0 4px; font-size: 15px; }
.lib-course > .hint { font-size: 12.5px; color: var(--muted); margin: 0 0 10px; }
.lib-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 7px 0; border-top: 1px solid var(--line); }
.lib-row .n { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 12.5px; min-width: 1.6em; text-align: right; flex: none; }
.lib-row .name { flex: 1 1 220px; min-width: 0; font-size: 14px; word-break: break-word; }
.lib-row .source-note { display: block; font-size: 12px; color: var(--muted); margin-top: 3px; }
.lib-confirm { flex: 1 1 100%; font-size: 13px; color: var(--muted); }
.source-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; }
.lib-row .size { font-size: 12.5px; color: var(--muted); flex: none; }
.lib-row input {
  width: 100%; font: inherit; font-size: 14px; color: inherit;
  background: var(--bg); border: 1px solid var(--accent); border-radius: 6px; padding: 5px 8px;
}
.lib-course button, #source-refresh {
  font: inherit; font-size: 12.5px; cursor: pointer; flex: none;
  color: var(--accent); background: none; border: 1px solid var(--line); border-radius: 6px; padding: 3px 9px;
}
.lib-row button:hover { border-color: var(--accent); }
.lib-course button:disabled { opacity: .45; cursor: default; }
.lib-row button.danger { color: #c0392b; }
.lib-row.busy { opacity: .5; }
"""


STYLE += """
#worker-list { list-style: none; padding: 0; }
.worker-tools { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; margin-bottom: 16px; }
.worker-tools label { display: grid; gap: 4px; margin-left: auto; flex: 0 1 250px; font-size: 13px; color: var(--muted); }
.worker-tools button, .worker-rename button, #worker-pairing button { font: inherit; font-size: 13px; background: var(--panel); color: var(--accent); border: 1px solid var(--line); border-radius: 6px; padding: 9px 12px; cursor: pointer; }
.worker-tools button:disabled, .worker-rename button:disabled { opacity: .5; cursor: default; }
.worker-tools select, .worker-rename input { font: inherit; color: var(--ink); background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; }
#worker-connect:not(:disabled) { background: var(--accent); border-color: var(--accent); color: var(--panel); }
.worker-card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 18px; margin: 16px 0; }
.worker-heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }
.worker-heading h2 { font-size: 18px; margin: 0; overflow-wrap: anywhere; }
.worker-state { font-size: 12px; border: 1px solid var(--line); border-radius: 999px; padding: 3px 10px; }
.worker-state.busy, .worker-state.available { background: var(--accent-soft); color: var(--accent); border-color: var(--accent); }
.worker-subtle { color: var(--muted); font-size: 13px; margin: 5px 0; overflow-wrap: anywhere; }
.worker-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; padding: 16px 0; margin: 12px 0; border-block: 1px solid var(--line); }
.worker-stats strong, .worker-stats span { display: block; }
.worker-stats strong { font-size: 20px; font-variant-numeric: tabular-nums; }
.worker-stats span { font-size: 12px; color: var(--muted); }
.worker-tasks { list-style: none; padding: 0; }
.worker-task { padding: 10px 0; overflow-wrap: anywhere; }
.worker-task + .worker-task { border-top: 1px solid var(--line); }
.worker-task p { margin: 5px 0; }
.worker-history { margin-top: 16px; }
.worker-history summary { cursor: pointer; color: var(--muted); font-size: 13px; }
.worker-delete { padding: 12px; margin-top: 12px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); font-size: 13px; }
.worker-delete p { margin-top: 0; }
.worker-delete button { font: inherit; border: 1px solid var(--line); border-radius: 6px; color: var(--accent); background: var(--panel); padding: 7px 10px; cursor: pointer; }
.worker-delete button + button { margin-left: 10px; }
.worker-delete button:disabled { opacity: .5; cursor: default; }
.worker-rename { display: flex; align-items: end; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
.worker-rename label { display: grid; gap: 4px; flex: 1 1 180px; }
.worker-rename input { width: 100%; min-width: 0; }
.worker-command { width: 100%; padding: 12px; font: 13px/1.6 var(--mono); background: var(--bg); color: var(--ink); border: 1px solid var(--line); border-radius: 6px; resize: vertical; }
#worker-pairing h2 { margin-top: 0; font-size: 18px; }
#worker-pairing h3 { margin-top: 22px; font-size: 15px; }
.worker-platform { display: grid; gap: 4px; max-width: 260px; font-size: 13px; }
.worker-platform select { font: inherit; padding: 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--ink); }
.worker-download { display: inline-block; padding: 9px 12px; background: var(--accent); color: var(--panel); border-radius: 6px; text-decoration: none; }
.worker-install { margin: 12px 0; font-size: 14px; }
.worker-install summary { cursor: pointer; color: var(--accent); }
.worker-install pre { white-space: pre-wrap; overflow-wrap: anywhere; }
.worker-gpu { display: flex; align-items: baseline; gap: 8px; margin-bottom: 12px; font-size: 13px; }
.worker-gpu[hidden] { display: none; }
#worker-pairing button + button { margin-left: 10px; }
@media (max-width: 600px) { .worker-stats { grid-template-columns: repeat(2, 1fr); } .worker-tools label { flex-basis: 100%; margin-left: 0; } }
"""


SCRIPT = """\
(function () {
  var player = document.getElementById('player');
  var video = player ? player.querySelector('video') : document.getElementById('lesson-video');
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
  var togglePlayer = noop;
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
      if (head) head.title = (collapsed ? 'Expand' : 'Collapse') + ' video (v)';
      try { localStorage.setItem(key + ':player', collapsed ? '1' : '0'); } catch (e) {}
      syncPlayerHeight();
    };

    var stored = null;
    try { stored = localStorage.getItem(key + ':player'); } catch (e) {}
    // Collapsed until asked for: the steps are the page, and clicking any
    // timestamp opens the player anyway.
    setCollapsed(stored === null ? true : stored === '1');

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
    togglePlayer = function () {
      var collapse = !player.classList.contains('collapsed');
      if (collapse && isFocused()) setFocus(false);
      setCollapsed(collapse);
    };
    if (head) head.addEventListener('click', togglePlayer);

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
  var rateReadout = player ? player.querySelector('.rate') : document.querySelector('.video-lesson .rate');

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
  var readingTail = 0;
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
    var reader = document.getElementById('reader-wrap');
    if (reader && y > limit) {
      // Leave enough room below the last step to align its notes too.
      readingTail += y - limit;
      reader.style.setProperty('--reading-tail', readingTail + 'px');
      limit = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    }
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
    if (rect.bottom > EDGE && rect.top < window.innerHeight - EDGE) return;
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
    var notes = notesPosition(step);
    var notesAhead = notes !== null && notes > y + EDGE && step.offsetHeight + TOP_INSET > window.innerHeight + EDGE;
    if (remaining <= EDGE && !notesAhead) return false;
    var distance = notesAhead ? notes - y : remaining;
    scrollPage(y + Math.min(distance, window.innerHeight * PAGE));
    return true;
  }

  function pageUp() {
    var step = steps[cursor];
    if (!step) return false;
    var y = currentScroll();
    var above = y - (docTop(step) - TOP_INSET);
    if (above <= EDGE) return false;
    var notes = notesPosition(step);
    var distance = notes !== null && notes < y - EDGE ? y - notes : above;
    scrollPage(y - Math.min(distance, window.innerHeight * PAGE));
    return true;
  }

  function notesPosition(step) {
    var notes = step.querySelector('.actions');
    if (!notes || !(step.querySelector('.clip') || step.querySelector('.shots'))) return null;
    return docTop(notes) - TOP_INSET;
  }

  function lastReadingPosition(step) {
    var top = docTop(step) - TOP_INSET;
    var last = docTop(step) + step.offsetHeight - window.innerHeight;
    var notes = notesPosition(step);
    // The second reading position starts with the instructions, even if that
    // goes further than merely fitting the last screenshot at the screen bottom.
    if (last > top + EDGE && notes !== null) last = Math.max(last, notes);
    return Math.max(top, last);
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
    scrollPage(lastReadingPosition(step));
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
    scrollPage(lastReadingPosition(previous));
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
    var picker = document.getElementById('lesson-picker');
    if (picker && picker.open) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    var target = event.target || {};
    var tag = target.tagName || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable) return;
    if (target.closest && target.closest('.course-contents')) return;
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
        if (tag === 'BUTTON' || tag === 'A' || tag === 'SUMMARY') return;
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
      case 'v':
      case 'V':
        if (!player) return;
        stop();
        if (!event.repeat) togglePlayer();
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

  var match = /[#&]t=(\\d+(?:\\.\\d+)?)/.exec(location.hash);
  if (match && video) {
    video.addEventListener('loadedmetadata', function () {
      video.currentTime = parseFloat(match[1]);
    }, { once: true });
  }
})();
"""


STATUS_SCRIPT = (Path(__file__).parent / "assets" / "status.js").read_text(encoding="utf-8")


UPLOAD_SCRIPT = STORAGE_SCRIPT + "\n" + """\
(function () {
  'use strict';

  var course = document.getElementById('course');
  var list = document.getElementById('courses');
  var drop = document.getElementById('drop');
  var picker = document.getElementById('picker');
  var choose = document.getElementById('choose');
  var queue = document.getElementById('queue');
  var message = document.getElementById('message');
  if (!course || !drop || !picker || !queue) { return; }

  var pending = [];
  var busy = false;

  function say(text, bad) {
    message.textContent = text;
    message.className = bad ? 'message bad' : 'message';
    message.hidden = !text;
  }

  function size(bytes) {
    if (!bytes || bytes < 0) { return '0 B'; }
    if (bytes >= 1073741824) { return (bytes / 1073741824).toFixed(1) + ' GB'; }
    if (bytes >= 1048576) { return Math.round(bytes / 1048576) + ' MB'; }
    return Math.max(1, Math.round(bytes / 1024)) + ' KB';
  }

  var library = document.getElementById('library');
  var libraryList = document.getElementById('library-list');
  var sourceControls = [], reclaimBusy = false;

  function button(text, className, onClick) {
    var el = document.createElement('button');
    el.type = 'button';
    if (className) { el.className = className; }
    el.textContent = text;
    sourceControls.push(el);
    el.addEventListener('click', function () { if (!reclaimBusy) onClick(); });
    return el;
  }

  function api(method, path, body) {
    var init = { method: method };
    if (body) {
      init.body = JSON.stringify(body);
      init.headers = { 'Content-Type': 'application/json' };
    }
    return fetch(path, init).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) { throw new Error(data.error || (method + ' failed (' + res.status + ')')); }
        return data;
      });
    });
  }

  function videoPath(course, name) {
    return 'api/library/' + encodeURIComponent(course) + '/' + encodeURIComponent(name);
  }

  function reclaimVideos(videos) {
    reclaimBusy = true;
    sourceControls.forEach(function (control) { control.disabled = true; });
    var total = 0, failures = [];
    var chain = Promise.resolve();
    videos.forEach(function (video, index) {
      chain = chain.then(function () {
        say('Verifying saved video ' + (index + 1) + ' of ' + videos.length + ': ' + video.name + '…');
        return api('POST', 'api/lessons/' + encodeURIComponent(video.lesson_id) + '/reclaim', { build_id: video.build_id })
          .then(function (result) { total += result.removed_bytes || 0; })
          .catch(function (err) { failures.push(video.name + ': ' + err.message); });
      });
    });
    return chain.then(function () {
      reclaimBusy = false;
      say('Removed ' + size(total) + ' of duplicate uploads. Lessons and saved videos are retained.' +
        (failures.length ? ' ' + failures.join(' ') : ''), !!failures.length);
      return load(false);
    });
  }

  function libRow(courseName, video, index) {
    var row = document.createElement('div');
    row.className = 'lib-row';

    var number = document.createElement('span');
    number.className = 'n';
    number.textContent = index + 1 + '.';
    var name = document.createElement('span');
    name.className = 'name';
    name.textContent = video.name;
    if (video.source_reclaimed) {
      var note = document.createElement('small');
      note.className = 'source-note'; note.textContent = 'Original reclaimed · saved video retained';
      name.appendChild(note);
    }
    var bytes = document.createElement('span');
    bytes.className = 'size';
    bytes.textContent = size(video.bytes);
    row.appendChild(number);
    row.appendChild(name);
    row.appendChild(bytes);
    if (video.href) {
      var open = document.createElement('a'); open.href = video.href;
      open.textContent = 'Open lesson'; row.appendChild(open);
    }

    function act(promise) {
      row.className = 'lib-row busy';
      promise.then(load).catch(function (err) {
        row.className = 'lib-row';
        say(err.message, true);
      });
    }

    var rename = button('Rename file', '', function () {
      var input = document.createElement('input');
      input.type = 'text';
      input.value = video.name;
      name.textContent = '';
      name.appendChild(input);
      rename.remove();
      remove.remove();
      reclaim.remove();
      var save = button('Save', '', function () {
        var wanted = (input.value || '').trim();
        if (!wanted || wanted === video.name) { load(); return; }
        act(api('POST', videoPath(courseName, video.name), { to_name: wanted }));
      });
      row.appendChild(save);
      row.appendChild(button('Cancel', '', load));
      input.focus();
      input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') { save.click(); }
        if (event.key === 'Escape') { load(); }
      });
    });

    // Deleting the lesson and reclaiming its redundant upload are distinct.
    var remove = button('Delete', 'danger', function () {
      rename.remove();
      remove.remove();
      reclaim.remove();
      var label = document.createElement('span');
      label.className = 'lib-confirm';
      label.textContent = video.source_reclaimed ? 'Remove this lesson from the library? Saved media is retained.' : 'Delete the original and remove this lesson from the library?';
      row.appendChild(label);
      row.appendChild(button('Yes, delete', 'danger', function () {
        act(api('DELETE', videoPath(courseName, video.name)));
      }));
      row.appendChild(button('Cancel', '', load));
    });

    var reclaim = button('Reclaim space', '', function () {
      rename.remove(); remove.remove(); reclaim.remove();
      var label = document.createElement('span'); label.className = 'lib-confirm';
      label.textContent = 'Remove the ' + size(video.bytes) + ' uploaded copy? The lesson and saved video stay available, including for reprocessing.';
      row.appendChild(label);
      row.appendChild(button('Reclaim original', '', function () { reclaimVideos([video]); }));
      row.appendChild(button('Cancel', '', load));
    });
    reclaim.disabled = !video.reclaimable;
    reclaim.title = video.reclaim_reason || 'Verify the saved video and remove only the duplicate upload.';

    if (video.file_actions !== false) {
      if (!video.source_reclaimed) row.appendChild(rename);
      row.appendChild(remove);
    }
    if (video.lesson_id && !video.source_reclaimed) row.appendChild(reclaim);
    return row;
  }

  function renderLibrary(courses) {
    sourceControls = [];
    libraryList.textContent = '';
    var any = false;
    courses.forEach(function (course) {
      if (!course.videos.length) { return; }
      any = true;
      var box = document.createElement('div');
      box.className = 'lib-course';
      var heading = document.createElement('h3');
      heading.textContent = course.title || course.name;
      var hint = document.createElement('p');
      hint.className = 'hint';
      hint.textContent = 'Original source files. Edit displayed titles and reading order in Library.';
      box.appendChild(heading);
      box.appendChild(hint);
      var candidates = course.videos.filter(function (video) { return video.reclaimable; });
      if (candidates.length) {
        var tools = document.createElement('div'); tools.className = 'source-tools';
        var total = candidates.reduce(function (bytes, video) { return bytes + video.bytes; }, 0);
        tools.appendChild(button('Reclaim completed uploads (' + size(total) + ')', '', function () {
          tools.textContent = '';
          var label = document.createElement('span'); label.className = 'lib-confirm';
          label.textContent = 'Verify and remove ' + candidates.length + ' duplicate uploads? All lessons and saved videos stay available.';
          tools.appendChild(label);
          tools.appendChild(button('Reclaim ' + candidates.length + ' originals', '', function () { reclaimVideos(candidates); }));
          tools.appendChild(button('Cancel', '', load));
        }));
        box.appendChild(tools);
      }
      course.videos.forEach(function (video, index) {
        box.appendChild(libRow(course.name, video, index));
      });
      libraryList.appendChild(box);
    });
    library.hidden = !any;
  }

  // The API is the only part of the site that is not a static file, so it can
  // be missing entirely -- a locally built copy, or a server with uploads off.
  function load(clearMessage) {
    if (reclaimBusy) return Promise.resolve();
    V2WStorage.refresh(course.value || '');
    return fetch('api/library').then(function (res) {
      if (!res.ok) { throw new Error(res.status); }
      return res.json();
    }).then(function (data) {
      var courses = data.courses || [];
      list.textContent = '';
      courses.forEach(function (course) {
        if (course.name === '.' || course.name.indexOf('/') !== -1) return;
        var option = document.createElement('option');
        option.value = course.name;
        if (course.title) { option.label = course.title; }
        list.appendChild(option);
      });
      if (library && libraryList) { renderLibrary(courses); }
      if (clearMessage !== false) { say(''); }
    }).catch(function () {
      choose.disabled = true;
      if (library) { library.hidden = true; }
      say('This copy of the site cannot take uploads. Add videos to the library folder directly.', true);
    });
  }

  load();
  var sourceRefresh = document.getElementById('source-refresh');
  if (sourceRefresh) sourceRefresh.addEventListener('click', function () { load(); });

  function row(file) {
    var li = document.createElement('li');
    var title = document.createElement('div');
    title.className = 't';
    title.textContent = file.name;
    var status = document.createElement('div');
    status.className = 's';
    status.textContent = size(file.size) + ' \u00b7 waiting';
    var bar = document.createElement('div');
    bar.className = 'bar';
    var fill = document.createElement('i');
    bar.appendChild(fill);
    li.appendChild(title);
    li.appendChild(status);
    li.appendChild(bar);
    queue.appendChild(li);
    return { li: li, status: status, fill: fill, bar: bar };
  }

  function send(job, overwrite) {
    job.ui.status.textContent = size(job.file.size) + ' · checking free space';
    V2WStorage.check(job.course, job.file.size).then(function () {
      transfer(job, overwrite);
    }).catch(function (error) {
      fail(job, error.message, false);
    });
  }

  function transfer(job, overwrite) {
    job.ui.status.textContent = size(job.file.size) + ' · uploading';
    var name = job.file.name;
    var url = 'api/library/' + encodeURIComponent(job.course) + '/' + encodeURIComponent(name);
    if (overwrite) { url += '?overwrite=1'; }

    // XHR rather than fetch: it is still the only one that reports upload
    // progress, and a 2 GB lesson with no progress bar is unusable.
    var xhr = new XMLHttpRequest();
    xhr.open('PUT', url, true);
    xhr.upload.addEventListener('progress', function (event) {
      if (!event.lengthComputable) { return; }
      var pct = Math.round(100 * event.loaded / event.total);
      job.ui.fill.style.width = pct + '%';
      job.ui.status.textContent = size(job.file.size) + ' \u00b7 ' + pct + '%';
    });
    xhr.addEventListener('load', function () {
      if (xhr.status === 201) {
        job.ui.li.className = 'uploaded';
        job.ui.fill.style.width = '100%';
        job.ui.status.textContent = size(job.file.size) + ' \u00b7 uploaded to ' + job.course;
        next();
        return;
      }
      var detail = '';
      try { detail = JSON.parse(xhr.responseText).error || ''; } catch (e) { detail = ''; }
      fail(job, detail || ('upload failed (' + xhr.status + ')'), xhr.status === 409);
    });
    xhr.addEventListener('error', function () { fail(job, 'the connection dropped', false); });
    xhr.addEventListener('abort', function () { fail(job, 'cancelled', false); });
    xhr.send(job.file);
  }

  function fail(job, text, offerReplace) {
    job.ui.li.className = 'failed';
    job.ui.bar.hidden = true;
    job.ui.status.textContent = text;
    var again = document.createElement('button');
    again.type = 'button';
    again.className = 'again';
    again.textContent = offerReplace ? 'Replace it' : 'Retry upload';
    again.addEventListener('click', function () {
      again.remove();
      job.ui.li.className = '';
      job.ui.bar.hidden = false;
      job.ui.status.textContent = size(job.file.size) + ' \u00b7 waiting';
      job.overwrite = offerReplace || !!job.overwrite;
      pending.push(job);
      if (!busy) { next(); }
    });
    job.ui.li.appendChild(again);
    next();
  }

  function next() {
    var job = pending.shift();
    if (!job) {
      busy = false;
      if (queue.children.length) {
        load(false);
        var failed = Array.prototype.filter.call(queue.children, function (row) { return row.className === 'failed'; }).length;
        say(failed ? failed + ' upload(s) need attention. Uploaded videos are queued for the builder.'
          : 'Uploaded. The builder will process these videos — watch progress on the home page.');
      }
      return;
    }
    busy = true;
    job.ui.status.textContent = size(job.file.size) + ' \u00b7 uploading';
    send(job, !!job.overwrite);
  }

  function add(files) {
    var name = (course.value || '').trim();
    if (!name) {
      say('Give the course a name first.', true);
      course.focus();
      return;
    }
    // One at a time: these are gigabytes over a house network, and parallel
    // uploads just make every one of them slower.
    Array.prototype.forEach.call(files, function (file) {
      pending.push({ file: file, course: name, ui: row(file) });
    });
    say('');
    if (!busy) { next(); }
  }

  choose.addEventListener('click', function () { picker.click(); });
  picker.addEventListener('change', function () { add(picker.files); picker.value = ''; });

  ['dragenter', 'dragover'].forEach(function (type) {
    drop.addEventListener(type, function (event) {
      event.preventDefault();
      drop.classList.add('over');
    });
  });
  ['dragleave', 'drop'].forEach(function (type) {
    drop.addEventListener(type, function (event) {
      event.preventDefault();
      drop.classList.remove('over');
    });
  });
  drop.addEventListener('drop', function (event) {
    if (event.dataTransfer && event.dataTransfer.files) { add(event.dataTransfer.files); }
  });

  // Losing a half-finished upload to a stray click is worth one confirmation.
  window.addEventListener('beforeunload', function (event) {
    if (!busy) { return; }
    event.preventDefault();
    event.returnValue = '';
  });
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
    ("v", "Collapse or expand the floating video"),
    ("Shift+J / Shift+K", "Next / previous lesson"),
    ("Shift+L / Shift+H", "Next / previous chapter"),
    ("/", "Find and jump to a lesson"),
    ("Esc", "Close the larger video or this list"),
    ("?", "Show this list"),
]


def _lesson_picker() -> str:
    return '''<dialog id="lesson-picker" class="lesson-picker" aria-labelledby="lesson-picker-title">
  <div class="picker-heading"><h2 id="lesson-picker-title">Jump to a lesson</h2><button type="button" id="lesson-picker-close" aria-label="Close lesson picker">Close <kbd>Esc</kbd></button></div>
  <label for="lesson-picker-search">Find by title, description, filename, or chapter</label>
  <input id="lesson-picker-search" type="search" autocomplete="off" spellcheck="false" role="combobox"
    aria-autocomplete="list" aria-expanded="true" aria-controls="lesson-picker-results" placeholder="Try a few words or letters…">
  <p id="lesson-picker-count" class="picker-count" role="status" aria-live="polite"></p>
  <div id="lesson-picker-results" role="listbox" aria-label="Lessons" tabindex="-1"></div>
  <p class="picker-hint">↑ ↓ to choose · Enter to open · Esc to close. Shortcuts follow chapter reading order.</p>
</dialog><span id="lesson-navigation-status" class="visually-hidden" role="status" aria-live="polite"></span>'''


def _lightbox() -> str:
    return (
        '<div class="lightbox" id="lightbox" hidden>'
        '<img alt="">'
        '<div class="lightbox-bar">'
        '<button class="lb-play" type="button">play from <span class="lb-at">0:00</span></button>'
        "<span>click the picture or press Esc to close</span>"
        "</div></div>"
    )


def _shortcut_overlay(*, video_only: bool = False) -> str:
    shortcuts = SHORTCUTS
    if video_only:
        allowed = {"Space", "h / l", ", / .", "Home", "Shift+J / Shift+K", "Shift+L / Shift+H", "/", "Esc", "?"}
        shortcuts = [(keys, "Close this list" if keys == "Esc" else what) for keys, what in SHORTCUTS if keys in allowed]
    rows = "".join(
        f"<dt><kbd>{_esc(keys)}</kbd></dt><dd>{_esc(what)}</dd>"
        for keys, what in shortcuts
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


def _page(
    title: str,
    body: str,
    *,
    depth: int,
    lesson_slug: str | None = None,
    scripts: tuple[tuple[str, str], ...] = (),
) -> str:
    up = "../" * depth
    body_attrs = f' data-lesson="{_esc(lesson_slug)}"' if lesson_slug else ""
    # The asset filenames never change, so without a content hash in the URL a
    # browser will happily keep running the script it cached last week.
    script = "\n".join(
        f'<script src="{up}assets/{name}?v={_fingerprint(source)}" defer></script>'
        for name, source in scripts
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


def lesson_title(lesson: dict) -> str:
    """Preserve source numbering; a catalog title is an explicit user override."""
    return lesson.get("display_title") or (Path(lesson["source_name"]).stem if lesson.get("source_name") else lesson["title"])


def lesson_description(lesson: dict) -> str:
    description = lesson.get("description", lesson.get("title", ""))
    return description if description != lesson_title(lesson) else ""


def lesson_format(lesson: dict) -> str:
    return f'{len(lesson["steps"])} steps' if lesson["steps"] else 'Video lesson'


def _course_browser(course: dict, *, current_slug: str | None = None) -> str:
    lessons = course["lessons"]
    reader = current_slug is not None
    runs = chapter_groups(lessons)
    has_chapters = any(run["number"] is not None for run in runs)
    positions = {lesson["slug"]: index for index, lesson in enumerate(lessons, 1)}
    reading_positions = {item["slug"]: index for index, item in enumerate(
        [item for group in runs for item in group["lessons"]], 1)}

    def card(lesson: dict) -> str:
        position = positions[lesson["slug"]]
        title = lesson_title(lesson)
        source = lesson.get("source_name", "")
        description = lesson_description(lesson)
        chapter = lesson_chapter(lesson)
        numbering = lesson_numbering(lesson)
        current = lesson["slug"] == current_slug
        thumb = (
            f'<img src="{_esc(lesson["poster"])}" alt="" loading="lazy">'
            if lesson.get("poster") and not reader else ""
        )
        description_html = f'<p class="lesson-description">{_esc(description)}</p>' if description else ""
        source_html = f'<div class="lesson-source">{_esc(source)}</div>' if source and title != Path(source).stem else ""
        current_html = '<span class="current-lesson">Current lesson</span>' if current else ""
        chapter_label = f"Chapter {chapter}" if chapter is not None else "Other lessons"
        search = " ".join((title, description, source, chapter_label))
        return (
            f'<li data-course-lesson="{_esc(lesson["slug"])}" data-position="{position}" '
            f'data-reading-position="{reading_positions[lesson["slug"]]}" '
            f'data-title="{_esc(title)}" data-lesson-number="{numbering[1] if numbering else ""}" '
            f'data-chapter="{chapter if chapter is not None else ""}" '
            f'data-duration="{float(lesson["duration"])}" data-search="{_esc(search)}" '
            f'data-current="{str(current).lower()}">'
            f'<a href="{_esc(lesson["slug"])}.html"' + (' aria-current="page"' if current else '') + '>'
            f'<span class="lesson-number" aria-label="Lesson {reading_positions[lesson["slug"]]}">{reading_positions[lesson["slug"]]}</span>{thumb}<div>'
            f'<div class="t">{_esc(title)}{current_html}</div>{description_html}{source_html}'
            f'<div class="s">{lesson_format(lesson)} &middot; {human_duration(lesson["duration"])}</div>'
            '</div></a></li>'
        )

    groups = []
    if has_chapters:
        for index, run in enumerate(runs):
            opened = any(item["slug"] == current_slug for item in run["lessons"]) if reader else index == 0
            count = len(run["lessons"])
            duration = human_duration(sum(item["duration"] for item in run["lessons"]))
            groups.append(
                f'<details class="course-chapter" data-chapter-key="{run["key"]}"' + (' open' if opened else '') + '>'
                f'<summary><span>{run["label"]}</span><span class="chapter-meta">'
                f'{count} {"lesson" if count == 1 else "lessons"} &middot; {duration}</span></summary>'
                f'<ol class="lesson-list">{"".join(card(item) for item in run["lessons"])}</ol></details>'
            )
    else:
        groups.append(f'<ol class="lesson-list">{"".join(card(item) for item in lessons)}</ol>')

    course_id = _esc(course.get("id") or course["slug"])
    return f'''<nav class="course-browser" aria-label="Course lessons" data-course="{course_id}"
  data-context="{"reader" if reader else "course"}" data-density="{"compact" if reader else "detailed"}">
  <div class="course-tools" data-course-controls hidden>
    <label class="course-search-label">Find a lesson<input type="search" data-course-search placeholder="Title, description, or chapter" autocomplete="off"></label>
    <label class="course-sort-label">Sort view<select data-course-sort><option value="saved">Saved order</option><option value="number">Lesson number</option><option value="title">Title A–Z</option><option value="shortest">Shortest first</option></select></label>
    <label>Group<select data-course-view><option value="chapters">Chapters</option><option value="flat">All lessons</option></select></label>
    <label>Rows<select data-course-density><option value="detailed">Detailed</option><option value="compact">Compact</option></select></label>
  </div>
  <p class="course-tools-note" data-course-controls hidden>View settings stay in this browser. <a href="../library.html?course={course_id}">Edit reading order in Library</a>.</p>
  <div class="course-actions" data-course-controls hidden>
    <span class="course-results" role="status" aria-live="polite"></span>
    <button type="button" data-course-clear hidden>Clear search</button>
    <button type="button" data-course-expand>Expand all</button>
    <button type="button" data-course-collapse>Collapse all</button>
  </div>
  <div class="course-groups">{"".join(groups)}</div>
  <p class="course-empty" hidden>No lessons match your search.</p>
</nav>'''


def render_lesson_page(lesson: dict, course: dict) -> str:
    has_video = bool(lesson.get("video_href"))
    video_only = not bool(lesson["steps"])
    steps_html = "\n".join(_render_step(step, has_video=has_video) for step in lesson["steps"])

    prereq_html = ""
    if lesson.get("prerequisites"):
        items = "".join(f"<li>{_esc(p)}</li>" for p in lesson["prerequisites"])
        prereq_html = f"<h2>Before you start</h2><ul>{items}</ul>"

    skipped_html = ""
    if lesson.get("skip") and not video_only:
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

    inline_video = ""
    if video_only:
        player = ""
        if has_video:
            dimensions = _size_attrs({"width": lesson.get("video_width"), "height": lesson.get("video_height")})
            poster = f' poster="{_esc(lesson["poster"])}"' if lesson.get("poster") else ""
            inline_video = (
                '<section class="video-lesson" aria-label="Original lesson video">'
                f'<video id="lesson-video" controls playsinline preload="metadata" src="{_esc(lesson["video_href"])}"{dimensions}{poster}></video>'
                '<p class="hint">Watch the original lesson. <kbd>Space</kbd> to play/pause · '
                '<kbd>h</kbd> / <kbd>l</kbd> to seek · <span class="rate">1x</span> playback speed. '
                f'<a href="{_esc(lesson["video_href"])}" download="{_esc(lesson.get("source_name", ""))}">Download video</a>.</p></section>'
            )
        else:
            inline_video = '<section class="summary"><p>This video lesson has no written steps. The original video is not included in this export.</p></section>'
    elif has_video:
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
            'Press <kbd>v</kbd> to collapse or expand. '
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

    neighbours = []
    course_lessons = [item for group in chapter_groups(course.get("lessons", [])) for item in group["lessons"]]
    current = next((i for i, item in enumerate(course_lessons) if item["slug"] == lesson["slug"]), None)
    if current is not None:
        for index, label in ((current - 1, "Previous"), (current + 1, "Next")):
            if 0 <= index < len(course_lessons):
                other = course_lessons[index]
                neighbours.append(f'<a href="{_esc(other["slug"])}.html">{label}: {_esc(lesson_title(other))}</a>')
    navigation = '<nav class="lesson-nav" aria-label="Lesson navigation">' + "".join(neighbours) + '</nav>' if neighbours else ""
    contents = ""
    if current is not None:
        chapter = lesson_chapter(lesson)
        chapter_label = f" &middot; Chapter {chapter}" if chapter is not None else ""
        contents = (
            '<details class="course-contents"><summary>Course contents'
            f'{chapter_label} &middot; Lesson {current + 1} of {len(course_lessons)}</summary>'
            f'{_course_browser(course, current_slug=lesson["slug"])}</details>'
        )
    description = f'<p class="lesson-description">{_esc(lesson_description(lesson))}</p>' if lesson_description(lesson) else ""
    body = f"""<header class="top">
  <div class="crumbs"><a href="../index.html">All courses</a> / <a href="index.html">{_esc(course["title"])}</a></div>
  <h1>{_esc(lesson_title(lesson))}</h1>
  {description}
  <div class="meta">{lesson_format(lesson)} &middot; {human_duration(lesson["duration"])} of video &middot; {_esc(lesson["source_name"])}<button class="keyhint" type="button" id="keyhint">? keys</button><button class="keyhint" type="button" id="lesson-picker-open" hidden>Jump to lesson <kbd>/</kbd></button></div>
</header>
<div class="wrap" id="reader-wrap">
  {contents}
  <main>
    {inline_video}
    {summary_html}
    {'<div class="progress"></div>' if not video_only else ''}
    {steps_html}
    {transcript_html}
    {navigation}
  </main>
</div>
{player}
{_lightbox()}
{_shortcut_overlay(video_only=video_only)}
{_lesson_picker()}"""
    return _page(
        lesson_title(lesson), body, depth=1,
        lesson_slug=lesson.get("reading_key") or lesson.get("id") or course["slug"] + ":" + lesson["slug"],
        scripts=(("app.js", SCRIPT), ("course.js", COURSE_SCRIPT), ("navigation.js", NAVIGATION_SCRIPT))
    )


def render_course_page(course: dict) -> str:
    total = sum(lesson["duration"] for lesson in course["lessons"])
    body = f"""<header class="top">
  <div class="crumbs"><a href="../index.html">All courses</a> / <a href="../library.html?course={_esc(course.get('id') or course['slug'])}">Organize course</a></div>
  <h1>{_esc(course["title"])}</h1>
  <div class="meta">{len(course["lessons"])} lessons &middot; {human_duration(total)}<button class="keyhint" type="button" id="lesson-picker-open" hidden>Jump to lesson <kbd>/</kbd></button></div>
</header>
<main class="wrap">{_course_browser(course)}</main>{_lesson_picker()}"""
    return _page(course["title"], body, depth=1, scripts=(("course.js", COURSE_SCRIPT), ("navigation.js", NAVIGATION_SCRIPT)))


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
            f'<li data-course-id="{_esc(course.get("id", ""))}" data-course-slug="{_esc(course["slug"])}" '
            f'data-course-title="{_esc(course["title"])}"><a class="course-card-main" href="{_esc(course["slug"])}/index.html">{thumb}<div>'
            f'<div class="t">{_esc(course["title"])}</div>'
            f'<div class="s">{len(lessons)} lessons &middot; {human_duration(total)}</div>'
            '</div></a><a class="course-activity" href="queue.html" hidden></a></li>'
        )
    meta = note if note and not courses else f"{len(courses)} courses"
    body = f"""<header class="top">
  <div class="crumbs"><a href="library.html">Manage library</a> / <a href="upload.html">Add videos</a> / <a href="queue.html">Task queue</a> / <a href="workers.html">Workers</a></div>
  <h1>Courses</h1>
  <div class="meta" id="course-count">{_esc(meta)}</div>
</header>
<main class="wrap"><ul class="cards" id="course-cards">{"".join(cards)}</ul></main>"""
    return _page("Courses", body, depth=0, scripts=(("status.js", STATUS_SCRIPT),))


def _storage_panel() -> str:
    return """<section class="storage-panel" id="storage-panel" aria-label="Library disk space">
  <h2>Storage</h2>
  <div id="storage-locations"></div>
  <p id="storage-message" class="storage-detail" role="status">Checking available disk space…</p>
  <p class="storage-note">Capacity is shared by all files on this disk. Processing needs additional space for snapshots and generated media.</p>
</section>"""


def render_library_page() -> str:
    body = f"""<header class="top">
  <div class="crumbs"><a href="index.html">All courses</a> / <a href="upload.html">Add videos</a> / <a href="queue.html">Task queue</a> / <a href="workers.html">Workers</a></div>
  <h1>Library</h1>
  <div class="meta" id="library-count">Loading your courses…</div>
</header>
<main class="wrap">
  {_storage_panel()}
  <div class="library-toolbar">
    <label for="library-search">Find a course or lesson<input id="library-search" type="search" placeholder="Title, filename, description, or chapter" autocomplete="off"></label>
    <label class="library-view-choice">Group lessons<select id="library-group"><option value="chapters">Chapters</option><option value="flat">All lessons</option></select></label>
    <label class="library-view-choice">Rows<select id="library-density"><option value="detailed">Detailed</option><option value="compact">Compact</option></select></label>
    <button id="library-refresh" type="button">Refresh</button>
    <button id="library-sort-courses" type="button" disabled>Sort courses by name</button>
    <button id="library-reset" type="button" disabled>Use folder order</button>
  </div>
  <p class="library-hint">Sorting here saves reading order for everyone. New lessons are added after a custom order; sort again to place them by name.</p>
  <p id="library-message" class="library-message" role="status" aria-live="polite" hidden></p>
  <div id="library-courses"></div>
  <p id="library-empty" hidden></p>
  <noscript>Enable JavaScript to organize your library.</noscript>
</main>"""
    return _page("Library", body, depth=0, scripts=(("library.js", LIBRARY_SCRIPT),))


def render_queue_page() -> str:
    body = """<header class="top">
  <div class="crumbs"><a href="index.html">All courses</a> / <a href="library.html">Manage library</a> / <a href="upload.html">Add videos</a> / <a href="workers.html">Workers</a></div>
  <h1>Task queue</h1>
  <div class="meta">See what's processing and what's coming next. Cancelling keeps your source video.</div>
</header>
<main class="wrap">
  <section class="building task-queue" aria-label="Processing queue">
    <div id="provider-pauses"></div>
    <p id="queue-summary" class="queue-summary" role="status" aria-live="polite">Loading the queue…</p>
    <div id="queue-course" class="queue-course" hidden><span id="queue-course-name"></span><button id="queue-all-courses" type="button">Show all courses</button></div>
    <div class="queue-tools">
      <label for="job-filter">Show
        <select id="job-filter">
          <option value="active">Running and waiting</option>
          <option value="queued">Waiting</option>
          <option value="blocked">API credits / billing</option>
          <option value="failed">Failed</option>
          <option value="cancelled">Cancelled</option>
          <option value="done">Ready</option>
          <option value="all">All lessons</option>
        </select>
      </label>
      <label for="job-search">Find a lesson
        <input id="job-search" type="search" placeholder="Course or filename" autocomplete="off">
      </label>
    </div>
    <p id="queue-notice" class="queue-notice" role="status" hidden></p>
    <ul id="job-list"></ul>
    <p id="queue-empty" class="queue-empty" hidden></p>
    <p id="queue-updated" class="queue-updated"></p>
    <noscript>Enable JavaScript to view live processing status and manage the queue.</noscript>
  </section>
</main>
<dialog id="processing-settings" class="processing-settings" aria-labelledby="processing-settings-title">
  <h2 id="processing-settings-title">Processing settings</h2>
  <p id="processing-settings-lesson"></p>
  <form id="processing-settings-form">
    <p id="processing-settings-current" class="library-hint"></p>
    <label><input id="processing-settings-default" type="checkbox"> Use the server default</label>
    <label>Transcript section length (minutes)<input id="processing-settings-chunk" type="number" min="1" max="120" step="any" required></label>
    <p id="processing-settings-hint" class="library-hint">Shorter sections reduce the chance of a truncated response, but make more model requests. Cached transcription and visual analysis are reused.</p>
    <p id="processing-settings-error" class="job-error" role="alert" hidden></p>
    <div class="job-actions">
      <button id="processing-settings-save" type="submit" disabled>Save and retry</button>
      <button id="processing-settings-reload" type="button" hidden>Reload latest settings</button>
      <button id="processing-settings-close" type="button">Cancel</button>
    </div>
  </form>
</dialog>"""
    return _page("Task queue", body, depth=0, scripts=(("queue.js", QUEUE_SCRIPT),))


def render_workers_page() -> str:
    body = """<header class="top">
  <div class="crumbs"><a href="index.html">All courses</a> / <a href="library.html">Manage library</a> / <a href="queue.html">Task queue</a> / <a href="workers.html">Workers</a></div>
  <h1>Processing workers</h1>
  <div class="meta">Use your other computers to speed up processing. Your library stays on the server.</div>
</header>
<main class="wrap">
  <div class="worker-tools">
    <button id="worker-connect" type="button" disabled>Connect a computer</button>
    <button id="worker-refresh" type="button">Refresh</button>
    <label for="worker-filter">Show <select id="worker-filter"><option value="current">Current computers</option><option value="connected">Connected computers</option><option value="archived">Archived computers</option><option value="all">All computers including archived</option></select></label>
  </div>
  <p id="worker-summary" class="queue-summary" role="status">Loading worker status…</p>
  <p id="worker-notice" class="queue-notice" role="status" aria-live="polite"></p>
  <section id="worker-pairing" class="summary" aria-label="Connect a processing helper" hidden></section>
  <p id="worker-empty" class="summary" hidden>No helpers paired yet. The server continues processing locally. Connect a computer whenever you want extra processing capacity.</p>
  <ul id="worker-list" aria-label="Processing computers"></ul>
  <p class="worker-subtle">Contributions count accepted tasks once. A lesson can use several computers. Interrupted attempts are listed separately; processing time is not an estimate of time saved.</p>
  <noscript>Enable JavaScript to view workers and manage connections.</noscript>
</main>"""
    return _page("Processing workers", body, depth=0, scripts=(("workers.js", WORKERS_SCRIPT),))


def render_upload_page() -> str:
    body = f"""<header class="top">
  <div class="crumbs"><a href="index.html">All courses</a> / <a href="library.html">Manage library</a> / <a href="queue.html">Task queue</a> / <a href="workers.html">Workers</a></div>
  <h1>Add videos</h1>
  <div class="meta">Upload source videos to a course. Organize titles and lesson order in <a href="library.html">Library</a>.</div>
</header>
<div class="wrap">
  {_storage_panel()}
  <div class="upload">
    <label for="course">Course</label>
    <input type="text" id="course" list="courses" autocomplete="off" spellcheck="false"
           placeholder="cgboost_launch_pad_2">
    <datalist id="courses"></datalist>
    <p class="hint">Pick an existing course to add lessons to it, or type a new name to start one.</p>
    <div class="drop" id="drop">
      <input type="file" id="picker" accept="video/*" multiple hidden>
      <button type="button" id="choose">Choose videos</button>
      <p class="hint">or drop them here</p>
    </div>
    <ul class="queue" id="queue"></ul>
    <p class="message" id="message" hidden></p>
  </div>
  <section class="library" id="library" hidden>
    <h2>Uploaded originals</h2>
    <p class="hint">Reclaim space removes duplicate uploads after processing finishes. Each saved video is verified first; lessons, playback, and reprocessing stay available. Delete removes the lesson from the library. Saved videos and previous versions are retained.</p>
    <button type="button" id="source-refresh">Refresh originals</button>
    <div id="library-list"></div>
  </section>
</div>"""
    return _page("Videos", body, depth=0, scripts=(("upload.js", UPLOAD_SCRIPT),))


def render_lesson_markdown(lesson: dict) -> str:
    out = [f"# {lesson_title(lesson)}", ""]
    if lesson_description(lesson):
        out += [lesson_description(lesson), ""]
    if not lesson["steps"]:
        out += [f'[Watch the original video](../{lesson["video_href"]})' if lesson.get("video_href")
                else 'Video lesson; the original video is not included in this export.', ""]
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
            out += ["", f"[{clip['seconds']:.0f}s clip from {hms(clip['time'])}](../{clip['src']})"]
        for shot in step.get("frames") or ([{"time": step["start"], "src": step["frame"]}] if step.get("frame") else []):
            out += ["", f"![step {step['index']} at {hms(shot['time'])}](../{shot['src']})"]
        out += [""]
    return "\n".join(out) + "\n"


def write_assets(site_dir: Path) -> None:
    assets = site_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    atomic_write(assets / "style.css", STYLE)
    atomic_write(assets / "app.js", SCRIPT)
    atomic_write(assets / "status.js", STATUS_SCRIPT)
    atomic_write(assets / "upload.js", UPLOAD_SCRIPT)
    atomic_write(assets / "queue.js", QUEUE_SCRIPT)
    atomic_write(assets / "library.js", LIBRARY_SCRIPT)
    atomic_write(assets / "workers.js", WORKERS_SCRIPT)
    atomic_write(assets / "course.js", COURSE_SCRIPT)
    atomic_write(assets / "navigation.js", NAVIGATION_SCRIPT)


def write_placeholder(site_dir: Path, note: str) -> None:
    """Give the web server something to serve before the first build lands.

    An empty root is worse than it sounds: nginx answers a directory with no
    index file and no autoindex with 403, which reads as a permissions problem
    rather than as "nothing built yet". The first real build overwrites this.
    """
    site_dir.mkdir(parents=True, exist_ok=True)
    write_assets(site_dir)
    atomic_write(site_dir / "index.html", render_root_index([], note=note))
    atomic_write(site_dir / "upload.html", render_upload_page())
    atomic_write(site_dir / "queue.html", render_queue_page())
    atomic_write(site_dir / "library.html", render_library_page())
    atomic_write(site_dir / "workers.html", render_workers_page())


def _prune_course(course_dir: Path, lessons: set[str]) -> int:
    removed = 0
    for page in course_dir.glob("*.html"):
        if page.name != "index.html" and page.stem not in lessons:
            page.unlink()
            removed += 1
    md_dir = course_dir / "md"
    if md_dir.is_dir():
        for doc in md_dir.glob("*.md"):
            if doc.stem not in lessons:
                doc.unlink()
                removed += 1
    for kind in ("frames", "clips"):
        parent = course_dir / kind
        if parent.is_dir():
            for sub in parent.iterdir():
                if sub.is_dir() and sub.name not in lessons:
                    shutil.rmtree(sub)
                    removed += 1
    videos = course_dir / "videos"
    if videos.is_dir():
        for item in videos.iterdir():
            if item.stem not in lessons:
                item.unlink()
                removed += 1
    return removed


def prune_site(site_dir: Path, keep: dict[str, set[str]]) -> int:
    """Remove output for courses and lessons the library no longer has.

    `keep` is what the build *looked at*, not what it managed to render: a
    lesson that failed this time keeps the page it had last time rather than
    disappearing from the site over a transient error.

    Only ever called for a build that covered the whole library. A build over
    one course knows nothing about the others and must not tidy them away.
    """
    removed = 0
    for entry in sorted(site_dir.iterdir()):
        if entry.is_file() or entry.name.startswith(".") or entry.name in {"assets", "_revisions", "_sources"}:
            continue
        if entry.name not in keep:
            shutil.rmtree(entry)
            removed += 1
            continue
        removed += _prune_course(entry, keep[entry.name])
    return removed


def write_site(
    site_dir: Path,
    courses: list[dict],
    *,
    write_markdown: bool = True,
    keep: dict[str, set[str]] | None = None,
) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    write_assets(site_dir)

    for course in courses:
        course_dir = site_dir / course["slug"]
        course_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(course_dir / "index.html", render_course_page(course))
        for lesson in course["lessons"]:
            atomic_write(course_dir / f"{lesson['slug']}.html", render_lesson_page(lesson, course))
            if write_markdown:
                md_dir = course_dir / "md"
                md_dir.mkdir(parents=True, exist_ok=True)
                atomic_write(md_dir / f"{lesson['slug']}.md", render_lesson_markdown(lesson))

    if keep is not None:
        # Whatever was just written stays, whatever the caller said: a bug in
        # the bookkeeping should not be able to delete this build's own output.
        safe = dict(keep)
        for course in courses:
            safe.setdefault(course["slug"], set())
            safe[course["slug"]] |= {lesson["slug"] for lesson in course["lessons"]}
        gone = prune_site(site_dir, safe)
        if gone:
            log(f"removed {gone} leftover path(s) from an earlier library layout")

    # The root index goes last. Until the pages it links to are on disk, a
    # reader who follows one gets a 404.
    atomic_write(site_dir / "library.html", render_library_page())
    atomic_write(site_dir / "queue.html", render_queue_page())
    atomic_write(site_dir / "index.html", render_root_index(courses))
    atomic_write(site_dir / "workers.html", render_workers_page())
    atomic_write(site_dir / "upload.html", render_upload_page())
    exported_courses = [dict(course, lessons=[
        dict(lesson, numbering=lesson_numbering(lesson), chapter=lesson_chapter(lesson)) for lesson in course["lessons"]
    ]) for course in courses]
    atomic_write(site_dir / "site.json", json.dumps(exported_courses, indent=2))
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
