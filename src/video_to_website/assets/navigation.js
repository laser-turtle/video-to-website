/* Keyboard navigation follows published chapter order, independent of view sorts. */
(function () {
  'use strict';
  var root = document.querySelector('.course-browser');
  var dialog = document.getElementById('lesson-picker');
  var search = document.getElementById('lesson-picker-search');
  var results = document.getElementById('lesson-picker-results');
  var count = document.getElementById('lesson-picker-count');
  var launch = document.getElementById('lesson-picker-open');
  var closeButton = document.getElementById('lesson-picker-close');
  var status = document.getElementById('lesson-navigation-status');
  if (!root || !dialog || !search || !results) return;

  function normalize(text) {
    return String(text || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  }
  var lessons = Array.from(root.querySelectorAll('[data-course-lesson]')).map(function (row) {
    var link = row.querySelector('a');
    var description = row.querySelector('.lesson-description');
    return {title: row.dataset.title, description: description ? description.textContent : '',
      text: normalize(row.dataset.search), titleText: normalize(row.dataset.title),
      chapter: row.dataset.chapter || '', href: link.getAttribute('href'), current: row.dataset.current === 'true',
      position: Number(row.dataset.readingPosition || row.dataset.position)};
  }).sort(function (a, b) { return a.position - b.position; });
  var current = lessons.findIndex(function (lesson) { return lesson.current; });
  var chapters = [];
  lessons.forEach(function (lesson) { if (!chapters.some(function (entry) { return entry.chapter === lesson.chapter; })) chapters.push(lesson); });
  var currentChapter = current < 0 ? -1 : chapters.findIndex(function (entry) { return entry.chapter === lessons[current].chapter; });
  var matches = [], active = 0, links = [], previousFocus = null;

  function chapterLabel(lesson) { return lesson.chapter === '' ? 'Other lessons' : 'Chapter ' + lesson.chapter; }
  function go(lesson, edge) {
    if (!lesson) { if (status) status.textContent = edge; return; }
    if (lesson.current) { close(); return; }
    location.assign(lesson.href);
  }
  function fuzzyScore(token, lesson) {
    var exact = lesson.titleText.indexOf(token);
    if (exact >= 0) return exact / 100;
    var elsewhere = lesson.text.indexOf(token);
    if (elsewhere >= 0) return 10 + elsewhere / 100;
    // Ordered letters allow abbreviations and omitted letters without ranking
    // accidental matches across a long generated description above real titles.
    var at = -1, start = -1;
    for (var letter of token) {
      at = lesson.titleText.indexOf(letter, at + 1);
      if (at < 0) return Infinity;
      if (start < 0) start = at;
    }
    return 30 + (at - start + 1 - token.length) / 2 + start / 100;
  }
  function select(index) {
    active = Math.max(0, Math.min(index, matches.length - 1));
    links.forEach(function (link, at) { link.setAttribute('aria-selected', String(at === active)); });
    if (links[active]) {
      search.setAttribute('aria-activedescendant', links[active].id);
      var bounds = results.getBoundingClientRect(), item = links[active].getBoundingClientRect();
      if (item.top < bounds.top) results.scrollTop += item.top - bounds.top;
      else if (item.bottom > bounds.bottom) results.scrollTop += item.bottom - bounds.bottom;
    } else search.removeAttribute('aria-activedescendant');
  }
  function draw(initial) {
    var query = normalize(search.value).trim();
    var chapterQuery = /\bchapter\s+([0-9]+)\b/.exec(query);
    var terms = (chapterQuery ? query.replace(chapterQuery[0], '') : query).split(/\s+/).filter(Boolean);
    matches = lessons.filter(function (lesson) { return !chapterQuery || lesson.chapter === String(Number(chapterQuery[1])); }).map(function (lesson) {
      return {lesson: lesson, score: terms.reduce(function (total, token) { return total + fuzzyScore(token, lesson); }, 0)};
    }).filter(function (entry) { return Number.isFinite(entry.score); })
      .sort(function (a, b) { return a.score - b.score || a.lesson.position - b.lesson.position; })
      .map(function (entry) { return entry.lesson; });
    results.replaceChildren(); links = [];
    matches.forEach(function (lesson, index) {
      var link = document.createElement('a');
      link.href = lesson.href; link.id = 'lesson-choice-' + index; link.tabIndex = -1;
      link.setAttribute('role', 'option');
      var title = document.createElement('span'); title.className = 'picker-title'; title.textContent = lesson.title;
      var meta = document.createElement('span'); meta.className = 'picker-meta';
      meta.textContent = chapterLabel(lesson) + (lesson.current ? ' · Current lesson' : '');
      link.appendChild(title); link.appendChild(meta);
      if (lesson.description) {
        var description = document.createElement('span'); description.className = 'picker-description';
        description.textContent = lesson.description; link.appendChild(description);
      }
      link.addEventListener('click', function (event) {
        if (lesson.current && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
          event.preventDefault(); close();
        }
      });
      results.appendChild(link); links.push(link);
    });
    count.textContent = matches.length ? matches.length + (matches.length === 1 ? ' lesson' : ' lessons') : 'No lessons match. Try fewer letters.';
    select(initial ? Math.max(0, matches.findIndex(function (lesson) { return lesson.current; })) : 0);
  }
  function open() {
    if (dialog.open) { search.focus(); return; }
    if (document.activeElement !== search && document.activeElement !== closeButton && document.activeElement !== dialog) previousFocus = document.activeElement;
    search.value = '';
    dialog.showModal();
    draw(true); search.focus();
  }
  function close() { if (dialog.open) dialog.close(); }
  if (launch) { launch.hidden = false; launch.addEventListener('click', open); }
  closeButton.addEventListener('click', close);
  search.addEventListener('input', function () { draw(false); });
  dialog.addEventListener('cancel', function (event) { event.preventDefault(); close(); });
  dialog.addEventListener('close', function () {
    // Native close events are deferred; a quick reopen must keep input focus.
    if (dialog.open) return;
    if (previousFocus && previousFocus.isConnected && previousFocus.focus) previousFocus.focus({preventScroll: true});
  });
  dialog.addEventListener('click', function (event) {
    if (event.target !== dialog) return;
    var bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) close();
  });
  document.addEventListener('keydown', function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey || event.isComposing) return;
    if (dialog.open) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); select(active + (event.key === 'ArrowDown' ? 1 : -1)); search.focus();
      } else if (event.key === 'Enter' && event.target !== closeButton && !['BUTTON', 'A'].includes((event.target || {}).tagName)) {
        event.preventDefault(); if (!event.repeat && matches[active]) go(matches[active]);
      } else if (event.key === 'Escape') { event.preventDefault(); close(); }
      return;
    }
    var target = event.target || {};
    if ((target !== search && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) || target.isContentEditable || event.repeat) return;
    var help = document.getElementById('shortcuts'), lightbox = document.getElementById('lightbox');
    if ((help && !help.hidden) || (lightbox && !lightbox.hidden)) return;
    if (event.key === 'b' || event.key === '/') { event.preventDefault(); open(); return; }
    if (!event.shiftKey || current < 0) return;
    switch (event.key) {
      case 'D':
      case 'J': event.preventDefault(); go(lessons[current + 1], 'You are at the last lesson.'); return;
      case 'E':
      case 'K': event.preventDefault(); go(lessons[current - 1], 'You are at the first lesson.'); return;
      case 'W':
      case 'L': event.preventDefault(); go(chapters[currentChapter + 1], 'You are at the last chapter.'); return;
      case 'Q':
      case 'H': event.preventDefault(); go(chapters[currentChapter - 1], 'You are at the first chapter.'); return;
    }
  });
}());
