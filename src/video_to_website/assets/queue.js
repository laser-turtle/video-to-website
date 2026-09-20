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

  function duration(seconds) {
    var minutes = Math.floor(seconds / 60);
    return minutes ? minutes + 'm ' + Math.floor(seconds % 60) + 's' : Math.floor(seconds) + 's';
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
      var stage = entry.label || 'Processing';
      if (entry.elapsed) stage += ' · ' + duration(entry.elapsed) + ' in this stage';
      item.appendChild(element('p', 'job-detail', stage));
      if (entry.steps && entry.step) {
        var bar = element('div', 'bar');
        var fill = element('i');
        fill.style.width = 100 * Math.max(0, entry.step - 0.5) / entry.steps + '%';
        bar.appendChild(fill);
        item.appendChild(bar);
      }
    } else if (entry.state === 'queued') {
      item.appendChild(element('p', 'job-detail', entry.queue_position === 1 ? 'Next to start when the worker is available' : 'Waiting for earlier lessons to finish'));
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
    var videos = data.videos || [];
    var counts = {};
    var position = 0;
    videos.forEach(function (entry) {
      counts[entry.state] = (counts[entry.state] || 0) + 1;
      if (entry.state === 'queued') {
        position++;
        entry.queue_position = entry.queue_position || position;
      }
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
    empty.textContent = !videos.length ? 'No processing activity yet. Upload videos to get started.'
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

  filter.addEventListener('change', render);
  search.addEventListener('input', render);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
  poll();
  setInterval(poll, 3000);
})();
