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
  var params = new URLSearchParams(typeof location !== 'undefined' ? location.search : '');
  var selectedLesson = params.get('lesson');
  var selectedCourse = params.get('course');
  var courseScope = document.getElementById('queue-course');
  var courseName = document.getElementById('queue-course-name');
  var clearCourse = document.getElementById('queue-all-courses');
  if (params.get('q')) search.value = params.get('q');
  if (['active', 'queued', 'failed', 'cancelled', 'done', 'all'].includes(params.get('state'))) filter.value = params.get('state');
  if (selectedLesson) filter.value = 'all';
  var stateLabels = {
    working: 'Running', queued: 'Waiting', failed: 'Failed',
    cancelled: 'Cancelled', done: 'Ready', skipped: 'Skipped'
  };
  var filterLabels = {
    active: 'Running and waiting', queued: 'Waiting', failed: 'Failed',
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
    if (selected === 'active') return entry.state === 'working' || entry.state === 'queued';
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
    } else if (entry.state === 'queued') {
      item.appendChild(element('p', 'job-detail', entry.queue_position === 1 ? 'Next in line for an available processor' : 'Waiting for processing capacity'));
    } else if (entry.state === 'cancelled') {
      item.appendChild(element('p', 'job-detail', 'Processing cancelled. Your source video is still in the library.'));
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
      var action = ['working', 'queued'].indexOf(entry.state) >= 0 ? 'cancel'
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
    var ranks = { working: 0, queued: 1, failed: 2, cancelled: 3, done: 4, skipped: 5 };
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
