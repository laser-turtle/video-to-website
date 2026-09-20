(function () {
  'use strict';
  var list = document.getElementById('library-courses');
  var search = document.getElementById('library-search');
  var message = document.getElementById('library-message');
  var count = document.getElementById('library-count');
  var refresh = document.getElementById('library-refresh');
  var reset = document.getElementById('library-reset');
  var sortCourses = document.getElementById('library-sort-courses');
  var grouping = document.getElementById('library-group');
  var density = document.getElementById('library-density');
  var empty = document.getElementById('library-empty');
  if (!list || !search) return;

  var catalog = { courses: [] };
  var editable = false;
  var busy = false;
  var editor = null;
  var preferences = {};
  try {
    var stored = JSON.parse(localStorage.getItem('v2w:library:view'));
    if (stored && typeof stored === 'object' && !Array.isArray(stored)) preferences = stored;
  } catch (e) {}
  function object(value) { return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; }
  var expanded = object(preferences.expanded);
  var chapterOpen = object(preferences.chapters);
  var sortMethods = {};
  grouping.value = preferences.grouping === 'flat' ? 'flat' : 'chapters';
  density.value = preferences.density === 'compact' ? 'compact' : 'detailed';
  var controls = [];
  var focusTargets = {};
  var selectedCourse = new URLSearchParams(location.search).get('course');
  if (selectedCourse) expanded[selectedCourse] = true;
  var labels = { ready: 'Ready', running: 'Processing', queued: 'Waiting', failed: 'Failed', cancelled: 'Cancelled', superseded: 'Waiting' };
  var sorts = [
    ['heuristic', 'Name (smart numbering)', 'Sort chapter and lesson prefixes numerically, then remaining names. Renamed lessons can use their original filename numbering.'],
    ['title', 'Displayed title A–Z', 'Sort the displayed titles naturally, with 2 before 10.'],
    ['filename', 'Filename A–Z', 'Sort original filenames naturally, across all folders in this course.'],
    ['shortest', 'Shortest first', 'Sort by video duration. Lessons whose duration is still unknown go last.'],
    ['longest', 'Longest first', 'Sort by video duration. Lessons whose duration is still unknown go last.']
  ];

  function remember() {
    try { localStorage.setItem('v2w:library:view', JSON.stringify({
      grouping: grouping.value, density: density.value, expanded: expanded, chapters: chapterOpen
    })); } catch (e) {}
  }
  function chapter(video) { return Array.isArray(video.numbering) ? video.numbering[0] : null; }
  function chapterGroups(videos) {
    var groups = [], occurrences = {};
    videos.forEach(function (video) {
      var number = chapter(video);
      var previous = groups[groups.length - 1];
      if (!previous || previous.number !== number) {
        var occurrence = occurrences[number] = (occurrences[number] || 0) + 1;
        previous = {number: number, videos: [], key: (number === null ? 'other' : number) + ':' + occurrence,
          label: (number === null ? 'Other lessons' : 'Chapter ' + number) + (occurrence > 1 ? ' (continued)' : '')};
        groups.push(previous);
      }
      previous.videos.push(video);
    });
    return groups;
  }
  function duration(seconds) {
    if (seconds === null || seconds === undefined) return 'Duration pending';
    seconds = Math.max(0, Math.round(seconds));
    return seconds < 60 ? seconds + 's' : Math.floor(seconds / 60) + 'm ' + String(seconds % 60).padStart(2, '0') + 's';
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function say(text, bad) {
    message.textContent = text;
    message.hidden = !text;
    message.className = bad ? 'library-message bad' : 'library-message';
  }
  function setBusy(value) {
    busy = value;
    search.disabled = value;
    refresh.disabled = value;
    reset.disabled = value || !editable || !!search.value.trim() || !catalog.courses.length;
    sortCourses.disabled = value || !editable || !!search.value.trim() || !!editor || catalog.courses.length < 2;
    grouping.disabled = density.disabled = value || !!editor;
    controls.forEach(function (button) { button.disabled = value || !!button.unavailable; });
  }
  function button(text, key, callback, unavailable) {
    var node = el('button', '', text);
    node.type = 'button';
    node.unavailable = !!unavailable;
    node.dataset.focusKey = key;
    node.addEventListener('click', callback);
    controls.push(node);
    focusTargets[key] = node;
    return node;
  }
  function link(text, href) {
    var node = el('a', '', text);
    node.href = href;
    return node;
  }
  function json(url, init) {
    return fetch(url, Object.assign({ cache: 'no-store' }, init)).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) throw new Error(body.error || 'Could not update the library (' + res.status + ').');
        return body;
      });
    });
  }
  function mutate(path, body, success, focusKey) {
    if (busy || !editable) return;
    setBusy(true);
    say('Saving…');
    json('api/catalog/' + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ revision: catalog.revision }, body))
    }).then(function (result) {
      catalog = result.catalog;
      editor = null;
      busy = false;
      render(focusKey);
      say(result.warning || success, !!result.warning);
    }).catch(function (err) {
      setBusy(false);
      say(err.message, true);
    });
  }
  function orderPath(course) { return course ? 'courses/' + course.id + '/order' : 'courses/order'; }
  function reorder(items, from, to, course, focusKey) {
    if (from === to) { editor = null; render(focusKey); return; }
    var ids = items.map(function (item) { return item.id; });
    ids.splice(to, 0, ids.splice(from, 1)[0]);
    mutate(orderPath(course), { ids: ids }, 'Order saved.', focusKey);
  }
  function editForm(item, kind, items, course) {
    var form = el('form', 'library-edit');
    var input = el('input');
    var moving = editor.action === 'move';
    input.type = moving ? 'number' : 'text';
    input.value = moving ? items.indexOf(item) + 1 : item.title;
    input.required = true;
    controls.push(input);
    if (moving) { input.min = '1'; input.max = String(items.length); }
    else input.maxLength = 240;
    input.setAttribute('aria-label', moving ? 'Position for ' + item.title : 'Title for ' + item.title);
    var label = el('label', '', moving ? 'Position (1–' + items.length + ')' : 'Displayed title');
    label.appendChild(input);
    form.appendChild(label);
    var save = button('Save', 'edit-save', function () { submit(); });
    var cancel = button('Cancel', 'edit-cancel', function () {
      editor = null; render(kind + '-' + item.id + '-rename');
    });
    form.appendChild(save);
    form.appendChild(cancel);
    if (!moving && kind === 'lesson' && item.custom_title) {
      form.appendChild(button('Use filename', 'edit-reset', function () {
        mutate('lessons/' + item.id + '/title', { title: null }, 'Filename restored as the title.', kind + '-' + item.id + '-rename');
      }));
    }
    function submit() {
      if (moving) {
        var position = Number(input.value);
        if (!Number.isInteger(position) || position < 1 || position > items.length) {
          say('Choose a position from 1 to ' + items.length + '.', true); input.focus(); return;
        }
        reorder(items, items.indexOf(item), position - 1, course, kind + '-' + item.id + '-rename');
      } else {
        var title = input.value.trim();
        if (!title || title.length > 240) { say('Use a title between 1 and 240 characters.', true); input.focus(); return; }
        if (title === item.title) { editor = null; render(kind + '-' + item.id + '-rename'); say('Title unchanged.'); return; }
        mutate((kind === 'course' ? 'courses/' : 'lessons/') + item.id + '/title', { title: title }, 'Title saved.', kind + '-' + item.id + '-rename');
      }
    }
    form.addEventListener('submit', function (event) { event.preventDefault(); submit(); });
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { event.preventDefault(); cancel.click(); }
    });
    form.appendChild(el('p', 'library-hint', moving ? 'Changes reading order; processing keeps its current queue.'
      : 'Source: ' + (item.source_name || item.source_path)));
    // Focus after the form has been inserted into the document.
    setTimeout(function () { input.focus(); if (!moving) input.select(); }, 0);
    return form;
  }
  function actions(item, kind, items, course) {
    var area = el('div', 'library-actions');
    var prefix = kind + '-' + item.id;
    if (item.href) area.appendChild(link(kind === 'course' ? 'Open course' : 'Open lesson', item.href));
    if (!editable) return area;
    area.appendChild(button('Rename', prefix + '-rename', function () {
      editor = { id: item.id, action: 'title' }; render();
    }));
    var index = items.indexOf(item);
    var filtered = !!search.value.trim();
    var up = button('↑', prefix + '-up', function () { reorder(items, index, index - 1, course, prefix + '-rename'); }, filtered || index === 0);
    var down = button('↓', prefix + '-down', function () { reorder(items, index, index + 1, course, prefix + '-rename'); }, filtered || index === items.length - 1);
    up.setAttribute('aria-label', 'Move ' + item.title + ' up');
    down.setAttribute('aria-label', 'Move ' + item.title + ' down');
    area.appendChild(up); area.appendChild(down);
    area.appendChild(button('Move to…', prefix + '-move', function () {
      editor = { id: item.id, action: 'move' }; render();
    }, filtered || items.length < 2));
    return area;
  }
  function sorting(course, filtered) {
    var area = el('div', 'managed-sort');
    var prefix = 'course-' + course.id + '-sort';
    var label = el('label', '', 'Sort lessons');
    var select = el('select');
    select.dataset.focusKey = prefix;
    focusTargets[prefix] = select;
    select.unavailable = filtered || !!editor || course.videos.length < 2;
    controls.push(select);
    sorts.forEach(function (sort) { var option = el('option', '', sort[1]); option.value = sort[0]; select.appendChild(option); });
    select.value = sortMethods[course.id] || 'heuristic';
    var hint = el('p', 'library-hint');
    hint.id = prefix + '-hint';
    select.setAttribute('aria-describedby', hint.id);
    function describe() { hint.textContent = sorts.find(function (sort) { return sort[0] === select.value; })[2]; }
    select.addEventListener('change', function () { sortMethods[course.id] = select.value; describe(); });
    describe();
    label.appendChild(select); area.appendChild(label);
    area.appendChild(button('Save sorted order', prefix + '-apply', function () {
      mutate(orderPath(course), {mode: select.value}, 'Reading order saved for ' + course.title + '.', prefix + '-apply');
    }, filtered || !!editor || course.videos.length < 2));
    area.appendChild(button('Reset to source order', 'course-' + course.id + '-reset', function () {
      mutate(orderPath(course), {mode: 'source'}, 'Folder and filename order restored.', 'course-' + course.id + '-reset');
    }, filtered || !!editor));
    area.appendChild(hint);
    return area;
  }
  function lessonRow(video, course) {
    var row = el('li', 'managed-lesson');
    row.dataset.lessonId = video.id;
    row.appendChild(el('span', 'library-number', String(course.videos.indexOf(video) + 1)));
    var copy = el('div', 'managed-copy');
    copy.appendChild(el('h3', '', video.title));
    if (video.description && video.description !== video.title) copy.appendChild(el('p', 'lesson-description', video.description));
    var sourcePath = video.source_path || video.source_name || video.title;
    var prefix = course.source_path + '/';
    if (sourcePath.indexOf(prefix) === 0) sourcePath = sourcePath.slice(prefix.length);
    copy.appendChild(el('p', 'library-hint', sourcePath + ' · ' + (labels[video.state] || video.state) + ' · ' + duration(video.duration)));
    row.appendChild(copy);
    row.appendChild(actions(video, 'lesson', course.videos, course));
    if (editor && editor.id === video.id) row.appendChild(editForm(video, 'lesson', course.videos, course));
    return row;
  }
  function render(focusKey) {
    var focused = document.activeElement;
    focusKey = focusKey || (focused && focused.dataset ? focused.dataset.focusKey : null);
    controls = []; focusTargets = {};
    list.textContent = '';
    list.dataset.density = density.value;
    var query = search.value.trim().toLowerCase();
    var total = catalog.courses.reduce(function (n, course) { return n + course.videos.length; }, 0);
    count.textContent = catalog.courses.length + ' courses · ' + total + ' lessons';
    catalog.courses.forEach(function (course, index) {
      var courseMatches = [course.title, course.source_path].join(' ').toLowerCase().includes(query);
      var videos = course.videos.filter(function (video) {
        return courseMatches || [video.title, video.source_name, video.source_path, video.description,
          chapter(video) === null ? 'Other lessons' : 'Chapter ' + chapter(video)].join(' ').toLowerCase().includes(query);
      });
      if (query && !videos.length) return;
      if (!Object.prototype.hasOwnProperty.call(expanded, course.id)) expanded[course.id] = selectedCourse ? selectedCourse === course.id : index === 0;
      var open = query || expanded[course.id];
      var section = el('section', 'managed-course');
      var heading = el('div', 'managed-heading');
      var title = el('h2');
      var toggle = button((open ? '▾ ' : '▸ ') + course.title, 'course-' + course.id + '-toggle', function () {
        expanded[course.id] = !expanded[course.id]; remember(); editor = null; render();
      });
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-controls', 'lessons-' + course.id);
      title.appendChild(toggle); heading.appendChild(title);
      heading.appendChild(el('span', 'library-hint', course.videos.length + ' lessons'));
      heading.appendChild(actions(course, 'course', catalog.courses, null));
      section.appendChild(heading);
      if (editor && editor.id === course.id) section.appendChild(editForm(course, 'course', catalog.courses, null));
      var contents = el('div', 'managed-contents');
      contents.id = 'lessons-' + course.id;
      contents.hidden = !open;
      var note = el('div', 'managed-order');
      note.appendChild(el('span', 'library-hint', query ? 'Clear search to reorder.' : 'Lessons appear in this order.'));
      var groups = chapterGroups(course.videos);
      var grouped = grouping.value === 'chapters' && groups.some(function (group) { return group.number !== null; });
      if (grouped) [true, false].forEach(function (opening) {
        note.appendChild(button(opening ? 'Expand chapters' : 'Collapse chapters', 'course-' + course.id + (opening ? '-expand' : '-collapse'), function () {
          groups.forEach(function (group) { chapterOpen[course.id + '|' + group.key] = opening; });
          remember(); render();
        }, !!query || !!editor));
      });
      if (editable) contents.appendChild(sorting(course, !!query));
      contents.appendChild(note);
      var visible = new Set(videos.map(function (video) { return video.id; }));
      (grouped ? groups : [{videos: course.videos}]).forEach(function (group, groupIndex) {
        var matches = group.videos.filter(function (video) { return visible.has(video.id); });
        if (!matches.length) return;
        var lessons = el('ol', 'managed-lessons');
        matches.forEach(function (video) { lessons.appendChild(lessonRow(video, course)); });
        if (!grouped) { contents.appendChild(lessons); return; }
        var details = el('details', 'managed-chapter');
        var groupKey = course.id + '|' + group.key;
        details.dataset.chapterKey = groupKey;
        var reveal = matches.some(function (video) {
          return (editor && editor.id === video.id) || (focusKey && focusKey.indexOf('lesson-' + video.id + '-') === 0);
        });
        details.open = !!query || reveal || (typeof chapterOpen[groupKey] === 'boolean' ? chapterOpen[groupKey] : groupIndex === 0);
        details.addEventListener('toggle', function () {
          if (!query && details.isConnected) { chapterOpen[groupKey] = details.open; remember(); }
        });
        var summary = el('summary', '', group.label);
        summary.appendChild(el('span', 'chapter-meta', (query ? matches.length + ' of ' : '') + group.videos.length + (group.videos.length === 1 ? ' lesson' : ' lessons')));
        details.appendChild(summary); details.appendChild(lessons); contents.appendChild(details);
      });
      section.appendChild(contents); list.appendChild(section);
    });
    empty.hidden = list.children.length > 0;
    empty.textContent = query ? 'No courses or lessons match your search.' : 'Your library is empty. Add videos to start a course.';
    setBusy(busy);
    if (focusKey && focusTargets[focusKey]) focusTargets[focusKey].focus({ preventScroll: true });
  }
  function load() {
    if (busy) return;
    editor = null;
    setBusy(true);
    json('api/catalog').then(function (data) {
      if (!Array.isArray(data.courses) || typeof data.revision !== 'string') throw new Error('Invalid catalog');
      editable = true; catalog = data; say('');
    }).catch(function () {
      editable = false;
      return json('site.json').then(function (courses) {
        if (!Array.isArray(courses)) throw new Error('Library unavailable');
        catalog = { courses: courses.map(function (course) {
          return { id: course.id || course.slug, title: course.title, source_path: course.title,
            href: course.slug + '/index.html', videos: course.lessons.map(function (lesson) {
              var sourceTitle = lesson.source_name ? lesson.source_name.replace(/\.[^.]+$/, '') : lesson.title;
              return { id: lesson.id || lesson.slug, title: lesson.display_title || sourceTitle,
                description: lesson.description || lesson.title, source_name: lesson.source_name,
                numbering: lesson.numbering, duration: lesson.duration,
                state: 'ready', href: course.slug + '/' + lesson.slug + '.html' };
            }) };
        }) };
        say('Read-only library. Editing is currently unavailable; connect to the durable service to rename and reorder.');
      });
    }).then(function () { busy = false; render(); }).catch(function () {
      setBusy(false); say('The library is unavailable. Try Refresh.', true);
    });
  }
  refresh.addEventListener('click', load);
  sortCourses.addEventListener('click', function () { mutate('courses/order', {mode: 'title'}, 'Courses sorted by name.', null); });
  [grouping, density].forEach(function (control) {
    control.addEventListener('change', function () { remember(); render(); });
  });
  reset.addEventListener('click', function () { mutate('courses/order', { mode: 'source' }, 'Folder order restored.', null); });
  search.addEventListener('input', function () { editor = null; render(); });
  load();
})();
