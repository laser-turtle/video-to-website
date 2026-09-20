/* Home-page activity belongs to courses; individual jobs live in the queue. */
(function () {
  'use strict';
  var list = document.getElementById('course-cards');
  if (!list) return;
  var cards = Array.from(list.querySelectorAll('[data-course-slug]'));
  var count = document.getElementById('course-count');
  var initialCount = count ? count.textContent : '';
  var pending = new Map();
  var builtAtLoad = null;
  var polling = false;

  function queueLink(group) {
    var params = new URLSearchParams();
    if (group.id || group.slug) params.set('course', group.id || group.slug);
    else params.set('q', group.title);
    if (group.failed) params.set('state', group.working || group.queued ? 'all' : 'failed');
    return 'queue.html?' + params.toString();
  }
  function node(tag, className, text) {
    var element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function clear() {
    cards.forEach(function (card) { card.querySelector('.course-activity').hidden = true; });
    pending.forEach(function (card) { card.remove(); });
    pending.clear();
    if (count) count.textContent = initialCount;
  }
  function render(data) {
    var groups = new Map();
    data.videos.forEach(function (video) {
      if (!['working', 'queued', 'failed'].includes(video.state)) return;
      var key = video.course_id ? 'id:' + video.course_id : video.course_slug ? 'slug:' + video.course_slug : 'title:' + video.course;
      if (!groups.has(key)) groups.set(key, {
        id: video.course_id || '', slug: video.course_slug || '', title: video.course || 'Untitled course',
        working: 0, queued: 0, failed: 0
      });
      groups.get(key)[video.state]++;
    });
    cards.forEach(function (card) { card.querySelector('.course-activity').hidden = true; });
    var remaining = new Set(pending.keys());
    groups.forEach(function (group, key) {
      // Titles are only a fallback for old CLI status files without stable IDs.
      var card = cards.find(function (item) {
        if (group.id && item.dataset.courseId) return group.id === item.dataset.courseId;
        if (group.slug) return group.slug === item.dataset.courseSlug;
        return group.title === item.dataset.courseTitle;
      });
      var href = queueLink(group);
      if (!card) {
        remaining.delete(key);
        card = pending.get(key);
        if (!card) {
          card = node('li', 'course-pending');
          var main = node('a', 'course-card-main');
          var copy = node('div', '');
          copy.appendChild(node('div', 't'));
          copy.appendChild(node('div', 's', 'No lessons ready yet'));
          main.appendChild(copy);
          card.appendChild(main);
          card.appendChild(node('a', 'course-activity'));
          list.appendChild(card);
          pending.set(key, card);
        }
        card.querySelector('.t').textContent = group.title;
        card.querySelector('.course-card-main').href = href;
      }
      var parts = [];
      if (group.working) parts.push(group.working + (group.working === 1 ? ' video processing' : ' videos processing'));
      if (group.queued) parts.push(group.queued + ' waiting');
      if (group.failed) parts.push(group.failed + ' failed');
      var activity = card.querySelector('.course-activity');
      var text = parts.join(' · ');
      if (activity.textContent !== text) activity.textContent = text;
      activity.href = href;
      activity.setAttribute('aria-label', group.title + ': ' + text + '. View task queue');
      activity.hidden = false;
    });
    remaining.forEach(function (key) { pending.get(key).remove(); pending.delete(key); });
    if (count) count.textContent = pending.size ? (cards.length + pending.size) + ' courses' : initialCount;
  }
  function poll() {
    if (polling || document.hidden) return;
    polling = true;
    fetch('status.json?t=' + Date.now(), {cache: 'no-store'}).then(function (response) {
      if (!response.ok) throw new Error('Status unavailable');
      return response.json();
    }).then(function (data) {
      if (!data || !Array.isArray(data.videos)) throw new Error('Invalid status');
      if (builtAtLoad === null) builtAtLoad = data.built || 0;
      else if ((data.built || 0) > builtAtLoad) {
        builtAtLoad = data.built;
        location.reload();
        return;
      }
      render(data);
    }).catch(clear).finally(function () { polling = false; });
  }
  poll();
  setInterval(poll, 3000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
}());
