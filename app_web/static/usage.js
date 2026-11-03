/* Usage log wiring: which views and controls the owner uses, on which device class
 * (contract: _vendored/usage-log/README.md).
 *
 * The loopback PC bypasses the bearer token, a phone on the tunnel or tailnet does not,
 * so the client's one endpoint needs the token or the phone's events would 401 silently
 * and the log would only describe the desktop. The vendored client takes it through its
 * `headers` option, a function so a rotated token is read on every send; nothing here
 * replaces window.fetch or navigator.sendBeacon.
 *
 * The usage router is mounted in the /admin sub-app, so the client's endpoint is the
 * /admin path rather than the vendored default `/api/usage/events`, which would 404.
 */

import { initUsageLog } from './_vendored/usage-log/usage-log.js';
import { readToken } from './api.js';

// Start the logger once, before the tabs are wired, so the restored tab counts as the
// first view. The caller hands `trackView` to the tab-change hook.
export function wireUsageLog() {
  return initUsageLog({
    endpoint: '/admin/api/usage/events',
    headers: function () {
      const token = readToken();
      return token ? { Authorization: 'Bearer ' + token } : {};
    },
  });
}
