/* Entry point: boots the SPA. Each module exposes named functions; this
 * file sequences boot, polling, and tab-change driven start/stop of
 * background event streams.
 */

import { state, els, THEME_KEY, STATUS_POLL_MS, COUNTERS_POLL_MS, MODELS_POLL_MS } from './state.js';
import { api, jsonApi, tokenFromUrl, writeToken, setSignInHandler, toast } from './api.js';
import { initRestartSheet } from './_vendored/page-foot/restart-sheet.js';
import { icon } from './_vendored/icons/icons.js';
import { bindTextSize } from './_vendored/text-size/text-size.js';
import { buildReadoutText } from './_vendored/page-foot/page-foot.js';
import { initBuildWatch } from './_vendored/build-watch/build-watch.js';
import { startPoll } from './_vendored/poll/poll.js';
import { wireTabs, onTabChange } from './tabs.js';
import { wireUsageLog } from './usage.js';
import { wireHub, fetchHubStatus, fetchCounters, startHubStreams, stopHubStreams, fetchInstallStatus } from './hub.js';
import { wireModels, fetchModels } from './models.js';
import { wireRolesCard } from './roles_card.js';
import { wirePlayground, fetchPlaygroundModels } from './playground.js';

// --------------------------------------------------------------- theme toggle
// The pre-paint boot script in index.html already stamped html[data-theme]
// (localStorage override, prefers-color-scheme fallback); this block owns the
// Hub-card sun/moon button. Same mechanism as home-automation/app-launcher.
function applyTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  // Show the glyph for the action: sun to switch to light, moon to switch to dark.
  if (els.themeToggleBtn) els.themeToggleBtn.innerHTML = icon(dark ? 'sun' : 'moon');
  localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light');
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme !== 'dark');
}

(function initTheme() {
  const stored = localStorage.getItem(THEME_KEY);
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  applyTheme(stored ? stored === 'dark' : prefersDark);
})();

if (els.themeToggleBtn) els.themeToggleBtn.addEventListener('click', toggleTheme);

// Text size: the pre-paint script stamped html[data-textsize]; this paints the
// stored step on the Health & install card control and persists clicks.
const textSizeControl = document.getElementById('textSizeControl');
if (textSizeControl) bindTextSize(textSizeControl, 'llmhub');

// The sheet's POST reads the status itself (a refusal is an answer, not an error).
async function postRestart(url, body) {
  let res;
  try {
    res = await api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  } catch (exc) {
    if (exc && exc.message === 'auth required') return { status: 401, body: null };
    throw exc;
  }
  let data = null;
  try { data = await res.json(); } catch (_) { data = null; }
  return { status: res.status, body: data };
}

async function fetchVersion() {
  try {
    const body = await jsonApi('/admin/api/version');
    state.version = body;
    if (els.buildReadout) {
      els.buildReadout.textContent = buildReadoutText(body.git_sha || 'unknown', body.built_at || '');
    }
  } catch (_) { /* ignore */ }
}

async function boot() {
  const fromUrl = tokenFromUrl();
  if (fromUrl) writeToken(fromUrl);

  setSignInHandler(function () { return resumeAfterLogin(); });
  wireHub();
  wireModels();
  wireRolesCard();
  wirePlayground();

  // Register the tab-change hook BEFORE wiring the nav: the vendored
  // component restores the persisted tab during wireTabs() and fires
  // onChange for it, which is what starts the right streams/polls.
  onTabChange(function (tab) {
    if (tab === 'hub') {
      startHubStreams();
    } else {
      stopHubStreams();
    }
  });
  onTabChange(wireUsageLog().trackView);
  wireTabs();

  await Promise.allSettled([
    fetchVersion(),
    fetchHubStatus(),
    fetchCounters(),
    fetchModels(),
    fetchInstallStatus(),
    fetchPlaygroundModels(),
  ]);
  initRestartSheet({
    fetchJson: jsonApi, postJson: postRestart,
    endpoint: '/admin/api/restart', versionEndpoint: '/admin/api/version',
  });

  // The vendored nav already restored the persisted tab (and its streams)
  // in wireTabs(); only (re)start the hub streams if that's where we are.
  if (state.tab === 'hub') startHubStreams();

  // Poll loops — light, no SSE for these
  // Self-scheduling, single-flight, paused while the page is hidden.
  startPoll(fetchHubStatus, STATUS_POLL_MS);
  startPoll(fetchCounters, COUNTERS_POLL_MS);
  startPoll(function () {
    if (state.tab === 'models') return fetchModels();
  }, MODELS_POLL_MS);
  // "New version available · Reload" when a deploy lands under an open page.
  initBuildWatch({ endpoint: '/admin/api/version', fetchJson: jsonApi });
}

async function resumeAfterLogin() {
  toast('Signed in.', 'good');
  await Promise.allSettled([
    fetchHubStatus(),
    fetchCounters(),
    fetchModels(),
    fetchInstallStatus(),
    fetchPlaygroundModels(),
  ]);
  if (state.tab === 'hub') startHubStreams();
}

window.addEventListener('DOMContentLoaded', function () {
  boot().catch(function (err) {
    console.error('boot failed', err);
  });
});
