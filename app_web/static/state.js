/* Shared singletons: state, DOM-element references, polling intervals.
 *
 * Auth: a bearer token is stored in localStorage. The page extracts it
 * from ?token=… on first load and strips it from the URL. On 401, the
 * login overlay shows; password → /admin/api/login → bearer token.
 */

export const TOKEN_KEY = 'llmhub.token';
/* Theme override — same key the pre-paint boot script in index.html reads;
 * absent = follow the OS prefers-color-scheme. */
export const THEME_KEY = 'llmhub.theme';
/* Active tab — persisted by the vendored nav so the installed PWA reopens
 * where you left it (fleet nav contract). */
export const TAB_KEY = 'llmhub.tab';
/* Models tab "Active only" filter — persisted like Plugs' show-hidden
 * toggle in home-automation (#266). */
export const MODELS_ACTIVE_ONLY_KEY = 'llmhub.models.activeOnly';

export const STATUS_POLL_MS = 4000;
export const COUNTERS_POLL_MS = 4000;
export const MODELS_POLL_MS = 5000;
export const STATS_POLL_MS = 2000;

export const state = {
  tab: 'hub',
  status: null,           // /admin/api/hub/status payload
  models: [],             // /admin/api/models payload
  counters: [],           // rows from /admin/api/hub/counters
  liveRequests: [],       // ring synced from SSE stream
  recentErrors: [],
  logLines: [],
  logPaused: false,
  installRows: [],
  version: null,
  hubStreamCtl: null,     // EventSource abort handles
  hubLogStreamCtl: null,

  // Models tab — "Active only" filter, default on (issue #266).
  modelsActiveOnly: true,
};

// ES modules are deferred; document.getElementById is safe at top level.
export const els = {
  // Tab buttons + panes are owned by the vendored nav (_vendored/nav) —
  // it discovers them from the markup; no element handles needed here.

  // Hub card — live status indicator lives inside the card header
  // (replaces the old always-on status strip).
  hubLiveStatus: document.getElementById('hubLiveStatus'),
  hubLiveStatusText: document.getElementById('hubLiveStatusText'),
  themeToggleBtn: document.getElementById('themeToggleBtn'),
  hubPid: document.getElementById('hubPid'),
  hubUptime: document.getElementById('hubUptime'),
  hubSparklines: document.getElementById('hubSparklines'),

  // Health & install
  installCard: document.getElementById('installCard'),
  installSummary: document.getElementById('installSummary'),
  installRows: document.getElementById('installRows'),
  installFixAllBtn: document.getElementById('installFixAllBtn'),
  installRefreshBtn: document.getElementById('installRefreshBtn'),

  // Diagnostic disclosure cards (Live / Counters / Errors / Log — #215)
  liveRequestsList: document.getElementById('liveRequestsList'),
  liveRequestsBadge: document.getElementById('liveRequestsBadge'),
  liveRequestsEmpty: document.getElementById('liveRequestsEmpty'),
  countersTable: document.getElementById('countersTable'),
  recentErrorsList: document.getElementById('recentErrorsList'),
  recentErrorsBadge: document.getElementById('recentErrorsBadge'),
  recentErrorsEmpty: document.getElementById('recentErrorsEmpty'),
  hubLog: document.getElementById('hubLog'),
  hubLogPauseBtn: document.getElementById('hubLogPauseBtn'),

  // Models tab
  modelsList: document.getElementById('modelsList'),
  modelsEmpty: document.getElementById('modelsEmpty'),
  modelsActiveToggle: document.getElementById('modelsActiveToggle'),

  // Model decisions card (issue #373) — Models tab, role → model list
  rolesCard: document.getElementById('rolesCard'),
  rolesStatus: document.getElementById('rolesStatus'),
  rolesList: document.getElementById('rolesList'),

  // Playground
  playgroundModel: document.getElementById('playgroundModel'),
  playgroundSystem: document.getElementById('playgroundSystem'),
  playgroundPrompt: document.getElementById('playgroundPrompt'),
  playgroundMore: document.getElementById('playgroundMore'),
  playgroundAttachment: document.getElementById('playgroundAttachment'),
  playgroundAttachmentBtn: document.getElementById('playgroundAttachmentBtn'),
  playgroundAttachmentName: document.getElementById('playgroundAttachmentName'),
  playgroundMaxTokens: document.getElementById('playgroundMaxTokens'),
  playgroundMaxTokensSeg: document.getElementById('playgroundMaxTokensSeg'),
  playgroundSendBtn: document.getElementById('playgroundSendBtn'),
  playgroundClearBtn: document.getElementById('playgroundClearBtn'),
  playgroundLatency: document.getElementById('playgroundLatency'),
  playgroundReply: document.getElementById('playgroundReply'),
  playgroundUsage: document.getElementById('playgroundUsage'),
  // Playground — transcription tester (lite fork)
  transcribeCard: document.getElementById('transcribeCard'),
  transcribeFile: document.getElementById('transcribeFile'),
  transcribeFileBtn: document.getElementById('transcribeFileBtn'),
  transcribeFileName: document.getElementById('transcribeFileName'),
  transcribeBtn: document.getElementById('transcribeBtn'),
  transcribeLatency: document.getElementById('transcribeLatency'),
  transcribeResult: document.getElementById('transcribeResult'),
  transcribeServedModel: document.getElementById('transcribeServedModel'),

  // Misc
  toast: document.getElementById('toast'),
  loginOverlay: document.getElementById('loginOverlay'),
  loginForm: document.getElementById('loginForm'),
  loginPassword: document.getElementById('loginPassword'),
  loginError: document.getElementById('loginError'),
  buildReadout: document.getElementById('buildReadout'),
};
