var V2WStorage = (function () {
  'use strict';
  var panel = document.getElementById('storage-panel');
  var locations = document.getElementById('storage-locations');
  var message = document.getElementById('storage-message');
  var courseInput = document.getElementById('course');
  var sequence = 0;
  var rendered = 0;
  var last = null;

  function size(bytes) {
    var units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    var index = 0;
    while (bytes >= 1024 && index < units.length - 1) { bytes /= 1024; index++; }
    return (index ? bytes.toFixed(1) : Math.round(bytes)) + ' ' + units[index];
  }
  function node(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function show(data) {
    last = data;
    if (!panel || !locations) return;
    locations.textContent = '';
    var shown = data.locations.slice();
    if (!shown.some(function (item) { return item.filesystem_id === data.destination.filesystem_id; })) {
      shown.push(Object.assign({}, data.destination, { label: 'Selected upload destination' }));
    }
    shown.forEach(function (location) {
      var box = node('div', 'storage-location' + (!location.accepting_uploads ? ' full' : location.low_space ? ' low' : ''));
      var heading = node('div', 'storage-heading');
      heading.appendChild(node('strong', '', location.label));
      heading.appendChild(node('span', '', size(location.free_bytes) + ' free'));
      box.appendChild(heading);
      var meter = node('div', 'bar storage-meter');
      var percent = location.total_bytes ? Math.min(100, Math.round(100 * location.used_bytes / location.total_bytes)) : 0;
      meter.setAttribute('role', 'meter');
      meter.setAttribute('aria-label', location.label + ' disk usage');
      meter.setAttribute('aria-valuemin', '0'); meter.setAttribute('aria-valuemax', '100');
      meter.setAttribute('aria-valuenow', String(percent));
      var fill = node('i'); fill.style.width = percent + '%'; meter.appendChild(fill); box.appendChild(meter);
      box.appendChild(node('p', 'storage-detail', size(location.used_bytes) + ' used of ' + size(location.total_bytes)
        + ' · ' + size(location.buffer_bytes) + ' kept free'));
      if (location.reserved_bytes) box.appendChild(node('p', 'storage-detail', size(location.reserved_bytes) + ' reserved for uploads in progress'));
      box.appendChild(node('p', 'storage-available', location.accepting_uploads
        ? (location.low_space ? 'Low space — ' : '') + size(location.available_bytes) + ' available for new uploads'
        : 'Uploads paused: the free-space buffer has been reached.'));
      var shared = data.locations.filter(function (other) { return other.id !== location.id && other.filesystem_id === location.filesystem_id; });
      if (shared.length) box.appendChild(node('p', 'storage-detail', 'Shares disk space with ' + shared.map(function (item) { return item.label; }).join(', ')));
      locations.appendChild(box);
    });
    message.textContent = '';
    message.hidden = true;
  }
  function read(course) {
    var request = ++sequence;
    var url = 'api/storage' + (course ? '?course=' + encodeURIComponent(course) : '');
    return fetch(url, { cache: 'no-store' }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) throw new Error(body.error || 'Could not check free disk space. Try again.');
        if (!Array.isArray(body.locations) || !body.destination ||
            !Number.isFinite(body.destination.available_bytes) || !Number.isFinite(body.destination.max_upload_bytes)) {
          throw new Error('Storage information is unavailable. Try again.');
        }
        if (request >= rendered) { rendered = request; show(body); }
        return body.destination;
      });
    });
  }
  function unavailable(error) {
    if (!message) return;
    message.textContent = error.message + (last ? ' Showing the last known capacity.' : ' Connect to the library service to monitor storage.');
    message.hidden = false;
  }
  function refresh(course) {
    return read(course === undefined ? (courseInput && courseInput.value || '') : course).catch(unavailable);
  }
  function check(course, bytes) {
    return read(course).then(function (storage) {
      if (bytes <= 0 || bytes > storage.max_upload_bytes) {
        throw new Error('The file must be non-empty and no larger than ' + size(storage.max_upload_bytes) + '.');
      }
      if (bytes > storage.available_bytes) {
        throw new Error('Not enough disk space: this file needs ' + size(bytes) + ', but only '
          + size(storage.available_bytes) + ' is available after the free-space buffer and current uploads.');
      }
      return storage;
    }).catch(function (error) { unavailable(error); throw error; });
  }
  if (panel) {
    refresh();
    setInterval(function () { refresh(); }, 15000);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
    if (courseInput) courseInput.addEventListener('change', function () { refresh(); });
  }
  return { refresh: refresh, check: check };
})();
