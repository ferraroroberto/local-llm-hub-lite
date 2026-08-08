/* Entry point: boots the SPA. Each module exposes named functions; this
 * file sequences boot, polling, and tab-change driven start/stop of
 * background event streams.
 */

import { state, els, THEME_KEY, STATUS_POLL_MS, COUNTERS_POLL_MS, MODELS_POLL_MS } from './state.js';
import { jsonApi, tokenFromUrl, writeToken, wireLoginForm, toast } from './api.js';
import { icon } from './_vendored/icons/icons.js';
import { wireTabs, onTabChange } from './tabs.js';
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

async function fetchVersion() {
  try {
    const body = await jsonApi('/admin/api/version');
    state.version = body;
    const sha = body.git_sha || 'unknown';
    const ts = (body.built_at || '').replace('T', ' ').slice(0, 16);
    if (els.buildReadout) {
      els.buildReadout.textContent = ts ? ('Build: ' + sha + ' · ' + ts) : ('Build: ' + sha);
    }
  } catch (_) { /* ignore */ }
}

async function boot() {
  const fromUrl = tokenFromUrl();
  if (fromUrl) writeToken(fromUrl);

  wireLoginForm(function () { return resumeAfterLogin(); });
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
  wireTabs();

  await Promise.allSettled([
    fetchVersion(),
    fetchHubStatus(),
    fetchCounters(),
    fetchModels(),
    fetchInstallStatus(),
    fetchPlaygroundModels(),
  ]);

  // The vendored nav already restored the persisted tab (and its streams)
  // in wireTabs(); only (re)start the hub streams if that's where we are.
  if (state.tab === 'hub') startHubStreams();

  // Poll loops — light, no SSE for these
  setInterval(function () { fetchHubStatus().catch(function () {}); }, STATUS_POLL_MS);
  setInterval(function () { fetchCounters().catch(function () {}); }, COUNTERS_POLL_MS);
  setInterval(function () {
    if (state.tab === 'models') fetchModels().catch(function () {});
  }, MODELS_POLL_MS);
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
