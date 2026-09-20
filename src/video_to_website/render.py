"""Static site generation: one page per lesson, plus course and root indexes."""

from __future__ import annotations

import hashlib
import html
import json
import shutil
from pathlib import Path

from .util import atomic_write, hms, human_duration, log

QUEUE_SCRIPT = (Path(__file__).parent / "assets" / "queue.js").read_text(encoding="utf-8")

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
.lib-course { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 14px; margin-bottom: 12px; }
.lib-course > h3 { margin: 0 0 4px; font-size: 15px; }
.lib-course > .hint { font-size: 12.5px; color: var(--muted); margin: 0 0 10px; }
.lib-row { display: flex; align-items: center; gap: 10px; padding: 7px 0; border-top: 1px solid var(--line); }
.lib-row .n { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 12.5px; min-width: 1.6em; text-align: right; flex: none; }
.lib-row .name { flex: 1; font-size: 14px; word-break: break-word; }
.lib-row .size { font-size: 12.5px; color: var(--muted); flex: none; }
.lib-row input {
  width: 100%; font: inherit; font-size: 14px; color: inherit;
  background: var(--bg); border: 1px solid var(--accent); border-radius: 6px; padding: 5px 8px;
}
.lib-row button {
  font: inherit; font-size: 12.5px; cursor: pointer; flex: none;
  color: var(--accent); background: none; border: 1px solid var(--line); border-radius: 6px; padding: 3px 9px;
}
.lib-row button:hover { border-color: var(--accent); }
.lib-row button.danger { color: #c0392b; }
.lib-row.busy { opacity: .5; }
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

  var match = /[#&]t=(\\d+(?:\\.\\d+)?)/.exec(location.hash);
  if (match && video) {
    video.addEventListener('loadedmetadata', function () {
      video.currentTime = parseFloat(match[1]);
    }, { once: true });
  }
})();
"""


STATUS_SCRIPT = """\
(function () {
  'use strict';

  var panel = document.getElementById('build-status');
  var list = document.getElementById('build-list');
  var count = document.getElementById('build-count');
  if (!panel || !list) { return; }

  var POLL_MS = 3000;
  var QUEUE_SHOWN = 6;
  // The 'built' stamp as of the first poll. When the builder finishes a course
  // it moves, and the page reloads itself so the new course actually appears.
  var builtAtLoad = null;

  function elapsed(seconds) {
    var s = Math.round(seconds || 0);
    if (s < 60) { return s + 's'; }
    var m = Math.floor(s / 60);
    if (m < 60) { return m + 'm ' + (s % 60) + 's'; }
    return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
  }

  function row(entry, updated) {
    var li = document.createElement('li');
    li.className = entry.state;

    var title = document.createElement('div');
    title.className = 't';
    var dot = document.createElement('span');
    dot.className = 'dot';
    title.appendChild(dot);
    title.appendChild(document.createTextNode(entry.title));
    li.appendChild(title);

    var parts = [entry.course, entry.label];
    if (entry.queue_position) { parts.push('#' + entry.queue_position + ' in queue'); }
    if (entry.state === 'working' && updated && Date.now() / 1000 - updated > 120) {
      parts.push('No recent update from the worker');
    }
    if (entry.state === 'working' && entry.elapsed) { parts.push(elapsed(entry.elapsed)); }
    var sub = document.createElement('div');
    sub.className = 's';
    sub.textContent = parts.filter(Boolean).join(' \u00b7 ');
    li.appendChild(sub);

    if (entry.state === 'working' && entry.steps) {
      var bar = document.createElement('div');
      bar.className = 'bar';
      var fill = document.createElement('i');
      // A stage is only finished once the next one starts, so a stage that is
      // under way counts as half of one. Better than a bar that sits still
      // through the minutes whisper takes.
      fill.style.width = (100 * Math.max(0, entry.step - 0.5) / entry.steps) + '%';
      bar.appendChild(fill);
      li.appendChild(bar);
    }
    if (/^[a-f0-9]{32}$/.test(entry.id || '') &&
        ['working', 'queued', 'failed', 'cancelled'].indexOf(entry.state) >= 0) {
      var action = entry.state === 'failed' || entry.state === 'cancelled' ? 'retry' : 'cancel';
      var control = document.createElement('button');
      control.type = 'button';
      control.textContent = action === 'retry' ? 'Retry' : 'Cancel';
      control.addEventListener('click', function () {
        control.disabled = true;
        fetch('api/lessons/' + entry.id + '/' + action, { method: 'POST' }).then(function (res) {
          if (!res.ok) { throw new Error('Request failed (' + res.status + ')'); }
          poll();
        }).catch(function (err) {
          control.disabled = false;
          control.textContent = err.message + ' — try again';
        });
      });
      li.appendChild(control);
    }
    return li;
  }

  function errorRow(data) {
    var li = document.createElement('li');
    li.className = 'failed';
    var title = document.createElement('div');
    title.className = 't';
    var dot = document.createElement('span');
    dot.className = 'dot';
    title.appendChild(dot);
    title.appendChild(document.createTextNode('Build stopped'));
    var sub = document.createElement('div');
    sub.className = 's';
    sub.textContent = data.retry_in
      ? data.error + ' \u00b7 retrying in ' + elapsed(data.retry_in)
      : data.error;
    li.appendChild(title);
    li.appendChild(sub);
    return li;
  }

  function render(data) {
    var videos = data.videos || [];
    var active = videos.filter(function (v) { return v.state === 'working'; });
    var queued = videos.filter(function (v) { return v.state === 'queued'; });
    var failed = videos.filter(function (v) { return v.state === 'failed' || v.state === 'cancelled'; });

    var shown = active.concat(failed, queued.slice(0, QUEUE_SHOWN));
    if (!shown.length && !data.error) { panel.hidden = true; return; }

    list.textContent = '';
    // A build that stopped part way is the one thing worth saying out loud:
    // the pages on disk are last build's, and nothing else on the page says so.
    if (data.error) { list.appendChild(errorRow(data)); }
    shown.forEach(function (entry) { list.appendChild(row(entry, data.updated)); });

    var hidden = queued.length - Math.min(queued.length, QUEUE_SHOWN);
    if (hidden > 0) {
      var more = document.createElement('li');
      more.className = 'more';
      var link = document.createElement('a');
      link.href = 'queue.html';
      link.textContent = 'View all ' + queued.length + ' waiting lessons';
      more.appendChild(link);
      list.appendChild(more);
    }
    if (count) {
      count.textContent = [active.length ? active.length + ' running' : '',
        queued.length ? queued.length + ' waiting' : '',
        failed.length ? failed.length + ' needing attention' : ''].filter(Boolean).join(' · ');
    }
    panel.hidden = false;
  }

  function poll() {
    // Cache-busted by hand: this file is small, changes constantly, and is not
    // worth trusting any proxy between here and the builder to revalidate.
    fetch('status.json?t=' + Date.now(), { cache: 'no-store' }).then(function (res) {
      if (!res.ok) { throw new Error(res.status); }
      return res.json();
    }).then(function (data) {
      if (builtAtLoad === null) { builtAtLoad = data.built || 0; }
      else if ((data.built || 0) > builtAtLoad) { location.reload(); return; }
      render(data);
    }).catch(function () {
      // No status file yet, or the builder is mid-write. Try again shortly.
      panel.hidden = true;
    });
  }

  poll();
  setInterval(poll, POLL_MS);
  // Coming back to a tab that was hidden for an hour should not wait for the
  // next tick to catch up.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) { poll(); }
  });
})();
"""


UPLOAD_SCRIPT = """\
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
    if (bytes >= 1073741824) { return (bytes / 1073741824).toFixed(1) + ' GB'; }
    if (bytes >= 1048576) { return Math.round(bytes / 1048576) + ' MB'; }
    return Math.max(1, Math.round(bytes / 1024)) + ' KB';
  }

  var library = document.getElementById('library');
  var libraryList = document.getElementById('library-list');

  function button(text, className, onClick) {
    var el = document.createElement('button');
    el.type = 'button';
    if (className) { el.className = className; }
    el.textContent = text;
    el.addEventListener('click', onClick);
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

  function libRow(courseName, video, index) {
    var row = document.createElement('div');
    row.className = 'lib-row';

    var number = document.createElement('span');
    number.className = 'n';
    number.textContent = index + 1 + '.';
    var name = document.createElement('span');
    name.className = 'name';
    name.textContent = video.name;
    var bytes = document.createElement('span');
    bytes.className = 'size';
    bytes.textContent = size(video.bytes);
    row.appendChild(number);
    row.appendChild(name);
    row.appendChild(bytes);

    function act(promise) {
      row.className = 'lib-row busy';
      promise.then(load).catch(function (err) {
        row.className = 'lib-row';
        say(err.message, true);
      });
    }

    var rename = button('Rename', '', function () {
      var input = document.createElement('input');
      input.type = 'text';
      input.value = video.name;
      name.textContent = '';
      name.appendChild(input);
      rename.remove();
      remove.remove();
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

    // Two steps, because this deletes the source video and there is no undo.
    var remove = button('Delete', 'danger', function () {
      rename.remove();
      remove.remove();
      var label = document.createElement('span');
      label.className = 'size';
      label.textContent = 'Remove source from library?';
      row.appendChild(label);
      row.appendChild(button('Yes, delete', 'danger', function () {
        act(api('DELETE', videoPath(courseName, video.name)));
      }));
      row.appendChild(button('Cancel', '', load));
    });

    row.appendChild(rename);
    row.appendChild(remove);
    return row;
  }

  function renderLibrary(courses) {
    libraryList.textContent = '';
    var any = false;
    courses.forEach(function (course) {
      if (!course.videos.length) { return; }
      any = true;
      var box = document.createElement('div');
      box.className = 'lib-course';
      var heading = document.createElement('h3');
      heading.textContent = course.name;
      var hint = document.createElement('p');
      hint.className = 'hint';
      hint.textContent = 'Lessons build in this order. Rename to change it.';
      box.appendChild(heading);
      box.appendChild(hint);
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
    return fetch('api/library').then(function (res) {
      if (!res.ok) { throw new Error(res.status); }
      return res.json();
    }).then(function (data) {
      var courses = data.courses || [];
      list.textContent = '';
      courses.forEach(function (course) {
        var option = document.createElement('option');
        option.value = course.name;
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
    if (offerReplace) {
      var again = document.createElement('button');
      again.type = 'button';
      again.className = 'again';
      again.textContent = 'Replace it';
      again.addEventListener('click', function () {
        again.remove();
        job.ui.li.className = '';
        job.ui.bar.hidden = false;
        job.ui.status.textContent = size(job.file.size) + ' \u00b7 waiting';
        job.overwrite = true;
        pending.push(job);
        if (!busy) { next(); }
      });
      job.ui.li.appendChild(again);
    }
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
    return _page(
        lesson["title"], body, depth=1,
        lesson_slug=lesson.get("reading_key") or lesson.get("id") or course["slug"] + ":" + lesson["slug"],
        scripts=(("app.js", SCRIPT),)
    )


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
    # Filled in by status.js from status.json, which the builder keeps current.
    # Static markup so the page is not blank for the length of the first poll.
    status = """<section class="building" id="build-status" hidden>
  <h2><a href="queue.html">Task queue</a><span class="count" id="build-count"></span></h2>
  <ul id="build-list"></ul>
</section>"""
    body = f"""<header class="top">
  <div class="crumbs"><a href="upload.html">Manage videos</a> / <a href="queue.html">Task queue</a></div>
  <h1>Courses</h1>
  <div class="meta">{_esc(meta)}</div>
</header>
<div class="wrap">{status}<ul class="cards">{"".join(cards)}</ul></div>"""
    return _page("Courses", body, depth=0, scripts=(("status.js", STATUS_SCRIPT),))


def render_queue_page() -> str:
    body = """<header class="top">
  <div class="crumbs"><a href="index.html">All courses</a> / <a href="upload.html">Manage videos</a></div>
  <h1>Task queue</h1>
  <div class="meta">See what's processing and what's coming next. Cancelling keeps your source video.</div>
</header>
<main class="wrap">
  <section class="building task-queue" aria-label="Processing queue">
    <p id="queue-summary" class="queue-summary" role="status" aria-live="polite">Loading the queue…</p>
    <div class="queue-tools">
      <label for="job-filter">Show
        <select id="job-filter">
          <option value="active">Running and waiting</option>
          <option value="queued">Waiting</option>
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
</main>"""
    return _page("Task queue", body, depth=0, scripts=(("queue.js", QUEUE_SCRIPT),))


def render_upload_page() -> str:
    body = """<header class="top">
  <div class="crumbs"><a href="index.html">All courses</a> / <a href="queue.html">Task queue</a></div>
  <h1>Videos</h1>
  <div class="meta">What the builder works from. Anything changed here rebuilds the site.</div>
</header>
<div class="wrap">
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
    <h2>Library</h2>
    <p class="hint">Deleting removes the library source. Processing snapshots and previous versions are retained.</p>
    <div id="library-list"></div>
  </section>
</div>"""
    return _page("Videos", body, depth=0, scripts=(("upload.js", UPLOAD_SCRIPT),))


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
    atomic_write(site_dir / "queue.html", render_queue_page())
    atomic_write(site_dir / "index.html", render_root_index(courses))
    atomic_write(site_dir / "upload.html", render_upload_page())
    atomic_write(site_dir / "site.json", json.dumps(courses, indent=2))
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
