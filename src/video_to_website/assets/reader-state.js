/* SQLite is authoritative on the service. Static exports use localStorage.
 * A server outage never silently turns into unsynchronized local edits. */
var V2WReaderState = (function () {
  'use strict';
  var defaults = {rate: 1, loop: true, player_collapsed: true, clip_autoplay: true};
  var rates = [0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3];
  var listeners = [], snapshot = null, mode = 'connecting', busy = false, polling = false;
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
  function notify() { listeners.forEach(function (fn) { fn(); }); }
  function read(key) {
    return snapshot ? (snapshot.states[key] || {}).state || {} : parse(local(key));
  }
  function preferences() { return snapshot ? snapshot.preferences.values : localPreferences(); }
  function revision(key) { return snapshot && snapshot.states[key] ? snapshot.states[key].revision : 0; }
  function status() {
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
      accept(value);
    } catch (e) {
      if ((e.status === 404 || e.status === 405) && !local('v2w:reader:server')) mode = 'local';
      else { mode = 'offline'; problem = e.message || 'Cannot reach reading sync.'; }
    }
    notify();
  }
  async function refresh() {
    if (busy || polling || mode === 'local' || mode === 'connecting') return;
    polling = true;
    try {
      if (!snapshot) await connect();
      else { accept(await request()); notify(); }
    } catch (e) { mode = 'offline'; problem = 'Cannot reach reading sync. Reconnect before editing.'; notify(); }
    finally { polling = false; notify(); }
  }
  async function edit(entries, preferenceValues, expectedPreferences) {
    await ready;
    if (busy || polling) throw new Error('Sync is busy. Try again in a moment.');
    if (mode !== 'server' && mode !== 'local') throw new Error('Reconnect to the server before editing progress or settings.');
    busy = true; problem = ''; notify();
    try {
      if (mode === 'server') {
        accept(await request(preferenceValues ? {action: 'preferences', values: preferenceValues,
          revision: expectedPreferences === undefined ? snapshot.preferences.revision : expectedPreferences}
          : {action: 'patch', entries: entries}));
      } else if (preferenceValues) {
        var prefs = Object.assign({}, localPreferences(), preferenceValues);
        localStorage.setItem('v2w:reader:preferences', JSON.stringify(prefs));
        localStorage.setItem('v2w:rate', String(prefs.rate));
        localStorage.setItem('v2w:loop', prefs.loop ? '1' : '0');
      } else {
        // Best-effort rollback makes storage quota failures visible and avoids
        // presenting a partially saved static-export bulk edit as successful.
        var previous = entries.map(function (entry) { return {key: entry.key, raw: localStorage.getItem(entry.key)}; });
        try {
          entries.forEach(function (entry) { localStorage.setItem(entry.key, JSON.stringify(Object.assign({}, read(entry.key), entry.changes))); });
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
  window.addEventListener('focus', refresh);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
  ready = connect();
  if (api) setInterval(function () { if (!document.hidden) refresh(); }, 10000);
  return {read: read, revision: revision, preferences: preferences, rates: rates, defaults: defaults,
    preferenceRevision: function () { return snapshot ? snapshot.preferences.revision : 0; },
    edit: edit, status: status, ready: ready, refresh: refresh,
    writable: function () { return !busy && !polling && (mode === 'server' || mode === 'local'); },
    subscribe: function (fn) { listeners.push(fn); }};
})();
