/* Course navigation also works on a static export: all lessons start in HTML. */
(function () {
  'use strict';
  document.querySelectorAll('.course-browser').forEach(function (root) {
    var rows = Array.from(root.querySelectorAll('[data-course-lesson]'));
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
    function makeGroup(chapter, occurrence) {
      var group = {rows: [], key: (chapter || 'other') + ':' + occurrence};
      group.label = chapter === '' ? 'Other lessons' : 'Chapter ' + chapter;
      if (occurrence > 1) group.label += ' (continued)';
      group.details = document.createElement('details');
      group.details.className = 'course-chapter';
      group.details.dataset.chapterKey = group.key;
      var summary = document.createElement('summary');
      summary.append(textNode('span', group.label));
      group.meta = textNode('span', '', 'chapter-meta');
      summary.append(group.meta);
      group.list = document.createElement('ol');
      group.list.className = 'lesson-list';
      group.details.append(summary, group.list);
      group.details.addEventListener('toggle', function () {
        // Programmatic opening during search must not erase the user's choices.
        if (!group.details.isConnected || search.value.trim() || group.details.open === group.expectedOpen) return;
        group.expectedOpen = group.details.open;
        opened[group.key] = group.details.open;
        save();
      });
      return group;
    }
    function filter() {
      var terms = search.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
      var visible = 0;
      rows.forEach(function (row) {
        var haystack = row.dataset.search.toLocaleLowerCase();
        row.hidden = !terms.every(function (term) { return haystack.includes(term); });
        if (!row.hidden) visible++;
      });
      groups.forEach(function (group) {
        var matches = group.rows.filter(function (row) { return !row.hidden; });
        group.details.hidden = !matches.length;
        group.meta.textContent = (terms.length ? matches.length + ' of ' : '') + lessonCount(group.rows.length) +
          ' · ' + duration(matches.reduce(function (sum, row) { return sum + Number(row.dataset.duration); }, 0));
        setOpen(group, terms.length ? matches.length > 0 : group.preferredOpen);
      });
      count.textContent = terms.length ? visible + ' of ' + lessonCount(rows.length) : lessonCount(rows.length);
      empty.textContent = rows.length ? 'No lessons match your search.' : 'No lessons published yet.';
      empty.hidden = visible !== 0;
      clear.hidden = !search.value;
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
        var occurrences = {};
        var previous;
        ordered.forEach(function (row) {
          var chapter = row.dataset.chapter;
          if (chapter !== previous) {
            occurrences[chapter] = (occurrences[chapter] || 0) + 1;
            groups.push(makeGroup(chapter, occurrences[chapter]));
            previous = chapter;
          }
          var group = groups[groups.length - 1];
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
            if (!search.value.trim() && group.details.isConnected) group.preferredOpen = group.details.open;
          });
          container.append(group.details);
        });
      }
      expand.hidden = collapse.hidden = !groups.length;
      filter();
    }
    search.addEventListener('input', filter);
    clear.addEventListener('click', function () { search.value = ''; filter(); search.focus(); });
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
          if (!search.value.trim()) { group.preferredOpen = value; opened[group.key] = value; }
        });
        save();
      });
    });
    root.dataset.density = density.value;
    rebuild();
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
