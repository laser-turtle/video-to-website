/* Run on the course's download page. Click the bookmark again to stop. */
(async () => {
  const key = '__v2wChapterZips';
  if (window[key]) { window[key].abort(); return; }

  const seen = new Set();
  const files = [...document.querySelectorAll('a[data-x-origin-download-name][href]')]
    .map(a => ({ name: a.getAttribute('data-x-origin-download-name').trim(), url: a.href }))
    .filter(file => /^\d+_.+_videos_1080p\.zip$/i.test(file.name) && /^https?:\/\//i.test(file.url))
    .filter(file => !seen.has(file.url) && seen.add(file.url))
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  if (!files.length) { alert('No chapter video ZIPs found. Open the course download page first.'); return; }

  const input = prompt('Found ' + files.length + ' chapter ZIPs (1080p).\nDownload one at a time, starting at chapter number:', '1');
  if (input === null) return;
  const first = Number(input);
  const queue = files.filter(file => parseInt(file.name, 10) >= first);
  if (!Number.isInteger(first) || first < 1 || !queue.length) { alert('Enter a chapter number from the page.'); return; }

  document.getElementById('v2w-chapter-zips')?.remove();
  const panel = document.createElement('div');
  panel.id = 'v2w-chapter-zips';
  panel.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:2147483647;box-sizing:border-box;width:420px;max-width:calc(100vw - 40px);padding:16px;background:#fff;color:#222;border:1px solid #888;border-radius:8px;box-shadow:0 3px 16px #0003;font:14px/1.5 sans-serif;overflow-wrap:anywhere';
  const status = document.createElement('div');
  status.setAttribute('role', 'status');
  const bar = document.createElement('progress');
  bar.max = 100; bar.setAttribute('aria-label', 'Current ZIP download progress');
  bar.style.cssText = 'display:block;width:100%;height:16px;margin:12px 0 6px;accent-color:#2673b8';
  const bytesLine = document.createElement('div');
  bytesLine.style.cssText = 'font-variant-numeric:tabular-nums';
  const timing = document.createElement('div');
  timing.style.cssText = 'font-size:12px;color:#555;min-height:18px';
  const stop = document.createElement('button');
  stop.type = 'button'; stop.textContent = 'Stop';
  stop.style.cssText = 'margin-top:10px;padding:4px 12px;cursor:pointer';
  panel.append(status, bar, bytesLine, timing, stop); document.body.append(panel);
  const controller = new AbortController();
  window[key] = controller;
  stop.onclick = () => { controller.abort(); status.textContent = 'Stopping…'; };

  function bytes(value) {
    const units = ['B', 'KiB', 'MiB', 'GiB'];
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return value.toFixed(unit ? 1 : 0) + ' ' + units[unit];
  }
  function duration(seconds) {
    seconds = Math.max(0, Math.ceil(seconds));
    if (seconds < 60) return seconds + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + seconds % 60 + 's';
    return Math.floor(seconds / 3600) + 'h ' + Math.floor(seconds % 3600 / 60) + 'm';
  }
  function download(url) {
    return new Promise((resolve, reject) => {
      // Native XHR progress keeps the large response as a browser-managed Blob;
      // there is no need to accumulate multi-GB arrays of chunks in JavaScript.
      const xhr = new XMLHttpRequest();
      const started = performance.now();
      let loaded = 0, total = 0, lastData = started, settled = false, timer;
      const samples = [{ time: started, bytes: 0 }];
      function paint() {
        const now = performance.now(), elapsed = (now - started) / 1000;
        const last = samples[samples.length - 1], first = samples[0];
        const rate = last.time - first.time >= 500 ? (last.bytes - first.bytes) * 1000 / (last.time - first.time) : 0;
        if (total) {
          bar.value = Math.min(100, loaded / total * 100);
          bytesLine.textContent = Math.floor(bar.value) + '% · ' + bytes(loaded) + ' / ' + bytes(total);
        } else {
          bar.removeAttribute('value');
          bytesLine.textContent = bytes(loaded) + ' received · total size unknown';
        }
        const fresh = now - lastData < 5000;
        timing.textContent = duration(elapsed) + ' elapsed' + (!fresh ? ' · Waiting for data…' : rate > 0
          ? ' · ' + bytes(rate) + '/s' + (total > loaded ? ' · ~' + duration((total - loaded) / rate) + ' left for this ZIP' : '') : ' · Measuring speed…');
      }
      function finish(error, blob) {
        if (settled) return;
        settled = true;
        clearInterval(timer);
        controller.signal.removeEventListener('abort', abort);
        if (error) reject(error); else resolve(blob);
      }
      function abort() {
        xhr.abort();
        finish(new DOMException('Stopped', 'AbortError'));
      }
      xhr.open('GET', url);
      xhr.responseType = 'blob';
      xhr.onprogress = event => {
        const now = performance.now();
        if (event.loaded > loaded) {
          lastData = now;
          samples.push({ time: now, bytes: event.loaded });
          while (samples.length > 2 && samples[1].time < now - 5000) samples.shift();
        }
        loaded = event.loaded;
        total = event.lengthComputable && event.total > 0 ? event.total : 0;
        paint();
      };
      xhr.onload = () => {
        if (xhr.status < 200 || xhr.status >= 300) { finish(new Error('HTTP ' + xhr.status)); return; }
        loaded = total = xhr.response.size;
        paint();
        finish(null, xhr.response);
      };
      xhr.onerror = () => finish(new Error('The connection failed or the site blocked the download.'));
      xhr.onabort = () => finish(new DOMException('Stopped', 'AbortError'));
      xhr.ontimeout = () => finish(new Error('The download timed out.'));
      controller.signal.addEventListener('abort', abort, { once: true });
      paint(); timer = setInterval(paint, 1000);
      if (controller.signal.aborted) { abort(); return; }
      try { xhr.send(); } catch (error) { finish(error); }
    });
  }

  let sent = 0, current = '';
  try {
    for (const file of queue) {
      controller.signal.throwIfAborted();
      current = file.name;
      status.textContent = 'Downloading ' + (sent + 1) + '/' + queue.length + ': ' + current;
      // Like Teachable's own button, use a Blob to retain the readable filename
      // across origins. Awaiting the full body limits network transfers to one.
      const blob = await download(file.url);
      status.textContent = 'Checking ' + (sent + 1) + '/' + queue.length + ': ' + current;
      timing.textContent = 'Download complete · checking ZIP archive…';
      const magic = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
      if (magic[0] !== 80 || magic[1] !== 75 || ![[3, 4], [5, 6], [7, 8]].some(pair => magic[2] === pair[0] && magic[3] === pair[1])) {
        throw new Error('The response is not a ZIP archive.');
      }
      controller.signal.throwIfAborted();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url; link.download = file.name;
      document.body.append(link); link.click(); link.remove();
      // Allow Firefox time to take ownership; do not retain every archive for
      // the lifetime of the page as the site's own handler does.
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      sent++;
      status.textContent = 'Sent ' + sent + '/' + queue.length + ' to Firefox: ' + current;
      timing.textContent = 'Check Firefox Downloads for save status.';
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    status.textContent = 'Sent ' + sent + ' ZIPs to Firefox. Check Downloads for their save status.';
  } catch (error) {
    status.textContent = controller.signal.aborted
      ? 'Stopped. ' + sent + ' ZIPs were sent to Firefox. Run the bookmark again to choose a starting chapter.'
      : 'Stopped at ' + current + ': ' + error.message + ' Run the bookmark again to retry this chapter.';
  } finally {
    delete window[key];
    bar.style.display = 'none'; bytesLine.textContent = ''; timing.textContent = '';
    stop.textContent = 'Close'; stop.onclick = () => panel.remove();
  }
})();
