/* Models tab — "Model decisions" card (issue #373).
 *
 * Folded by default, styled with the same vendored disclosure idiom as the
 * other collapsibles (_vendored/disclosure). Mirrors app-launcher's
 * System Map lazy-load discipline: no polling while collapsed, the small
 * JSON payload (GET /admin/api/roles) is fetched only when the <details>
 * is expanded, and re-fetched on every subsequent open so a role change
 * made elsewhere shows up next time you look, without a background timer.
 */

import { els } from './state.js';
import { jsonApi, escapeHtml } from './api.js';

// -------------------------------------------------------------- formatting
// role_key -> a human label without hardcoding every role name: split on the
// audio.* dotted nesting and underscores, title-case each word. Works for
// today's roles and whatever a future role adds later.
function roleLabel(key) {
  return String(key).split('.').map(function (part) {
    return part.split('_').map(function (w) {
      return w ? w.charAt(0).toUpperCase() + w.slice(1) : w;
    }).join(' ');
  }).join(' · ');
}

function setStatus(msg) {
  if (!els.rolesStatus) return;
  els.rolesStatus.textContent = msg || '';
  els.rolesStatus.hidden = !msg;
}

// ------------------------------------------------------------------ render
function renderRoles(rolesBody) {
  const list = els.rolesList;
  if (!list) return;
  const roles = (rolesBody && rolesBody.roles) || {};
  const keys = Object.keys(roles);
  list.innerHTML = '';
  if (!keys.length) {
    list.innerHTML = '<li class="muted small">No roles configured in config/models.yaml.</li>';
    return;
  }
  keys.forEach(function (key) {
    const entry = roles[key] || {};
    const li = document.createElement('li');
    li.className = 'startup-row';
    const value = escapeHtml(entry.display_name || entry.model_id || '—')
      + ((entry.fallback || []).length
        ? ' <span class="muted small">(+ fallback: ' + escapeHtml(entry.fallback.join(', ')) + ')</span>'
        : '');
    li.innerHTML =
      '<span class="startup-row-label"><span class="fleet-model-name">' + escapeHtml(roleLabel(key)) + '</span></span>'
      + '<span class="roles-row-value">' + value + '</span>';
    list.appendChild(li);
  });
}

// ------------------------------------------------------------------- fetch
async function loadRolesCard() {
  setStatus('Loading…');
  try {
    const rolesBody = await jsonApi('/admin/api/roles');
    renderRoles(rolesBody);
    setStatus('');
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    setStatus('Could not load role data.');
  }
}

// ------------------------------------------------------------------ wiring
export function wireRolesCard() {
  if (els.rolesCard) {
    els.rolesCard.addEventListener('toggle', function () {
      if (els.rolesCard.open) loadRolesCard();
    });
  }
}
