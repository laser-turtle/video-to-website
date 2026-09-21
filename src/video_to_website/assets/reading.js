/* Progress calculations and controls shared by readers and overviews. */
var V2WReading = (function () {
  'use strict';
  function read(lesson) { return V2WReaderState.read(lesson.key); }
  function units(lesson) { return lesson.steps.length ? lesson.steps : ['__lessonComplete']; }
  function stats(lesson) {
    var state = read(lesson), ids = units(lesson);
    var done = ids.filter(function (id) { return !!state[id]; }).length;
    return {done: done, total: ids.length, fraction: done / ids.length,
      status: done === ids.length ? 'complete' : done ? 'in-progress' : 'not-started'};
  }
  function aggregate(lessons) {
    var states = lessons.map(stats);
    return {done: states.filter(function (s) { return s.status === 'complete'; }).length,
      total: lessons.length, fraction: states.length ? states.reduce(function (n, s) { return n + s.fraction; }, 0) / states.length : 0};
  }
  function percent(fraction) { return fraction === 1 ? 100 : Math.min(99, Math.round(fraction * 100)); }
  function label(lesson) {
    var s = stats(lesson);
    if (!lesson.steps.length) return s.status === 'complete' ? 'Complete' : 'Not started';
    return (s.status === 'complete' ? 'Complete · ' : s.done ? '' : 'Not started · ') +
      s.done + ' of ' + s.total + ' steps · ' + percent(s.fraction) + '%';
  }
  function summary(lessons) {
    var s = aggregate(lessons);
    return s.total ? s.done + ' of ' + s.total + ' lessons complete · ' + percent(s.fraction) + '%' : 'No published lessons yet';
  }
  async function setStep(lesson, id, value) {
    if (!lesson.steps.includes(id)) return;
    await V2WReaderState.edit([{key: lesson.key, revision: V2WReaderState.revision(lesson.key), changes: {[id]: value}}]);
  }
  async function mark(lessons, value) {
    var result = {changes: [], failed: 0, skipped: 0};
    var entries = lessons.map(function (lesson) {
      var changes = {};
      units(lesson).forEach(function (id) { changes[id] = value; });
      result.changes.push({lesson: lesson, before: Object.assign({}, read(lesson)), after: changes});
      return {key: lesson.key, revision: V2WReaderState.revision(lesson.key), changes: changes};
    });
    if (entries.length) await V2WReaderState.edit(entries);
    result.changes.forEach(function (change) { change.revision = V2WReaderState.revision(change.lesson.key); });
    return result;
  }
  async function undo(changes) {
    var result = {changes: [], failed: 0, skipped: 0};
    var entries = [];
    changes.forEach(function (change) {
      var current = read(change.lesson), restore = {};
      var ids = units(change.lesson);
      if (V2WReaderState.revision(change.lesson.key) !== change.revision ||
          ids.some(function (id) { return !!current[id] !== !!change.after[id]; })) { result.skipped++; return; }
      ids.forEach(function (id) { restore[id] = !!change.before[id]; });
      entries.push({key: change.lesson.key, revision: change.revision, changes: restore});
      result.changes.push(change);
    });
    if (entries.length) await V2WReaderState.edit(entries);
    return result;
  }
  function report(result, verb) {
    return verb + ' ' + result.changes.length + ' lesson' + (result.changes.length === 1 ? '.' : 's.') +
      (result.failed ? ' ' + result.failed + ' could not be saved. Browser storage may be blocked or full.' : '') +
      (result.skipped ? ' ' + result.skipped + ' left unchanged because progress was edited since this action.' : '');
  }
  function paint(node, lessons) {
    var s = aggregate(lessons);
    node.querySelector('[data-reading-text]').textContent = summary(lessons);
    var bar = node.querySelector('progress');
    bar.value = percent(s.fraction);
    node.hidden = false;
  }
  return {read: read, stats: stats, aggregate: aggregate, label: label, summary: summary,
    percent: percent, setStep: setStep, mark: mark, undo: undo, report: report, paint: paint,
    subscribe: V2WReaderState.subscribe};
})();

(function () {
  'use strict';
  function syncStatus() {
    document.querySelectorAll('[data-reading-sync]').forEach(function (node) { node.textContent = V2WReaderState.status(); });
  }
  V2WReading.subscribe(syncStatus); syncStatus();
  document.querySelectorAll('[data-reading-summary]').forEach(function (node) {
    var lessons = JSON.parse(node.dataset.readingSummary);
    function refresh() { V2WReading.paint(node, lessons); }
    V2WReading.subscribe(refresh);
    refresh();
  });
  document.querySelectorAll('[data-reading-control]').forEach(function (node) {
    var lesson = JSON.parse(node.dataset.readingControl);
    var button = node.querySelector('[data-reading-toggle]');
    var undo = node.querySelector('[data-reading-undo]');
    var message = node.querySelector('[data-reading-message]');
    var changes = [];
    function refresh() {
      var s = V2WReading.stats(lesson);
      node.querySelector('[data-reading-text]').textContent = V2WReading.label(lesson);
      node.querySelector('progress').value = V2WReading.percent(s.fraction);
      button.textContent = s.status === 'complete' ? 'Reset lesson progress' : 'Mark lesson complete';
      button.disabled = undo.disabled = !V2WReaderState.writable();
    }
    button.addEventListener('click', async function () {
      try {
        var result = await V2WReading.mark([lesson], V2WReading.stats(lesson).status !== 'complete');
        changes = result.changes;
        undo.hidden = !changes.length;
        message.textContent = V2WReading.report(result, 'Updated');
      } catch (e) { message.textContent = e.message; }
    });
    undo.addEventListener('click', async function () {
      try {
        message.textContent = V2WReading.report(await V2WReading.undo(changes), 'Restored');
        changes = []; undo.hidden = true;
      } catch (e) { message.textContent = e.message; }
    });
    V2WReading.subscribe(refresh);
    node.hidden = false;
    refresh();
  });
})();
