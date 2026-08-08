/* Hub tab — status card, install panel, live request stream,
 * counters, errors, log tail. The four diagnostic surfaces are vendored
 * disclosure cards, folded by default (#215).
 */

import { els, state } from './state.js';
import { jsonApi, postJson, eventStream, toast, escapeHtml, fmtClock, renderCounterTable, shortGpu, modelLabel } from './api.js';
import { icon } from './_vendored/icons/icons.js';

// --------------------------------------------------------- status / urls
export async function fetchHubStatus() {
  try {
    const body = await jsonApi('/admin/api/hub/status');
    state.status = body;
    if (els.hubPid) els.hubPid.textContent = body.pid || '—';
    if (els.hubUptime) els.hubUptime.textContent = fmtUptime(body.uptime_s);
    setHubLive('good', 'up');
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    setHubLive('danger', 'unreachable');
  }
}

function setHubLive(kind, text) {
  if (!els.hubLiveStatus) return;
  els.hubLiveStatus.classList.remove('good', 'warn', 'danger');
  if (kind) els.hubLiveStatus.classList.add(kind);
  if (els.hubLiveStatusText) els.hubLiveStatusText.textContent = text;
}

function fmtUptime(seconds) {
  if (!Number.isFinite(seconds)) return '—';
  if (seconds < 60) return Math.floor(seconds) + 's';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + Math.floor(seconds % 60) + 's';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h + 'h ' + m + 'm';
}

// --------------------------------------------------------- counters
export async function fetchCounters() {
  try {
    const body = await jsonApi('/admin/api/hub/counters');
    state.counters = body.counters || [];
    renderCounters();
  } catch (_) { /* ignore */ }
}

function renderCounters() {
  renderCounterTable(els.countersTable, state.counters);
}

// --------------------------------------------------------- live requests
function prependRequest(rec) {
  // Dedup by ts. The SSE seed (20 most-recent records) is re-sent every
  // time the EventSource (re)connects — and the browser auto-reconnects
  // after network blips, a tab-switch round-trip, or any uvicorn keepalive
  // drop. Without dedup each reconnect duplicates whatever's still in the
  // server-side ring. `ts` is the middleware's wall-clock float seconds,
  // unique per request because the recording `finally` only fires once.
  const isDup = function (arr) {
    return rec && rec.ts != null && arr.some(function (r) { return r.ts === rec.ts; });
  };
  if (!isDup(state.liveRequests)) {
    state.liveRequests = [rec].concat(state.liveRequests).slice(0, 50);
    renderRequests();
  }
  if (rec.status >= 400 && !isDup(state.recentErrors)) {
    state.recentErrors = [rec].concat(state.recentErrors).slice(0, 50);
    renderErrors();
  }
}

function renderRequests() {
  const items = state.liveRequests || [];
  const list = els.liveRequestsList;
  if (!list) return;
  if (els.liveRequestsBadge) els.liveRequestsBadge.textContent = items.length;
  if (els.liveRequestsEmpty) els.liveRequestsEmpty.hidden = items.length > 0;
  list.innerHTML = '';
  items.forEach(function (r) {
    const li = document.createElement('li');
    const cls = r.status >= 500 ? 'err' : r.status >= 400 ? 'warn' : 'ok';
    li.innerHTML =
      '<span class="muted">' + fmtClock(r.ts) + '</span>' +
      '<span>' + modelLabel(r, '(no model)') + ' <span class="muted">' + escapeHtml(r.backend || '') + '</span></span>' +
      '<span class="req-status ' + cls + '">' + r.status + ' · ' + r.latency_ms + ' ms</span>' +
      '<span class="muted">' + (r.in_tok || 0) + ' / ' + (r.out_tok || 0) + ' tok</span>';
    list.appendChild(li);
  });
}

