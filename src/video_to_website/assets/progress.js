var V2WProgress = (function () {
  'use strict';
  function time(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    var minutes = Math.floor(seconds / 60);
    if (minutes >= 60) return Math.floor(minutes / 60) + 'h ' + minutes % 60 + 'm';
    return minutes ? minutes + 'm' + (seconds % 60 ? ' ' + seconds % 60 + 's' : '') : seconds + 's';
  }
  function clock(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
  }
  function describe(entry, now) {
    now = now === undefined ? Date.now() / 1000 : now;
    var p = entry.progress || {};
    var measured = typeof p.fraction === 'number' && isFinite(p.fraction) && p.fraction >= 0 && p.fraction <= 1
      && !(p.unit === 'sections' && p.total === 1 && p.completed < 1);
    var fraction = measured ? p.fraction : null;
    var percent = measured ? Math.round(fraction * 100) : null;
    var age = typeof p.updated === 'number' ? Math.max(0, now - p.updated) : 0;
    var stale = !!p.stale || age > 120;
    var label = p.label && p.phase !== 'starting' ? p.label : entry.label || 'Processing';
    var detail = [];
    if (typeof p.completed === 'number' && typeof p.total === 'number' && isFinite(p.completed) && isFinite(p.total) && p.total > 0) {
      if (p.unit === 'seconds') detail.push(clock(p.completed) + ' / ' + clock(p.total) + ' of video processed');
      if (p.unit === 'steps') detail.push(p.completed + ' of ' + p.total + ' steps illustrated');
      if (p.unit === 'sections') detail.push(p.completed + ' of ' + p.total + ' transcript sections completed');
    }
    if (p.detail) detail.push(p.detail);
    var estimate = 'Time remaining is not available yet.';
    if (stale) {
      estimate = 'No new progress report for ' + time(age) + '. Estimate paused.';
    } else if (measured && typeof p.eta_seconds === 'number' && isFinite(p.eta_seconds) && p.eta_seconds > 0) {
      var quantum = p.eta_seconds < 60 ? 5 : p.eta_seconds < 600 ? 30 : 60;
      var remaining = Math.ceil(p.eta_seconds / quantum) * quantum;
      var scope = { speech: 'transcription', audio: 'audio extraction', scenes: 'visual analysis',
        sections: 'instruction writing', assets: 'lesson media', clip: 'this clip', activity: 'motion analysis' }[p.phase] || 'this stage';
      estimate = 'About ' + time(remaining) + ' left for ' + scope;
    } else if (fraction === 1) {
      estimate = p.phase === 'cached' ? 'Already processed; using saved results.' : 'Finishing this stage…';
    } else if (p.unit === 'sections' && p.total === 1) {
      estimate = 'Writing length varies; no time estimate yet.';
    } else if (measured) {
      estimate = 'Estimating time remaining as progress arrives…';
    }
    var elapsed = entry.stage_started ? Math.max(0, now - entry.stage_started) : entry.elapsed || 0;
    return { label: label, text: label + (measured ? ' · ' + percent + '%' : ''),
      detail: detail.join(' · '), estimate: estimate, fraction: fraction, percent: percent,
      stale: stale, elapsed: time(elapsed), hasElapsed: elapsed > 0 };
  }
  return { describe: describe };
})();
