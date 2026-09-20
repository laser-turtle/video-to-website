(function () {
  'use strict';
  var list = document.getElementById('worker-list');
  if (!list) return;
  var summary = document.getElementById('worker-summary'), notice = document.getElementById('worker-notice');
  var connect = document.getElementById('worker-connect'), pairing = document.getElementById('worker-pairing');
  var filter = document.getElementById('worker-filter');
  var data = {workers: []}, online = false, loading = false, pending = {}, editing = {}, expanded = {}, confirming = {}, sequence = 0;
  var setupLoading = false, generating = false, generateButton = null;
  var labels = {available: 'Available', busy: 'Processing', draining: 'Finishing current task', paused: 'Paused',
    offline: 'Offline', revoked: 'Revoked', archived: 'Archived', cooldown: 'Recovering after a failure'};
  var kinds = {transcribe: 'Transcription', scenes: 'Scene analysis', frame: 'Screenshot',
    frame_hash: 'Screenshot selection', activity: 'Motion analysis', clip: 'Clip encoding'};
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function duration(value) {
    var s = Math.max(0, Math.round(Number(value) || 0));
    return s < 60 ? s + 's' : s < 3600 ? Math.floor(s / 60) + 'm ' + s % 60 + 's' : Math.floor(s / 3600) + 'h ' + Math.floor(s % 3600 / 60) + 'm';
  }
  function bytes(value) { return ((Number(value) || 0) / 1073741824).toFixed(1) + ' GiB'; }
  function button(text, key, fn) {
    var b = el('button', '', text); b.type = 'button'; b.dataset.focusKey = key; b.disabled = !online;
    b.addEventListener('click', fn); return b;
  }
  function request(url, body) {
    var options = {cache: 'no-store', signal: AbortSignal.timeout(10000)};
    if (body !== undefined) { options.method = 'POST'; options.headers = {'Content-Type': 'application/json'}; options.body = JSON.stringify(body); }
    return fetch(url, options).then(function (r) { return r.json().then(function (v) {
      if (!r.ok) throw new Error(v.error || 'The server could not complete this request.'); return v;
    }); });
  }
  function act(worker, body) {
    if (pending[worker.id] || !online) return;
    pending[worker.id] = true; render();
    request('api/workers/' + encodeURIComponent(worker.id), body).then(function () {
      delete editing[worker.id];
      delete confirming[worker.id];
      notice.textContent = body.action === 'stop' ? 'Helper paused. Its current task will restart on the server when the CPU is available.'
        : body.action === 'pause' ? 'New tasks paused. Any current task can finish.'
        : body.action === 'archive' ? 'Worker archived and disconnected. Unfinished work returns to the server; history is kept under Archived computers.'
        : body.action === 'restore' ? 'Worker restored to the list. Access stays revoked until you reconnect it with a fresh pairing code.'
        : body.action === 'delete' ? 'Worker and contribution history deleted. Your library and lessons are unchanged.'
        : body.action === 'revoke' ? 'Access revoked. Pair this computer again to reconnect it.' : 'Worker updated.';
      return poll();
    }).catch(function (e) { notice.textContent = e.message; }).finally(function () { delete pending[worker.id]; render(); });
  }
  function taskRow(task, current) {
    var row = el('li', 'worker-task'), title = el('a', '', task.title);
    title.href = 'queue.html?lesson=' + encodeURIComponent(task.lesson_id); row.appendChild(title);
    row.appendChild(el('p', 'worker-subtle', task.course + ' · ' + (kinds[task.kind] || task.kind)));
    if (task.description) row.appendChild(el('p', 'worker-subtle', task.description));
    if (current) {
      var p = task.progress, age = p ? Math.max(0, Date.now() / 1000 - p.updated) : 0;
      row.appendChild(el('p', '', p && p.label || 'Starting this operation…'));
      if (p && p.detail) row.appendChild(el('p', 'worker-subtle', p.detail));
      var f = p && Number.isFinite(p.completed) && Number.isFinite(p.total) && p.total > 0 ? Math.min(1, Math.max(0, p.completed / p.total)) : null;
      var bar = el('div', 'bar' + (f === null ? ' indeterminate' : '') + (age > 30 ? ' progress-paused' : ''));
      bar.setAttribute('role', 'progressbar'); bar.setAttribute('aria-label', p && p.label || 'Task progress');
      if (f !== null) bar.setAttribute('aria-valuenow', String(Math.round(f * 100)));
      bar.setAttribute('aria-valuemin', '0'); bar.setAttribute('aria-valuemax', '100');
      var fill = el('i'); fill.style.width = f === null ? '30%' : Math.round(f * 100) + '%'; bar.appendChild(fill); row.appendChild(bar);
      row.appendChild(el('p', 'worker-subtle', (f === null ? '' : Math.round(f * 100) + '% · ') + duration(Date.now() / 1000 - task.started)
        + ' elapsed' + (age > 30 ? ' · Waiting for a progress report' : '')));
      if (p && Number.isFinite(p.eta_seconds) && age <= 30) row.appendChild(el('p', 'worker-subtle', 'About ' + duration(p.eta_seconds) + ' remaining in this operation'));
    } else {
      row.appendChild(el('p', 'worker-subtle', (task.state === 'accepted' ? 'Accepted' : task.state === 'lost' ? 'Interrupted' : task.state)
        + ' · ' + duration(task.metrics.processing_seconds) + ' processing' + (task.finished ? ' · ' + new Date(task.finished * 1000).toLocaleString() : '')));
      if (task.error) row.appendChild(el('p', 'worker-subtle', task.error));
    }
    return row;
  }
  function workerRow(w) {
    var card = el('li', 'worker-card'); card.id = 'worker-' + w.id;
    var head = el('div', 'worker-heading'); head.appendChild(el('h2', '', w.name));
    head.appendChild(el('span', 'worker-state ' + w.status, labels[w.status] || w.status)); card.appendChild(head);
    card.appendChild(el('p', 'worker-subtle', w.id === 'server' ? 'Permanent local processing · Automatic fallback'
      : (w.platform || 'Waiting for the helper to connect').replace('Darwin', 'macOS').replace(' / ', ' · ')));
    var d = w.details || {};
    if (w.id !== 'server') {
      if (d.hardware) card.appendChild(el('p', 'worker-subtle', d.hardware));
      card.appendChild(el('p', 'worker-subtle', w.capabilities.map(function (k) { return kinds[k] || k; }).join(' · ') || 'No processing capabilities reported yet'));
      if (d.whisper) card.appendChild(el('p', 'worker-subtle', 'Whisper: ' + d.whisper + ' · Clips: '
        + (d.clip_encoder === 'h264_nvenc' ? 'NVIDIA GPU (H.264)' : 'CPU (H.264)')));
      if (d.cache_limit_bytes) card.appendChild(el('p', 'worker-subtle', 'Temporary cache ' + bytes(d.cache_used_bytes) + ' / ' + bytes(d.cache_limit_bytes) + ' · ' + bytes(d.free_bytes) + ' disk free'));
      card.appendChild(el('p', 'worker-subtle', w.last_seen ? 'Last contact ' + duration(Date.now() / 1000 - w.last_seen) + ' ago' : 'Not connected yet'));
    }
    var stats = el('div', 'worker-stats');
    [[w.totals.accepted_tasks, 'accepted tasks'], [duration(w.totals.media_seconds), 'audio transcribed'],
      [duration(w.totals.processing_seconds), 'processing time'], [w.totals.interrupted_tasks, 'failed or interrupted']].forEach(function (s) {
      var n = el('div'); n.appendChild(el('strong', '', s[0])); n.appendChild(el('span', '', s[1])); stats.appendChild(n);
    }); card.appendChild(stats);
    if (w.active.length) {
      var tasks = el('ul', 'worker-tasks'); w.active.forEach(function (t) { tasks.appendChild(taskRow(t, true)); }); card.appendChild(tasks);
    } else if (w.status === 'offline') card.appendChild(el('p', '', 'This computer can reconnect whenever it is available. Its contribution history is retained.'));
    if (w.archived) card.appendChild(el('p', 'worker-subtle', 'Archived and disconnected. Contribution history is retained.'));
    if (w.id !== 'server') {
      var controls = el('div', 'job-actions'), actions = [];
      if (w.archived) actions.push(['Restore to list', 'restore']);
      else {
        if (!w.revoked) {
          actions.push(w.paused ? ['Resume', 'resume'] : ['Pause new tasks', 'pause']);
          if (w.active.length) actions.push(['Stop task and pause', 'stop']);
          actions.push(['Revoke access', 'revoke']);
        }
        actions.push(['Rename', 'rename'], ['Archive', 'archive']);
      }
      actions.push(['Delete worker…', 'delete']);
      actions.forEach(function (a) {
        var b = button(a[0], w.id + '-' + a[1], function () {
          if (a[1] === 'rename') { editing[w.id] = w.name; render(); }
          else if (a[1] === 'delete') { confirming[w.id] = true; render(); }
          else act(w, {action: a[1]});
        }); b.disabled = b.disabled || !!pending[w.id]; controls.appendChild(b);
      }); card.appendChild(controls);
      if (editing[w.id] !== undefined) {
        var form = el('form', 'worker-rename'), label = el('label', '', 'Worker name'), input = el('input');
        input.type = 'text'; input.maxLength = 120; input.value = editing[w.id]; input.dataset.focusKey = w.id + '-name';
        input.addEventListener('input', function () { editing[w.id] = input.value; }); label.appendChild(input); form.appendChild(label);
        form.appendChild(button('Save name', w.id + '-save', function () { act(w, {action: 'rename', name: input.value}); }));
        form.addEventListener('submit', function (e) { e.preventDefault(); act(w, {action: 'rename', name: input.value}); });
        form.appendChild(button('Cancel', w.id + '-cancel', function () { delete editing[w.id]; render(); })); card.appendChild(form);
      }
      if (confirming[w.id]) {
        var confirmation = el('div', 'worker-delete'); confirmation.setAttribute('role', 'group');
        confirmation.setAttribute('aria-label', 'Delete ' + w.name);
        confirmation.appendChild(el('p', '', w.deletion_blocked_reason || 'Delete ' + w.name + ' and all of its contribution history permanently? This disconnects the helper. Your library and published lessons are kept.'));
        var remove = button('Delete permanently', w.id + '-delete-confirm', function () { act(w, {action: 'delete'}); });
        remove.disabled = remove.disabled || !!pending[w.id] || !!w.deletion_blocked_reason;
        confirmation.appendChild(remove);
        confirmation.appendChild(button('Keep worker', w.id + '-delete-cancel', function () { delete confirming[w.id]; render(); }));
        card.appendChild(confirmation);
      }
    }
    var history = el('details', 'worker-history'); history.open = !!expanded[w.id];
    history.addEventListener('toggle', function () { expanded[w.id] = history.open; });
    history.appendChild(el('summary', '', 'Recent contributions (' + w.recent.length + ')'));
    var recent = el('ol', 'worker-tasks'); w.recent.forEach(function (t) { recent.appendChild(taskRow(t, false)); }); history.appendChild(recent);
    if (!w.recent.length) history.appendChild(el('p', 'worker-subtle', 'Completed and interrupted tasks will appear here.'));
    card.appendChild(history); return card;
  }
  function render() {
    var focused = document.activeElement, key = focused && focused.dataset && focused.dataset.focusKey, selection = focused && focused.selectionStart;
    var helpers = data.workers.filter(function (w) { return w.id !== 'server' && !w.revoked && !w.archived; });
    var connected = helpers.filter(function (w) { return w.online; }).length;
    var running = data.workers.reduce(function (n, w) { return n + w.active.length; }, 0);
    summary.textContent = connected + (connected === 1 ? ' helper connected · ' : ' helpers connected · ')
      + running + (running === 1 ? ' native task running' : ' native tasks running') + (online ? '' : ' · Last known status');
    list.textContent = '';
    var shown = data.workers.filter(function (w) {
      if (filter.value === 'all') return true;
      if (filter.value === 'archived') return !!w.archived;
      if (filter.value === 'connected') return w.online && !w.archived;
      return !w.archived;
    });
    shown.forEach(function (w) { list.appendChild(workerRow(w)); });
    var empty = document.getElementById('worker-empty');
    empty.hidden = filter.value === 'archived' || filter.value === 'connected' ? shown.length > 0 : helpers.length > 0;
    empty.textContent = filter.value === 'archived' ? 'No archived computers.'
      : filter.value === 'connected' ? 'No computers are currently connected.'
      : 'No current helpers. The server processes locally. Connect a computer for extra capacity, or choose Archived computers to see stored history.';
    connect.disabled = !online || setupLoading;
    if (generateButton) generateButton.disabled = !online || generating;
    if (key) Array.prototype.forEach.call(list.querySelectorAll('[data-focus-key]'), function (n) {
      if (n.dataset.focusKey === key) { n.focus({preventScroll: true}); if (typeof selection === 'number' && n.setSelectionRange) n.setSelectionRange(selection, selection); }
    });
  }
  function poll() {
    if (loading) return Promise.resolve(); loading = true;
    return request('api/workers').then(function (value) {
      if (!value || !Array.isArray(value.workers)) throw new Error('Invalid worker status response.');
      data = value; online = true;
      if (notice.dataset.unavailable) { notice.textContent = ''; delete notice.dataset.unavailable; }
    }).catch(function () {
      online = false; notice.dataset.unavailable = 'true';
      notice.textContent = 'Live worker status is unavailable. Showing the last response; controls are disabled until the server reconnects.';
    }).finally(function () { loading = false; render(); });
  }
  function setupPanel(info) {
    if (!/^v2w-worker-[a-f0-9]{12}\.pyz$/.test(info.filename || '') || !/^[a-f0-9]{64}$/.test(info.sha256 || '') || !info.guides) {
      throw new Error('The server returned invalid helper download information. Refresh and try again.');
    }
    pairing.hidden = false; pairing.textContent = ''; generateButton = null;
    pairing.appendChild(el('h2', '', 'Connect a computer to your library'));
    pairing.appendChild(el('p', '', 'Download the helper from this server and run it with Python 3.11 or newer. No Git checkout, pip install or virtualenv needed.'));
    var platformLabel = el('label', 'worker-platform', 'Computer to connect');
    var target = el('select'); target.id = 'worker-platform';
    ['windows', 'macos', 'linux'].forEach(function (key) {
      var option = el('option', '', info.guides[key].label); option.value = key; target.appendChild(option);
    });
    var agent = typeof navigator !== 'undefined' ? navigator.userAgent : '';
    target.value = /Windows/i.test(agent) ? 'windows' : /Macintosh|Mac OS/i.test(agent) ? 'macos' : 'linux';
    platformLabel.appendChild(target); pairing.appendChild(platformLabel);
    pairing.appendChild(el('h3', '', '1. Download the helper'));
    var download = el('a', 'worker-download', 'Download helper (' + Math.ceil(info.bytes / 1024) + ' KB)');
    download.id = 'worker-download'; download.href = 'api/workers/download?sha256=' + info.sha256; download.download = info.filename;
    pairing.appendChild(download);
    pairing.appendChild(el('p', 'worker-subtle', 'Save the file on the computer you want to connect. Run the commands below in that download folder.'));
    pairing.appendChild(el('h3', '', '2. Check that computer'));
    function commandBox(label, id) {
      var field = el('textarea', 'worker-command'); field.readOnly = true; field.rows = id === 'worker-connect-command' ? 4 : 2; field.id = id;
      field.setAttribute('aria-label', label); pairing.appendChild(field); return field;
    }
    var checkCommand = commandBox('Tool check command', 'worker-check-command');
    pairing.appendChild(el('p', 'worker-subtle', 'FFmpeg handles images and clips; Whisper handles transcription. The check shows what is ready or missing. Either tool is enough to contribute. Model weights download automatically when needed.'));
    var native = el('details', 'worker-install'); native.appendChild(el('summary', '', 'Install media tools if the check reports them missing'));
    var guidance = el('div'); native.appendChild(guidance); pairing.appendChild(native);
    var toolsNote = el('p', 'worker-subtle', 'You can extract native tool downloads into a tools folder beside the helper; common bin and Release subfolders are detected automatically. Keep supporting libraries with their executables. For other locations, add --tools followed by the folder path (repeatable and saved).');
    native.appendChild(toolsNote);
    pairing.appendChild(el('h3', '', '3. Connect when ready'));
    var gpuLabel = el('label', 'worker-gpu'); var gpu = el('input'); gpu.type = 'checkbox'; gpu.id = 'worker-nvidia';
    gpuLabel.appendChild(gpu); gpuLabel.appendChild(el('span', '', 'Use NVIDIA GPU clip encoding (requires NVENC in FFmpeg)')); pairing.appendChild(gpuLabel);
    var command = commandBox('Helper connection command', 'worker-connect-command'); command.hidden = true;
    var code = null, codeSequence = 0;
    var pairingNote = el('p', 'worker-subtle', 'Generate a code after the check is ready. Codes expire after 10 minutes.');
    var select = button('Select connection command', 'select-command', function () { command.focus(); command.select(); }); select.hidden = true;
    var base = location.origin + location.pathname.slice(0, location.pathname.lastIndexOf('/'));
    function quote(value) { return "'" + value.replace(/'/g, target.value === 'windows' ? "''" : "'\\''") + "'"; }
    function launch() { return (target.value === 'windows' ? 'py -3 ' : 'python3 ') + quote(info.filename); }
    var reconnect = el('p', 'worker-subtle');
    function commands() {
      checkCommand.value = launch() + ' --check';
      if (code) command.value = launch() + ' --server ' + quote(base) + ' --pairing-code ' + quote(code)
        + ' --clip-encoder ' + (gpu.checked && target.value !== 'macos' ? 'h264_nvenc' : 'libx264');
      reconnect.textContent = 'Later, reconnect with: ' + launch() + '. Your pairing and settings are saved on that computer.';
    }
    function platformChanged() {
      gpuLabel.hidden = target.value === 'macos';
      guidance.textContent = '';
      var guide = info.guides[target.value]; guidance.appendChild(el('p', '', guide.instructions));
      if (guide.command) { var pre = el('pre'); pre.appendChild(el('code', '', guide.command)); guidance.appendChild(pre); }
      guide.links.forEach(function (item) {
        if (!/^https:\/\//.test(item.url)) return;
        var link = el('a', '', item.label); link.href = item.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
        var p = el('p'); p.appendChild(link); guidance.appendChild(p);
      });
      commands();
    }
    var panelSequence = sequence;
    generateButton = button('Generate connection command', 'generate-pairing', function () {
      if (!online || generating) return;
      generating = true; generateButton.disabled = true;
      request('api/workers/pairing', {}).then(function (body) {
        if (panelSequence !== sequence) return;
        if (!/^[A-Za-z0-9_-]+$/.test(body.code || '')) throw new Error('Invalid pairing code. Try again.');
        code = body.code; commands(); command.hidden = false; select.hidden = false;
        generateButton.textContent = 'Generate a new code'; pairingNote.textContent = 'This code works once and expires in 10 minutes. The helper download contains no credentials.';
        var current = ++codeSequence;
        setTimeout(function () {
          if (panelSequence !== sequence || current !== codeSequence) return;
          code = null; command.value = ''; command.hidden = true; select.hidden = true;
          pairingNote.textContent = 'This code expired. Generate a new connection command; the download is still usable.';
        }, body.expires_in * 1000);
        command.focus(); command.select();
      }).catch(function (e) { if (panelSequence === sequence) pairingNote.textContent = e.message; }).finally(function () {
        generating = false; if (generateButton) generateButton.disabled = !online;
      });
    });
    pairing.appendChild(generateButton); pairing.appendChild(select); pairing.appendChild(pairingNote); pairing.appendChild(reconnect);
    pairing.appendChild(button('Close', 'close-pairing', function () { sequence++; pairing.hidden = true; pairing.textContent = ''; generateButton = null; }));
    target.addEventListener('change', platformChanged); gpu.addEventListener('change', commands); platformChanged();
  }
  connect.addEventListener('click', function () {
    if (!online || setupLoading) return;
    setupLoading = true; connect.disabled = true;
    pairing.hidden = true; pairing.textContent = ''; generateButton = null;
    var current = ++sequence;
    request('api/workers/download-info').then(function (info) { if (current === sequence) setupPanel(info); })
      .catch(function (e) { notice.textContent = e.message; })
      .finally(function () { setupLoading = false; connect.disabled = !online; });
  });
  document.getElementById('worker-refresh').addEventListener('click', poll); filter.addEventListener('change', render);
  function repeat() { poll().finally(function () { setTimeout(repeat, 3000); }); }
  repeat();
}());