function renderErrors() {
  const items = state.recentErrors || [];
  const list = els.recentErrorsList;
  if (!list) return;
  if (els.recentErrorsBadge) els.recentErrorsBadge.textContent = items.length;
  if (els.recentErrorsEmpty) els.recentErrorsEmpty.hidden = items.length > 0;
  list.innerHTML = '';
  items.forEach(function (r) {
    const li = document.createElement('li');
    li.innerHTML =
      '<span class="muted">' + fmtClock(r.ts) + '</span>' +
      '<span>' + modelLabel(r, '(no model)') + ' <span class="muted">' + escapeHtml(r.backend || '') + '</span></span>' +
      '<span class="req-status err">' + r.status + '</span>' +
      '<span class="muted">' + escapeHtml((r.error_detail || '').slice(0, 80)) + '</span>';
    list.appendChild(li);
  });
}

// --------------------------------------------------------- log tail
let logBuf = [];

function appendLogLine(line) {
  if (state.logPaused) return;
  logBuf.push(line);
  if (logBuf.length > 800) logBuf = logBuf.slice(-800);
  const pre = els.hubLog;
  if (!pre) return;
  pre.textContent = logBuf.join('\n');
  pre.scrollTop = pre.scrollHeight;
}

// --------------------------------------------------------- streams
export function startHubStreams() {
  stopHubStreams();
  state.hubStreamCtl = eventStream('/admin/api/hub/requests/stream', {
    message: function (data) {
      if (!data || typeof data !== 'object') return;
      prependRequest(data);
    },
  });
  state.hubLogStreamCtl = eventStream('/admin/api/hub/log/tail', {
    message: function (data) {
      if (typeof data === 'string') appendLogLine(data);
    },
  });
}

export function stopHubStreams() {
  if (state.hubStreamCtl) { try { state.hubStreamCtl.close(); } catch (_) {} state.hubStreamCtl = null; }
  if (state.hubLogStreamCtl) { try { state.hubLogStreamCtl.close(); } catch (_) {} state.hubLogStreamCtl = null; }
}

// --------------------------------------------------------- install panel
export async function fetchInstallStatus() {
  try {
    const body = await jsonApi('/admin/api/install/status');
    state.installRows = body.checks || [];
    renderInstall(body);
  } catch (_) { /* ignore */ }
}

function renderInstall(body) {
  if (!els.installRows || !els.installSummary) return;
  const checks = body.checks || [];
  const overall = body.worst_status || 'ok';
  els.installSummary.textContent = checks.length + ' checks · overall ' + overall;
  els.installSummary.className = 'collapse-count overall-' + overall;
  els.installRows.innerHTML = '';
  checks.forEach(function (c) {
    const row = document.createElement('div');
    row.className = 'install-row install-' + c.status;
    const glyph = c.status === 'ok' ? icon('circle-check')
      : c.status === 'warn' ? icon('triangle-alert')
      : c.status === 'missing' ? icon('circle-help')
      : icon('circle-x');
    row.innerHTML =
      '<span class="install-glyph">' + glyph + '</span>' +
      '<span class="install-label">' + escapeHtml(c.label) + '</span>' +
      '<span class="install-detail muted small">' + escapeHtml(c.detail || '') + '</span>';
    if (c.fix_id && (c.status === 'missing' || c.status === 'error')) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ghost-btn';
      btn.innerHTML = icon('wrench') + escapeHtml(c.fix_label || 'Fix');
      btn.addEventListener('click', async function () {
        btn.disabled = true;
        btn.textContent = 'Running…';
        try {
          await postJson('/admin/api/install/fix', { fix_id: c.fix_id });
          toast('Fixed ' + c.id, 'good');
          await fetchInstallStatus();
        } catch (exc) {
          toast(String(exc.message || exc), 'error');
          btn.disabled = false;
          btn.innerHTML = icon('wrench') + 'Retry';
        }
      });
      row.appendChild(btn);
    }
    els.installRows.appendChild(row);
  });
}

