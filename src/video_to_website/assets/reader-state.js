/* SQLite is authoritative on the service. Static exports use localStorage.
 * A server outage never silently turns into unsynchronized local edits. */
var V2WReaderState = (function () {
  'use strict';
  var defaults = {rate: 1, loop: true, player_collapsed: true, clip_autoplay: true, hide_completed: false};
  var rates = [0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3];
  var listeners = [], snapshot = null, mode = 'connecting', busy = false, polling = false;
  var saveQueue = new Map(), saving = false, saveError = '';
  var problem = '', ready, api = document.body.dataset.readingApi;
  function parse(raw) {
    try { var value = JSON.parse(raw); return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; }
    catch (e) { return {}; }
  }
  function local(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function localPreferences() {
    var values = Object.assign({}, defaults), saved = parse(local('v2w:reader:preferences'));
    Object.keys(defaults).forEach(function (key) {
      if (key === 'rate' ? rates.includes(saved[key]) : typeof saved[key] === 'boolean') values[key] = saved[key];
    });
    var rate = Number(local('v2w:rate'));
    if (rates.includes(rate)) values.rate = rate;
    if (local('v2w:loop') !== null) values.loop = local('v2w:loop') !== '0';
    return values;
  }
  function notify() {
    listeners.forEach(function (fn) { fn(); });
    if (saveQueue.size && !saveError) Promise.resolve().then(drainQueue);
  }
  function confirmed(key) {
    return snapshot ? (snapshot.states[key] || {}).state || {} : parse(local(key));
  }
  function legacyView(scope) {
    var library = scope === 'library';
    var saved = parse(local(library ? 'v2w:library:view' : 'v2w:course:' + scope.slice(7) + ':navigation'));
    var value = {}, choices = library ? {grouping: ['chapters', 'flat'], density: ['detailed', 'compact']} :
      {sort: ['saved', 'number', 'title', 'shortest'], view: ['chapters', 'flat'], density: ['detailed', 'compact'], readerDensity: ['detailed', 'compact']};
    Object.keys(choices).forEach(function (key) { if (choices[key].includes(saved[key])) value[key] = saved[key]; });
    (library ? [['expanded', 'expanded:'], ['chapters', 'chapter:']] : [['opened', 'opened:']]).forEach(function (entry) {
      var map = saved[entry[0]];
      if (!map || typeof map !== 'object' || Array.isArray(map)) return;
      Object.keys(map).forEach(function (key) { if (typeof map[key] === 'boolean') value[entry[1] + key] = map[key]; });
    });
    return value;
  }
  function localView(scope) {
    var raw = local('v2w:view:' + scope);
    return raw === null ? legacyView(scope) : parse(raw);
  }
  function confirmedView(scope) { return snapshot ? ((snapshot.views || {})[scope] || {}).values || {} : localView(scope); }
  function confirmedPreferences() { return snapshot ? Object.assign({}, defaults, snapshot.preferences.values) : localPreferences(); }
  function overlay(kind, scope, value) {
    var pending = saveQueue.get(kind + ':' + scope);
    if (!pending || saveError) return value;
    value = Object.assign({}, value);
    Object.keys(pending.changes).forEach(function (id) { value[id] = pending.changes[id].value; });
    return value;
  }
  function read(key) { return overlay('steps', key, confirmed(key)); }
  function view(scope) { return overlay('view', scope, confirmedView(scope)); }
  function preferences() { return overlay('preferences', 'global', confirmedPreferences()); }
  function savedRevision(kind, scope) {
    if (kind === 'steps') return revision(scope);
    if (kind === 'preferences') return snapshot ? snapshot.preferences.revision : 0;
    return snapshot && (snapshot.views || {})[scope] ? snapshot.views[scope].revision : 0;
  }
  function pendingChanges() {
    var count = 0;
    saveQueue.forEach(function (entry) { count += Object.keys(entry.changes).length; });
    return {count: count, error: saveError, busy: busy || polling || saving};
  }
  function canQueueStep() { return !saveError && (mode === 'server' || mode === 'local'); }
  function queueStep(key, id, value) {
    return queueChanges('steps', key, {[id]: value});
  }
  function queueChanges(kind, scope, values) {
    if (!canQueueStep()) return false;
    var current = kind === 'steps' ? read(scope) : kind === 'view' ? view(scope) : preferences();
    var key = kind + ':' + scope;
    Object.keys(values).forEach(function (id) {
      var before = kind === 'steps' ? !!current[id] : current[id];
      if (before === values[id]) return;
      if (!saveQueue.has(key)) saveQueue.set(key, {kind: kind, scope: scope, revision: savedRevision(kind, scope), changes: {}});
      // A new object distinguishes a newer change from an in-flight save.
      saveQueue.get(key).changes[id] = {value: values[id]};
    });
    notify();
    return true;
  }
  async function drainQueue() {
    if (saving || busy || polling || !saveQueue.size || saveError) return;
    if (!canQueueStep()) {
      saveError = 'Reconnect to the server before retrying these changes.';
      notify(); return;
    }
    saving = true;
    var [key, entry] = saveQueue.entries().next().value;
    var batch = Object.assign({}, entry.changes), changes = {};
    Object.keys(batch).forEach(function (id) { changes[id] = batch[id].value; });
    try {
      var body = entry.kind === 'steps'
        ? {action: 'patch', entries: [{key: entry.scope, revision: entry.revision, changes: changes}]}
        : {action: entry.kind, scope: entry.scope, revision: entry.revision, values: changes};
      await commit(body, true);
      Object.keys(batch).forEach(function (id) { if (entry.changes[id] === batch[id]) delete entry.changes[id]; });
      // Only our own successful save advances the revision of queued edits.
      // An unrelated device edit must still trigger the normal conflict check.
      entry.revision = savedRevision(entry.kind, entry.scope);
      if (!Object.keys(entry.changes).length) saveQueue.delete(key);
    } catch (e) {
      saveError = e.message || 'The save could not be confirmed.';
    } finally { saving = false; notify(); }
  }
  async function retryChanges() {
    if (!saveError || busy || polling || saving) return;
    await refresh();
    if (mode !== 'server' && mode !== 'local') return;
    saveQueue.forEach(function (entry) { entry.revision = savedRevision(entry.kind, entry.scope); });
    saveError = ''; notify();
  }
  function discardChanges() {
    if (!saveError || busy || polling || saving) return;
    saveQueue.clear(); saveError = ''; problem = ''; notify();
  }
  function revision(key) { return snapshot && snapshot.states[key] ? snapshot.states[key].revision : 0; }
  function status() {
    if (saveError) return 'There are unsaved changes. ' + saveError;
    if (saveQueue.size) return 'Saving changes…';
    if (busy) return 'Saving…';
    if (problem) return problem;
    return {connecting: 'Connecting to reading sync…', server: 'Synced with server',
      local: 'Saved in this browser only · static export', offline: 'Cannot reach reading sync. Reconnect before editing.'}[mode];
  }
  async function request(body) {
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 10000);
    try {
      var response = await fetch(api, {method: body ? 'POST' : 'GET', cache: 'no-store', signal: controller.signal,
        headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
      var value = response.headers.get('Content-Type')?.includes('application/json') ? await response.json() : {};
      if (!response.ok) { var error = new Error(value.error || 'Reading sync is unavailable. Try again.'); error.status = response.status; throw error; }
      if (value.version !== 1 || !value.states || !value.preferences || !value.lessons) throw new Error('Unexpected reading sync response. Reload after updating the server.');
      return value;
    } finally { clearTimeout(timeout); }
  }
  function accept(value) {
    snapshot = value; mode = 'server'; problem = '';
    // Cache for display during a later outage, and for older open reader pages.
    try {
      localStorage.setItem('v2w:reader:server', '1');
      Object.keys(value.lessons).forEach(function (key) {
        localStorage.setItem(key, JSON.stringify((value.states[key] || {}).state || {}));
      });
      localStorage.setItem('v2w:reader:preferences', JSON.stringify(value.preferences.values));
      localStorage.setItem('v2w:rate', String(value.preferences.values.rate));
      localStorage.setItem('v2w:loop', value.preferences.values.loop ? '1' : '0');
      if (value.views && Array.isArray(value.courses)) ['library'].concat(value.courses.map(function (id) { return 'course:' + id; })).forEach(function (scope) {
        localStorage.setItem('v2w:view:' + scope, JSON.stringify((value.views[scope] || {}).values || {}));
      });
    } catch (e) { /* Server saves do not depend on browser storage capacity. */ }
  }
  async function connect() {
    if (!api || location.protocol === 'file:') { mode = 'local'; notify(); return; }
    try {
      var value = await request();
      // Capture legacy state before accepting the authoritative snapshot.
      var entries = [];
      Object.keys(value.lessons).forEach(function (key) {
        if (value.states[key]) return;
        var saved = parse(local(key)), changes = {};
        value.lessons[key].forEach(function (id) { if (Object.hasOwn(saved, id)) changes[id] = !!saved[id]; });
        if (Object.keys(changes).length) entries.push({key: key, changes: changes});
      });
      if (entries.length) {
        // Each import only fills missing namespaces, even if two browsers race.
        for (var start = 0; start < entries.length; start += 2000) {
          value = await request({action: 'import', entries: entries.slice(start, start + 2000)});
        }
      }
      if (!value.preferences.revision && (local('v2w:rate') !== null || local('v2w:loop') !== null || local('v2w:reader:preferences') !== null)) {
        value = await request({action: 'import-preferences', values: localPreferences()});
      }
      if (value.views && Array.isArray(value.courses)) {
        var views = ['library'].concat(value.courses.map(function (id) { return 'course:' + id; })).filter(function (scope) {
          return !value.views[scope];
        }).map(function (scope) { return {scope: scope, values: localView(scope)}; }).filter(function (entry) { return Object.keys(entry.values).length; });
        for (var offset = 0; offset < views.length; offset += 2000) value = await request({action: 'import-views', entries: views.slice(offset, offset + 2000)});
      }
      accept(value);
    } catch (e) {
      if ((e.status === 404 || e.status === 405) && !local('v2w:reader:server')) mode = 'local';
      else { mode = 'offline'; problem = e.message || 'Cannot reach reading sync.'; }
    }
    notify();
  }
  async function refresh() {
    if (busy || polling || mode === 'local' || mode === 'connecting' || (saveQueue.size && !saveError)) return;
    polling = true;
    try {
      if (!snapshot) await connect();
      else { accept(await request()); notify(); }
    } catch (e) { mode = 'offline'; problem = 'Cannot reach reading sync. Reconnect before editing.'; notify(); }
    finally { polling = false; notify(); }
  }
  function edit(entries, preferenceValues, expectedPreferences) {
    return commit(preferenceValues ? {action: 'preferences', values: preferenceValues,
      revision: expectedPreferences === undefined ? savedRevision('preferences', 'global') : expectedPreferences}
      : {action: 'patch', entries: entries});
  }
  async function commit(body, fromQueue) {
    await ready;
    if (busy || polling || (saveQueue.size && !fromQueue)) throw new Error('Changes are still saving. Try again in a moment.');
    if (mode !== 'server' && mode !== 'local') throw new Error('Reconnect to the server before editing progress or settings.');
    busy = true; problem = ''; notify();
    try {
      if (mode === 'server') {
        accept(await request(body));
      } else if (body.action === 'preferences') {
        var prefs = Object.assign({}, localPreferences(), body.values);
        localStorage.setItem('v2w:reader:preferences', JSON.stringify(prefs));
        localStorage.setItem('v2w:rate', String(prefs.rate));
        localStorage.setItem('v2w:loop', prefs.loop ? '1' : '0');
      } else if (body.action === 'view') {
        localStorage.setItem('v2w:view:' + body.scope, JSON.stringify(Object.assign({}, localView(body.scope), body.values)));
      } else {
        // Best-effort rollback makes storage quota failures visible and avoids
        // presenting a partially saved static-export bulk edit as successful.
        var previous = body.entries.map(function (entry) { return {key: entry.key, raw: localStorage.getItem(entry.key)}; });
        try {
          body.entries.forEach(function (entry) { localStorage.setItem(entry.key, JSON.stringify(Object.assign({}, confirmed(entry.key), entry.changes))); });
        } catch (e) {
          previous.forEach(function (entry) { try {
            if (entry.raw === null) localStorage.removeItem(entry.key); else localStorage.setItem(entry.key, entry.raw);
          } catch (ignored) {} });
          throw new Error('Progress could not be saved. Browser storage may be blocked or full.');
        }
      }
    } catch (e) {
      if (mode === 'server') {
        // A lost response can still mean a committed transaction. Refetch before
        // offering another edit; never retry an old full-lesson write blindly.
        try { accept(await request()); } catch (ignored) { mode = 'offline'; }
      }
      problem = e.message || 'The change could not be saved.';
      throw e;
    } finally { busy = false; notify(); }
  }
  window.addEventListener('storage', function (e) {
    if (e.key === null || e.key.startsWith('v2w:')) { if (mode === 'local') notify(); else refresh(); }
  });
  window.addEventListener('pageshow', refresh);
  window.addEventListener('beforeunload', function (event) {
    if (!saveQueue.size) return;
    event.preventDefault(); event.returnValue = '';
  });
  window.addEventListener('focus', refresh);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
  ready = connect();
  if (api) setInterval(function () { if (!document.hidden) refresh(); }, 10000);
  return {read: read, revision: revision, preferences: preferences, rates: rates, defaults: defaults,
    preferenceRevision: function () { return snapshot ? snapshot.preferences.revision : 0; },
    edit: edit, status: status, ready: ready, refresh: refresh,
    writable: function () { return !busy && !polling && !saveQueue.size && (mode === 'server' || mode === 'local'); },
    queueStep: queueStep, canQueueStep: canQueueStep, canQueue: canQueueStep,
    queuePreferences: function (values) { return queueChanges('preferences', 'global', values); },
    queueView: function (scope, values) { return queueChanges('view', scope, values); }, view: view,
    pendingChanges: pendingChanges, retryChanges: retryChanges, discardChanges: discardChanges,
    pendingSteps: pendingChanges, retrySteps: retryChanges, discardSteps: discardChanges,
    subscribe: function (fn) { listeners.push(fn); }};
})();
