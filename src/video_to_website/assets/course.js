/* Course navigation also works on a static export: all lessons start in HTML. */
(function () {
  'use strict';
  document.querySelectorAll('.course-browser').forEach(function (root) {
    var rows = Array.from(root.querySelectorAll('[data-course-lesson]'));
    // Server HTML is grouped; the positions retain the original flat order.
    rows.sort(function (a, b) { return Number(a.dataset.position) - Number(b.dataset.position); });
    var container = root.querySelector('.course-groups');
    var search = root.querySelector('[data-course-search]');
    var sort = root.querySelector('[data-course-sort]');
    var view = root.querySelector('[data-course-view]');
    var density = root.querySelector('[data-course-density]');
    var count = root.querySelector('.course-results');
    var empty = root.querySelector('.course-empty');
    var clear = root.querySelector('[data-course-clear]');
    var expand = root.querySelector('[data-course-expand]');
    var collapse = root.querySelector('[data-course-collapse]');
    var progressFilter = root.querySelector('[data-course-progress]');
    var hideCompleted = root.querySelector('[data-course-hide-completed]');
    var manage = root.querySelector('[data-manage-progress]');
    var bulk = root.querySelector('[data-reading-bulk]');
    var selected = new Set(), managing = false, lastChanges = [];
    rows.forEach(function (row) {
      row.reading = JSON.parse(row.dataset.reading);
      if (!manage) return;
      var label = document.createElement('label'); label.className = 'reading-select';
      var box = document.createElement('input'); box.type = 'checkbox';
      box.setAttribute('aria-label', 'Select ' + row.dataset.title);
      label.append(box);
      row.prepend(label); row.selectionBox = box;
      box.addEventListener('change', function () {
        if (box.checked) selected.add(row); else selected.delete(row);
        selection();
      });
    });
    var hasChapters = rows.some(function (row) { return row.dataset.chapter !== ''; });
    var reader = root.dataset.context === 'reader';
    var key = 'v2w:course:' + root.dataset.course + ':navigation';
    var saved = {};
    try {
      var value = JSON.parse(localStorage.getItem(key));
      if (value && typeof value === 'object' && !Array.isArray(value)) saved = value;
    } catch (e) {}
    var opened = saved.opened && typeof saved.opened === 'object' && !Array.isArray(saved.opened) ? saved.opened : {};
    var groups = [];
    var collator = new Intl.Collator(undefined, {numeric: true, sensitivity: 'base'});
    function choice(value, allowed, fallback) { return allowed.includes(value) ? value : fallback; }
    sort.value = choice(saved.sort, ['saved', 'number', 'title', 'shortest'], 'saved');
    view.value = hasChapters ? choice(saved.view, ['chapters', 'flat'], 'chapters') : 'flat';
    view.disabled = !hasChapters;
    density.value = choice(saved[reader ? 'readerDensity' : 'density'], ['detailed', 'compact'], reader ? 'compact' : 'detailed');
    function save() {
      saved.sort = sort.value;
      saved.view = view.value;
      saved[reader ? 'readerDensity' : 'density'] = density.value;
      saved.opened = opened;
      try { localStorage.setItem(key, JSON.stringify(saved)); } catch (e) {}
    }
    function duration(seconds) {
      seconds = Math.max(0, Math.round(seconds));
      if (seconds < 60) return seconds + 's';
      var minutes = Math.floor(seconds / 60);
      return minutes < 60 ? minutes + 'm ' + String(seconds % 60).padStart(2, '0') + 's' :
        Math.floor(minutes / 60) + 'h ' + String(minutes % 60).padStart(2, '0') + 'm';
    }
    function lessonCount(n) { return n + (n === 1 ? ' lesson' : ' lessons'); }
    function selection() {
      if (!bulk) return;
      var hidden = rows.filter(function (row) { return row.hidden && selected.has(row); }).length;
      bulk.querySelector('[data-reading-selected]').textContent = selected.size + ' selected' + (hidden ? ' (' + hidden + ' hidden by filters)' : '');
      bulk.querySelector('[data-reading-complete]').disabled = bulk.querySelector('[data-reading-reset]').disabled = !selected.size || !V2WReaderState.writable();
      bulk.querySelector('[data-reading-undo]').disabled = !V2WReaderState.writable();
      bulk.querySelector('[data-reading-select-all]').disabled = !rows.some(function (row) { return !row.hidden; });
      rows.forEach(function (row) { row.dataset.selected = String(selected.has(row)); row.selectionBox.checked = selected.has(row); });
      groups.forEach(function (group) {
        if (!group.selectBox) return;
        var n = group.rows.filter(function (row) { return selected.has(row); }).length;
        group.selectBox.checked = n === group.rows.length;
        group.selectBox.indeterminate = n > 0 && n < group.rows.length;
      });
    }
    function progress() {
      rows.forEach(function (row) {
        row.querySelector('[data-lesson-progress]').textContent = V2WReading.label(row.reading);
        row.dataset.progress = V2WReading.stats(row.reading).status;
      });
      groups.forEach(function (group) {
        var lessons = group.rows.map(function (row) { return row.reading; });
        var state = V2WReading.aggregate(lessons);
        group.details.dataset.complete = String(state.total > 0 && state.done === state.total);
        group.progress.textContent = V2WReading.summary(lessons);
      });
      filter();
    }
    function textNode(tag, text, className) {
      var node = document.createElement(tag);
      node.textContent = text;
      if (className) node.className = className;
      return node;
    }
    function setOpen(group, value) {
      group.expectedOpen = value;
      group.details.open = value;
    }
    function makeGroup(chapter) {
      var group = {rows: [], key: (chapter || 'other') + ':1'};
      group.label = chapter === '' ? 'Other lessons' : 'Chapter ' + chapter;
      group.details = document.createElement('details');
      group.details.className = 'course-chapter';
      group.details.dataset.chapterKey = group.key;
      var summary = document.createElement('summary');
      summary.append(textNode('span', group.label));
      group.meta = textNode('span', '', 'chapter-meta');
      summary.append(group.meta);
      group.progress = textNode('span', '', 'chapter-reading');
      summary.append(group.progress);
      group.list = document.createElement('ol');
      group.list.className = 'lesson-list';
      group.details.append(summary);
      if (manage) {
        group.selection = document.createElement('label'); group.selection.className = 'chapter-selection';
        group.selectBox = document.createElement('input'); group.selectBox.type = 'checkbox';
        group.selectBox.setAttribute('aria-label', 'Select all lessons in ' + group.label);
        group.selection.append(group.selectBox, textNode('span', 'Select entire ' + group.label.toLocaleLowerCase() + ' (including filtered lessons)'));
        group.selection.hidden = !managing;
        group.selectBox.addEventListener('change', function () {
          group.rows.forEach(function (row) { if (group.selectBox.checked) selected.add(row); else selected.delete(row); });
          selection();
        });
        group.details.append(group.selection);
      }
      group.details.append(group.list);
      group.details.addEventListener('toggle', function () {
        // Programmatic opening during search must not erase the user's choices.
        if (!group.details.isConnected || search.value.trim() || progressFilter.value !== 'all' || group.details.open === group.expectedOpen) return;
        group.expectedOpen = group.details.open;
        opened[group.key] = group.details.open;
        save();
      });
      return group;
    }
    function filter() {
      var terms = search.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
      var state = progressFilter.value;
      hideCompleted.checked = state === 'unfinished';
      var filtered = terms.length || state !== 'all';
      var visible = 0;
      rows.forEach(function (row) {
        var haystack = row.dataset.search.toLocaleLowerCase();
        var matchesProgress = state === 'all' || (state === 'unfinished' ? row.dataset.progress !== 'complete' : row.dataset.progress === state);
        row.hidden = !matchesProgress || !terms.every(function (term) { return haystack.includes(term); });
        if (!row.hidden) visible++;
      });
      groups.forEach(function (group) {
        var matches = group.rows.filter(function (row) { return !row.hidden; });
        group.details.hidden = !matches.length;
        group.meta.textContent = (filtered ? matches.length + ' of ' : '') + lessonCount(group.rows.length) +
          ' · ' + duration(matches.reduce(function (sum, row) { return sum + Number(row.dataset.duration); }, 0));
        setOpen(group, filtered ? matches.length > 0 : group.preferredOpen);
      });
      count.textContent = filtered ? visible + ' of ' + lessonCount(rows.length) : lessonCount(rows.length);
      empty.textContent = rows.length ? 'No lessons match your search or progress filter.' : 'No lessons published yet.';
      empty.hidden = visible !== 0;
      clear.hidden = !search.value && state === 'all';
      selection();
    }
    function rebuild() {
      var ordered = rows.slice();
      if (sort.value !== 'saved') ordered.sort(function (a, b) {
        var result;
        if (sort.value === 'shortest') result = Number(a.dataset.duration) - Number(b.dataset.duration);
        else if (sort.value === 'number') {
          var aChapter = a.dataset.chapter === '' ? Infinity : Number(a.dataset.chapter);
          var bChapter = b.dataset.chapter === '' ? Infinity : Number(b.dataset.chapter);
          result = (aChapter === bChapter ? 0 : aChapter - bChapter) ||
            Number(a.dataset.lessonNumber) - Number(b.dataset.lessonNumber) || collator.compare(a.dataset.title, b.dataset.title);
        } else result = collator.compare(a.dataset.title, b.dataset.title);
        return result || Number(a.dataset.position) - Number(b.dataset.position);
      });
      container.replaceChildren();
      groups = [];
      if (view.value === 'flat') {
        var list = document.createElement('ol');
        list.className = 'lesson-list';
        ordered.forEach(function (row) { list.append(row); });
        container.append(list);
      } else {
        var byChapter = new Map();
        ordered.forEach(function (row) {
          var chapter = row.dataset.chapter;
          if (!byChapter.has(chapter)) {
            var created = makeGroup(chapter);
            byChapter.set(chapter, created);
            groups.push(created);
          }
          var group = byChapter.get(chapter);
          group.rows.push(row);
          group.list.append(row);
        });
        groups.forEach(function (group, index) {
          var current = group.rows.some(function (row) { return row.dataset.current === 'true'; });
          group.preferredOpen = reader && current ? true :
            (typeof opened[group.key] === 'boolean' ? opened[group.key] : (!reader && index === 0));
          // Update the preferred state on native user toggles as well as the
          // stored value, so clearing a search restores the in-page selection.
          group.details.addEventListener('toggle', function () {
            if (!search.value.trim() && progressFilter.value === 'all' && group.details.isConnected) group.preferredOpen = group.details.open;
          });
          container.append(group.details);
        });
      }
      expand.hidden = collapse.hidden = !groups.length;
      progress();
    }
    search.addEventListener('input', filter);
    clear.textContent = 'Clear filters';
    clear.addEventListener('click', function () { search.value = ''; progressFilter.value = 'all'; filter(); search.focus(); });
    progressFilter.value = 'all';
    progressFilter.addEventListener('change', filter);
    hideCompleted.addEventListener('change', function () {
      progressFilter.value = hideCompleted.checked ? 'unfinished' : 'all';
      filter();
    });
    if (manage) {
      manage.addEventListener('click', function () {
        managing = !managing; root.dataset.managing = String(managing); bulk.hidden = !managing;
        manage.setAttribute('aria-expanded', String(managing));
        manage.textContent = managing ? 'Finish managing' : 'Manage progress';
        groups.forEach(function (group) { group.selection.hidden = !managing; });
        if (!managing) selected.clear();
        selection();
      });
      bulk.querySelector('[data-reading-select-all]').addEventListener('click', function () {
        rows.forEach(function (row) { if (!row.hidden) selected.add(row); }); selection();
      });
      bulk.querySelector('[data-reading-deselect]').addEventListener('click', function () { selected.clear(); selection(); });
      async function apply(value) {
        var message = bulk.querySelector('[data-reading-message]');
        try {
          var result = await V2WReading.mark(rows.filter(function (row) { return selected.has(row); }).map(function (row) { return row.reading; }), value);
          lastChanges = result.changes;
          bulk.querySelector('[data-reading-undo]').hidden = !lastChanges.length;
          message.textContent = V2WReading.report(result, value ? 'Completed' : 'Reset');
          selected.clear(); selection();
        } catch (e) { message.textContent = e.message; }
      }
      bulk.querySelector('[data-reading-complete]').addEventListener('click', function () { apply(true); });
      bulk.querySelector('[data-reading-reset]').addEventListener('click', function () { apply(false); });
      bulk.querySelector('[data-reading-undo]').addEventListener('click', async function () {
        try {
          bulk.querySelector('[data-reading-message]').textContent = V2WReading.report(await V2WReading.undo(lastChanges), 'Restored');
          lastChanges = []; bulk.querySelector('[data-reading-undo]').hidden = true;
        } catch (e) { bulk.querySelector('[data-reading-message]').textContent = e.message; }
      });
    }
    [sort, view].forEach(function (control) {
      control.addEventListener('change', function () { rebuild(); save(); });
    });
    density.addEventListener('change', function () { root.dataset.density = density.value; save(); });
    [expand, collapse].forEach(function (button) {
      button.addEventListener('click', function () {
        groups.forEach(function (group) {
          if (group.details.hidden) return;
          var value = button === expand;
          setOpen(group, value);
          if (!search.value.trim() && progressFilter.value === 'all') { group.preferredOpen = value; opened[group.key] = value; }
        });
        save();
      });
    });
    root.dataset.density = density.value;
    rebuild();
    V2WReading.subscribe(progress);
    root.querySelectorAll('[data-course-controls]').forEach(function (node) { node.hidden = false; });
    var contents = root.closest('.course-contents');
    if (contents) contents.addEventListener('toggle', function () {
      if (!contents.open || search.value.trim()) return;
      var current = rows.find(function (row) { return row.dataset.current === 'true'; });
      if (!current || current.hidden || !current.getClientRects().length) return;
      // Scroll the bounded menu, leaving the reader's page position alone.
      var rowBounds = current.getBoundingClientRect();
      var bounds = container.getBoundingClientRect();
      if (rowBounds.top < bounds.top || rowBounds.bottom > bounds.bottom) {
        container.scrollTop += rowBounds.top - bounds.top - Math.max(0, (container.clientHeight - rowBounds.height) / 3);
      }
    });
  });
}());