// --------------------------------------------------------- wire buttons
export function wireHub() {
  function togglePause() {
    state.logPaused = !state.logPaused;
    if (els.hubLogPauseBtn) {
      els.hubLogPauseBtn.innerHTML = state.logPaused
        ? icon('play') + 'Resume'
        : icon('pause') + 'Pause';
    }
  }
  if (els.hubLogPauseBtn) els.hubLogPauseBtn.addEventListener('click', togglePause);

  if (els.installFixAllBtn) {
    els.installFixAllBtn.addEventListener('click', async function () {
      els.installFixAllBtn.disabled = true;
      const original = els.installFixAllBtn.textContent;
      els.installFixAllBtn.textContent = 'Running…';
      try {
        await postJson('/admin/api/install/fix-all', {});
        toast('Fix-all complete.', 'good');
        await fetchInstallStatus();
      } catch (exc) {
        toast(String(exc.message || exc), 'error');
      } finally {
        els.installFixAllBtn.disabled = false;
        els.installFixAllBtn.textContent = original;
      }
    });
  }
  if (els.installRefreshBtn) {
    els.installRefreshBtn.addEventListener('click', function () { fetchInstallStatus(); });
  }

  // Sparklines: lightweight inline-SVG renderer driven by /admin/api/hub/stats.
  setInterval(function () {
    if (state.tab !== 'hub') return;
    renderSparklines();
  }, 2500);
}

async function renderSparklines() {
  let stats;
  try {
    stats = await jsonApi('/admin/api/hub/stats');
  } catch (_) { return; }
  const container = els.hubSparklines;
  if (!container) return;
  container.innerHTML = '';
  const history = stats.history || [];
  const g0 = stats.gpus && stats.gpus.length ? stats.gpus[0] : null;
  // Utilization tiles on top, memory tiles on bottom (#288).
  const groups = [
    { label: 'CPU', value: stats.cpu && stats.cpu.percent, series: history.map(function (h) { return h.cpu_percent; }) },
  ];
  if (g0) {
    groups.push({ label: 'GPU util', value: g0.util_percent, series: history.map(function (h) { return h.gpu0_util_percent; }) });
  }
  groups.push({ label: 'RAM', value: stats.ram && stats.ram.percent, series: history.map(function (h) { return h.ram_percent; }) });
  if (g0) {
    groups.push({ label: 'VRAM ' + shortGpu(g0.name), value: g0.vram_percent, series: history.map(function (h) { return h.gpu0_vram_percent; }) });
  }
  groups.forEach(function (g) {
    container.appendChild(buildSparkline(g));
  });
}

function buildSparkline(g) {
  const root = document.createElement('div');
  root.className = 'sparkline';
  const series = (g.series || []).filter(function (v) { return v !== null && v !== undefined && !isNaN(v); });
  const max = 100;
  const w = 140, h = 64;
  let path = '';
  if (series.length >= 2) {
    const step = w / (series.length - 1);
    series.forEach(function (v, i) {
      const x = i * step;
      const y = h - (v / max) * h;
      path += (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1) + ' ';
    });
  }
  // Energy-tab chart look (#215): a 2px accent line over a translucent
  // accent area (fill closed down to the baseline). Both pull from the live
  // --accent token so the tiles re-theme for free.
  const area = path ? path + 'L' + w + ',' + h + ' L0,' + h + ' Z' : '';
  root.innerHTML =
    '<div class="sparkline-label"><span>' + escapeHtml(g.label) + '</span>' +
    '<span>' + (Number.isFinite(g.value) ? Math.round(g.value) + '%' : '—') + '</span></div>' +
    '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
    (area ? '<path d="' + area + '" fill="color-mix(in srgb, var(--accent) 18%, transparent)" stroke="none"/>' : '') +
    (path ? '<path d="' + path + '" fill="none" stroke="var(--accent)" stroke-width="2" vector-effect="non-scaling-stroke"/>' : '') +
    '</svg>';
  return root;
}


/* escapeHtml / fmtClock live in api.js (sibling dedup, #211). */
