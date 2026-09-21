(function () {
  'use strict';

  var list = document.getElementById('job-list');
  var summary = document.getElementById('queue-summary');
  var filter = document.getElementById('job-filter');
  var search = document.getElementById('job-search');
  var empty = document.getElementById('queue-empty');
  var notice = document.getElementById('queue-notice');
  var updated = document.getElementById('queue-updated');
  if (!list || !filter || !search) return;

  var data = { videos: [] };
  var canManage = false;
  var loaded = false;
  var polling = false;
  var pollAgain = false;
  var pending = {};
  var errors = {};
  var providerList = document.getElementById('provider-pauses');
  var resuming = {}, resumeErrors = {};
  var settingsDialog = document.getElementById('processing-settings');
  var settingsForm = document.getElementById('processing-settings-form');
  var settingsLesson = document.getElementById('processing-settings-lesson');
  var settingsCurrent = document.getElementById('processing-settings-current');
  var settingsDefault = document.getElementById('processing-settings-default');
  var settingsChunk = document.getElementById('processing-settings-chunk');
  var settingsHint = document.getElementById('processing-settings-hint');
  var settingsError = document.getElementById('processing-settings-error');
  var settingsSave = document.getElementById('processing-settings-save');
  var settingsReload = document.getElementById('processing-settings-reload');
  var settingsClose = document.getElementById('processing-settings-close');
  var settingsEditor = null;
  var params = new URLSearchParams(typeof location !== 'undefined' ? location.search : '');
  var selectedLesson = params.get('lesson');
  var selectedCourse = params.get('course');
  var courseScope = document.getElementById('queue-course');
  var courseName = document.getElementById('queue-course-name');
  var clearCourse = document.getElementById('queue-all-courses');
  if (params.get('q')) search.value = params.get('q');
  if (['active', 'queued', 'blocked', 'failed', 'cancelled', 'done', 'all'].includes(params.get('state'))) filter.value = params.get('state');
  if (selectedLesson) filter.value = 'all';
  var stateLabels = {
    working: 'Running', queued: 'Waiting', blocked: 'Waiting for API credits / billing', failed: 'Failed',
    cancelled: 'Cancelled', done: 'Ready', skipped: 'Skipped'
  };
  var filterLabels = {
    active: 'Running and waiting', queued: 'Waiting', blocked: 'API credits / billing', failed: 'Failed',
    cancelled: 'Cancelled', done: 'Ready', all: 'All lessons'
  };

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function matches(entry, selected) {
    if (selected === 'all') return true;
    if (selected === 'active') return ['working', 'queued', 'blocked'].includes(entry.state);
    return entry.state === selected;
  }

  function act(entry, action) {
    if (pending[entry.id]) return;
    pending[entry.id] = action;
    delete errors[entry.id];
    render();
    fetch('api/lessons/' + encodeURIComponent(entry.id) + '/' + action, {
      method: 'POST', cache: 'no-store'
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) throw new Error(body.error || 'Could not ' + action + ' this lesson. Try again.');
      });
    }).then(function () {
      // Refresh directly from the catalog instead of waiting for status.json.
      delete pending[entry.id];
      poll();
    }).catch(function (err) {
      delete pending[entry.id];
      errors[entry.id] = err.message;
      render();
    });
  }

  function syncSettings() {
    if (!settingsEditor) return;
    var editor = settingsEditor, profile = editor.profile;
    var latest = (data.videos || []).find(function (entry) { return entry.id === editor.entry.id; });
    var blocked = '';
    if (!canManage) blocked = 'Processing settings need a connection to the server.';
    else if (!profile) blocked = 'Loading settings…';
    else if (!latest) blocked = 'This lesson is no longer available. Close this window and refresh the queue.';
    else if (Number(latest.updated || 0) >= Number(profile.updated || 0) && latest.build_id !== profile.build_id) {
      blocked = 'This lesson changed or was retried elsewhere. Reload the latest settings before saving.';
    } else if (['queued', 'running'].includes(profile.state) ||
      (latest.build_id === profile.build_id && ['queued', 'working', 'blocked'].includes(latest.state))) {
      blocked = 'This lesson is queued or processing. Cancel it before changing settings.';
    }
    settingsDefault.disabled = !profile || editor.saving || !!blocked;
    settingsChunk.disabled = settingsDefault.checked || settingsDefault.disabled;
    settingsSave.disabled = !profile || editor.saving || !!blocked;
    settingsClose.disabled = !!editor.saving;
    settingsSave.textContent = editor.saving ? 'Saving…' : profile && profile.state === 'ready' ? 'Save and reprocess' : 'Save and retry';
    settingsReload.hidden = !editor.error && !blocked;
    settingsReload.disabled = !canManage || editor.saving || editor.loading || !latest;
    var errorText = editor.error || (blocked === 'Loading settings…' ? '' : blocked);
    if (settingsError.textContent !== errorText) settingsError.textContent = errorText;
    settingsError.hidden = !settingsError.textContent;
  }

  function loadSettings(editor, keepDraft) {
    editor.loading = true; editor.error = ''; editor.profile = null;
    settingsCurrent.textContent = 'Loading settings…'; syncSettings();
    fetch('api/lessons/' + encodeURIComponent(editor.entry.id) + '/settings', {cache: 'no-store'}).then(function (response) {
      return response.json().then(function (profile) {
        if (!response.ok) throw new Error(profile.error || 'Could not load processing settings.');
        if (!profile.build_id || typeof profile.chunk_minutes !== 'number') throw new Error('Invalid processing settings.');
        if (settingsEditor !== editor || !settingsDialog.open) return;
        editor.profile = profile; editor.loading = false;
        var suggestion = null;
        if (!keepDraft && /response hit max_tokens/i.test(editor.entry.error || '') && profile.build_id === editor.entry.build_id) {
          var length = profile.chunk_minutes;
          if (profile.duration && profile.duration / 60 <= length * 1.25) length = profile.duration / 60;
          suggestion = Math.max(1, Math.floor(Math.min(10, length / 2) * 10) / 10);
          if (suggestion >= profile.chunk_minutes) suggestion = null;
        }
        if (!keepDraft) {
          settingsDefault.checked = profile.custom_chunk_minutes === null && suggestion === null;
          settingsChunk.value = String(suggestion === null ? profile.chunk_minutes : suggestion);
        }
        if (settingsDefault.checked) settingsChunk.value = String(profile.default_chunk_minutes);
        settingsCurrent.textContent = 'Last attempt: ' + profile.chunk_minutes + ' min sections. Server default: ' + profile.default_chunk_minutes + ' min.';
        settingsHint.textContent = (suggestion !== null ? 'Suggested for this truncated response: ' + suggestion + ' min. ' : '') +
          'Shorter sections make more model requests, with less output per request. Cached transcription and visual analysis are reused. This saves a setting for this lesson and starts a new attempt.';
        syncSettings();
        if (!keepDraft && !settingsChunk.disabled) settingsChunk.focus();
      });
    }).catch(function (err) {
      if (settingsEditor !== editor || !settingsDialog.open) return;
      editor.loading = false; editor.error = err.message; syncSettings();
    });
  }

  function openSettings(entry) {
    settingsEditor = {entry: entry, profile: null, loading: false, saving: false, error: ''};
    settingsLesson.textContent = entry.course + ' · ' + (entry.title || entry.source_name);
    settingsDefault.checked = false; settingsChunk.value = '';
    settingsHint.textContent = '';
    settingsDialog.showModal();
    loadSettings(settingsEditor, false);
  }

  if (settingsDialog) {
    settingsDefault.addEventListener('change', function () {
      if (settingsEditor && settingsEditor.profile && settingsDefault.checked) settingsChunk.value = String(settingsEditor.profile.default_chunk_minutes);
      syncSettings();
    });
    settingsClose.addEventListener('click', function () { if (!settingsEditor || !settingsEditor.saving) settingsDialog.close(); });
    settingsDialog.addEventListener('cancel', function (event) { if (settingsEditor && settingsEditor.saving) event.preventDefault(); });
    settingsDialog.addEventListener('close', function () {
      if (settingsDialog.open) return;
      var id = settingsEditor && settingsEditor.entry.id;
      settingsEditor = null;
      var control = id && document.querySelector('[data-job-id="' + id + '"][data-action="settings"]');
      if (control) control.focus({preventScroll: true}); else filter.focus({preventScroll: true});
    });
    settingsReload.addEventListener('click', function () { if (settingsEditor && !settingsEditor.saving) loadSettings(settingsEditor, true); });
    settingsForm.addEventListener('submit', function (event) {
      event.preventDefault(); syncSettings();
      if (!settingsEditor || settingsSave.disabled) return;
      var editor = settingsEditor;
      var minutes = settingsDefault.checked ? null : Number(settingsChunk.value);
      if (minutes !== null && (!settingsChunk.value.trim() || !Number.isFinite(minutes) || minutes < 1 || minutes > 120)) {
        editor.error = 'Choose a section length between 1 and 120 minutes.'; syncSettings(); settingsChunk.focus(); return;
      }
      editor.saving = true; editor.error = ''; syncSettings();
      fetch('api/lessons/' + encodeURIComponent(editor.entry.id) + '/settings', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({build_id: editor.profile.build_id, chunk_minutes: minutes})
      }).then(function (response) {
        return response.json().then(function (body) { if (!response.ok) throw new Error(body.error || 'Could not save processing settings.'); });
      }).then(function () {
        editor.saving = false;
        if (settingsEditor === editor) settingsDialog.close();
        poll();
      }).catch(function (err) {
        editor.saving = false; editor.error = err.message;
        if (settingsEditor === editor) syncSettings();
      });
    });
  }

  function renderProviders() {
    if (!providerList) return;
    var focused = document.activeElement;
    var focusProvider = focused && focused.dataset && focused.dataset.provider;
    providerList.textContent = '';
    (data.provider_pauses || []).forEach(function (pause) {
      var section = element('section', 'provider-pause');
      section.appendChild(element('h2', '', (pause.name || pause.provider) + ' paused'));
      section.appendChild(element('p', '', pause.message));
      section.appendChild(element('p', 'library-hint', 'Restore credits or billing access, then resume. Waiting lessons will continue automatically.'));
      var button = element('button', '', resuming[pause.id] ? 'Resuming…' : 'Resume requests');
      button.type = 'button'; button.disabled = !canManage || !!resuming[pause.id];
      button.dataset.provider = pause.provider;
      button.addEventListener('click', function () {
        if (!canManage || resuming[pause.id]) return;
        resuming[pause.id] = true; delete resumeErrors[pause.id]; renderProviders();
        fetch('api/providers/' + encodeURIComponent(pause.provider) + '/resume', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pause_id: pause.id})
        }).then(function (response) {
          return response.json().then(function (body) {
            if (!response.ok) throw new Error(body.error || 'Could not resume requests. Try again.');
          });
        }).then(function () { delete resuming[pause.id]; poll(); }).catch(function (err) {
          delete resuming[pause.id]; resumeErrors[pause.id] = err.message; renderProviders();
        });
      });
      section.appendChild(button);
      if (resumeErrors[pause.id]) section.appendChild(element('p', 'job-error', resumeErrors[pause.id]));
      providerList.appendChild(section);
      if (focusProvider === pause.provider) button.focus({preventScroll: true});
    });
  }

  function row(entry, controls) {
    var item = element('li', 'job ' + entry.state);
    var heading = element('div', 'job-heading');
    heading.appendChild(element('h2', '', entry.title || entry.source_name || 'Untitled lesson'));
    var badge = stateLabels[entry.state] || entry.state;
    if (entry.state === 'queued' && entry.queue_position) badge = '#' + entry.queue_position + ' in queue';
    heading.appendChild(element('span', 'job-state', badge));
    item.appendChild(heading);
    item.appendChild(element('p', 'job-source', entry.course + ' · ' + (entry.source_path || entry.source_name || entry.title)));

    if (entry.state === 'working') {
      if (entry.executions && entry.executions.length > 1) {
        var active = entry.executions.filter(function (e) { return e.state === 'running'; });
        active.forEach(function (execution) {
          var machine = element('p', 'job-work');
          if (/^(server|[a-f0-9]{32})$/.test(execution.worker_id || '')) {
            var link = element('a', '', execution.worker_name || 'Processing computer');
            link.href = 'workers.html#worker-' + execution.worker_id;
            machine.appendChild(link);
          } else machine.appendChild(element('span', '', execution.worker_name || 'Processing computer'));
          machine.appendChild(element('span', '', ' · ' + (execution.description || execution.kind)));
          item.appendChild(machine);
        });
        var ready = entry.executions.filter(function (e) { return e.state === 'pending'; }).length;
        var local = entry.executions.filter(function (e) { return e.state === 'fallback'; }).length;
        var waiting = [];
        if (ready) waiting.push(ready + (ready === 1 ? ' operation ready' : ' operations ready') + ' for a processor');
        if (local) waiting.push(local + (local === 1 ? ' operation waiting' : ' operations waiting') + ' for server fallback');
        if (waiting.length) item.appendChild(element('p', 'job-work', waiting.join(' · ')));
      } else if (entry.execution) {
        var execution = entry.execution;
        var machine = element('p', 'job-work');
        var name = execution.worker_name || (execution.state === 'fallback' ? 'Library server (waiting for CPU)' : 'Assigning a processor');
        if (/^(server|[a-f0-9]{32})$/.test(execution.worker_id || '')) {
          var workerLink = element('a', '', name);
          workerLink.href = 'workers.html#worker-' + execution.worker_id;
          machine.appendChild(workerLink);
        } else machine.textContent = name;
        item.appendChild(machine);
        if (execution.error) item.appendChild(element('p', 'job-work', execution.error));
      }
      var progress = V2WProgress.describe(entry);
      var stage = progress.text;
      if (progress.hasElapsed) stage += ' · ' + progress.elapsed + ' in this stage';
      item.appendChild(element('p', 'job-detail', stage));
      if (progress.detail) item.appendChild(element('p', 'job-work', progress.detail));
      item.appendChild(element('p', 'job-estimate', progress.estimate));
      var bar = element('div', 'bar' + (progress.fraction === null ? ' indeterminate' : '') + (progress.stale ? ' progress-paused' : ''));
      bar.setAttribute('role', 'progressbar');
      bar.setAttribute('aria-label', progress.label);
      bar.setAttribute('aria-valuemin', '0');
      bar.setAttribute('aria-valuemax', '100');
      if (progress.fraction !== null) bar.setAttribute('aria-valuenow', String(progress.percent));
      var fill = element('i');
      fill.style.width = progress.fraction === null ? '30%' : progress.percent + '%';
      bar.appendChild(fill);
      item.appendChild(bar);
    } else if (entry.state === 'blocked') {
      item.appendChild(element('p', 'job-detail', entry.blocked ? entry.blocked.message : 'Waiting for provider access. Resume requests after restoring credits or billing.'));
    } else if (entry.state === 'queued') {
      item.appendChild(element('p', 'job-detail', entry.queue_position === 1 ? 'Next in line for an available processor' : 'Waiting for processing capacity'));
    } else if (entry.state === 'cancelled') {
      item.appendChild(element('p', 'job-detail', 'Processing cancelled. Your lesson video is retained.'));
    }
    if (entry.error || entry.state === 'failed') {
      item.appendChild(element('p', 'job-error', entry.error || entry.label || 'Processing failed. Retry when the issue is resolved.'));
    }
    if (errors[entry.id]) {
      var error = element('p', 'job-error', errors[entry.id]);
      error.setAttribute('role', 'alert');
      item.appendChild(error);
    }

    var actions = element('div', 'job-actions');
    if (canManage && /^[a-f0-9]{32}$/.test(entry.id || '')) {
      var action = ['working', 'queued', 'blocked'].indexOf(entry.state) >= 0 ? 'cancel'
        : ['failed', 'cancelled'].indexOf(entry.state) >= 0 ? 'retry' : null;
      if (action) {
        var button = element('button', '', pending[entry.id]
          ? (action === 'cancel' ? 'Cancelling…' : 'Retrying…')
          : (action === 'cancel' ? 'Cancel processing' : 'Retry processing'));
        button.type = 'button';
        button.disabled = !!pending[entry.id];
        button.dataset.jobId = entry.id;
        button.dataset.action = action;
        button.setAttribute('aria-label', (action === 'cancel' ? 'Cancel ' : 'Retry ') + entry.title);
        button.addEventListener('click', function () { act(entry, action); });
        controls[entry.id + ':' + action] = button;
        actions.appendChild(button);
      }
      if (settingsDialog && entry.processing && entry.build_id) {
        var settings = element('button', '', /response hit max_tokens/i.test(entry.error || '') ? 'Adjust settings & retry' : 'Processing settings');
        settings.type = 'button'; settings.dataset.jobId = entry.id; settings.dataset.action = 'settings';
        settings.disabled = !!pending[entry.id];
        settings.addEventListener('click', function () { openSettings(entry); });
        controls[entry.id + ':settings'] = settings; actions.appendChild(settings);
      }
    }
    // Only allow generated relative lesson links, including for old static status.
    if (entry.lesson_href && /^[a-z0-9-]+\/[a-z0-9-]+\.html$/.test(entry.lesson_href)) {
      var link = element('a', '', entry.state === 'done' ? 'Open lesson' : 'Open published version');
      link.href = entry.lesson_href;
      actions.appendChild(link);
    }
    item.appendChild(actions);
    return item;
  }

  function render() {
    renderProviders();
    syncSettings();
    var allVideos = data.videos || [];
    var position = 0;
    allVideos.forEach(function (entry) {
      if (entry.state === 'queued') { position++; entry.queue_position = entry.queue_position || position; }
    });
    var videos = selectedCourse ? allVideos.filter(function (entry) {
      return entry.course_id === selectedCourse || entry.course_slug === selectedCourse;
    }) : allVideos;
    if (courseScope) {
      courseScope.hidden = !selectedCourse;
      courseName.textContent = selectedCourse ? 'Course: ' + (videos.length ? videos[0].course : 'No remaining jobs') : '';
    }
    var counts = {};
    videos.forEach(function (entry) {
      counts[entry.state] = (counts[entry.state] || 0) + 1;
    });
    var parts = [
      (counts.working || 0) + ' running', (counts.queued || 0) + ' waiting',
      (counts.failed || 0) + ' failed', (counts.done || 0) + ' ready'
    ];
    if (counts.cancelled) parts.push(counts.cancelled + ' cancelled');
    if (counts.blocked) parts.push(counts.blocked + ' waiting for API access');
    if (summary.textContent !== parts.join(' · ')) summary.textContent = parts.join(' · ');
    Array.prototype.forEach.call(filter.options, function (option) {
      var total = videos.filter(function (entry) { return matches(entry, option.value); }).length;
      option.textContent = filterLabels[option.value] + ' (' + total + ')';
    });

    var query = search.value.trim().toLowerCase();
    var shown = videos.filter(function (entry) {
      if (selectedLesson && entry.id !== selectedLesson) return false;
      return matches(entry, filter.value) && [entry.title, entry.course, entry.source_name, entry.source_path]
        .join(' ').toLowerCase().includes(query);
    });
    var ranks = { working: 0, blocked: 1, queued: 2, failed: 3, cancelled: 4, done: 5, skipped: 6 };
    shown.sort(function (a, b) {
      var rank = ranks[a.state] - ranks[b.state];
      if (rank) return rank;
      if (a.state === 'queued') return (a.queue_position || 0) - (b.queue_position || 0);
      return (b.updated || 0) - (a.updated || 0);
    });

    // Polling must not take keyboard focus away from an action being inspected.
    var focused = document.activeElement;
    var focusKey = focused && focused.dataset && focused.dataset.jobId
      ? focused.dataset.jobId + ':' + focused.dataset.action : null;
    var controls = {};
    list.textContent = '';
    shown.forEach(function (entry) { list.appendChild(row(entry, controls)); });
    if (focusKey && controls[focusKey]) controls[focusKey].focus({ preventScroll: true });
    empty.hidden = shown.length > 0;
    empty.textContent = !videos.length ? (selectedCourse ? 'No processing activity for this course.' : 'No processing activity yet. Upload videos to get started.')
      : query ? 'No lessons match your search.'
      : filter.value === 'active' ? 'Nothing is running or waiting. Choose Ready to see completed lessons.'
      : 'No lessons with this status.';
    updated.textContent = data.updated ? 'Status updated ' + new Date(data.updated * 1000).toLocaleTimeString() : '';
  }

  function get(url) {
    return fetch(url, { cache: 'no-store' }).then(function (res) {
      if (!res.ok) throw new Error('Status unavailable');
      return res.json();
    }).then(function (body) {
      if (!body || !Array.isArray(body.videos)) throw new Error('Invalid status response');
      return body;
    });
  }

  function poll() {
    if (polling) { pollAgain = true; return; }
    polling = true;
    get('api/jobs').then(function (body) {
      canManage = true;
      return body;
    }).catch(function () {
      canManage = false;
      return get('status.json?t=' + Date.now());
    }).then(function (body) {
      data = body;
      loaded = true;
      notice.textContent = data.error || (canManage ? '' : 'Read-only status. Queue controls are currently unavailable.');
      notice.hidden = !notice.textContent;
      render();
    }).catch(function () {
      canManage = false;
      if (loaded) render();
      else {
        empty.hidden = false;
        empty.textContent = 'Queue status is not available yet.';
      }
      notice.textContent = loaded ? 'Connection lost. Showing the last known queue; retrying automatically.'
        : 'Waiting for the worker to report its queue. This page will update automatically.';
      notice.hidden = false;
    }).finally(function () {
      polling = false;
      if (pollAgain) { pollAgain = false; poll(); }
    });
  }

  filter.addEventListener('change', function () { selectedLesson = null; render(); });
  if (clearCourse) clearCourse.addEventListener('click', function () { selectedCourse = null; render(); });
  search.addEventListener('input', function () { selectedLesson = null; render(); });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
  poll();
  setInterval(poll, 3000);
})();
