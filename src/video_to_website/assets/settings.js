(function () {
  'use strict';
  var form = document.getElementById('reader-settings');
  var save = form.querySelector('[data-settings-save]');
  var reset = form.querySelector('[data-settings-reset]');
  var message = form.querySelector('[data-settings-message]');
  var controls = {};
  var dirty = false, revision = 0;
  Object.keys(V2WReaderState.defaults).forEach(function (key) {
    var control = form.elements.namedItem(key);
    if (control) controls[key] = control;
  });
  function fill() {
    var values = V2WReaderState.preferences();
    Object.keys(controls).forEach(function (key) {
      if (key === 'rate') controls[key].value = String(values[key]); else controls[key].checked = values[key];
    });
    revision = V2WReaderState.preferenceRevision();
  }
  function refresh() {
    save.disabled = reset.disabled = !V2WReaderState.writable();
    if (!dirty) fill();
  }
  form.addEventListener('change', function () { dirty = true; message.textContent = 'Unsaved changes'; });
  async function persist(values) {
    try {
      await V2WReaderState.edit([], values, revision);
      dirty = false; fill(); message.textContent = 'Settings saved.';
    } catch (e) {
      dirty = false; fill(); message.textContent = e.message;
    }
  }
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var values = {};
    Object.keys(controls).forEach(function (key) { values[key] = key === 'rate' ? Number(controls[key].value) : controls[key].checked; });
    persist(values);
  });
  reset.addEventListener('click', function () { persist(V2WReaderState.defaults); });
  document.getElementById('reading-reconnect').addEventListener('click', function () { V2WReaderState.refresh(); });
  V2WReaderState.subscribe(refresh); refresh();
})();
