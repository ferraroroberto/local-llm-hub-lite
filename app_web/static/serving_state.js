/* A model's serving state as the hub derives it (`state` and `reason` on each
 * controllable /admin/api/models row: stopped, loading, ready, adopted, failed,
 * unknown). The Models tab reads the badge, the Start / Stop / Ping enablement
 * and the on-demand label from here, so a tile never rebuilds the state from
 * `ownership` and `reachable`. */

/* A row from an older hub sends only `ownership`, read the way it always was. */
export function servingState(m) {
  if (m.state) return m.state;
  if (m.ownership === 'ours') return 'ready';
  return m.ownership === 'external' ? 'adopted' : 'stopped';
}

/* Serves requests: ready or adopted. Loading, failed and unknown do not. */
export function isServing(m) {
  const s = servingState(m);
  return s === 'ready' || s === 'adopted';
}

/* The tile's badge: the label, its tone class and a tooltip (the reason a
 * failed or unknown state carries). */
const BADGES = {
  stopped: { text: 'stopped', tone: '' },
  loading: { text: 'loading', tone: 'warn' },
  ready: { text: 'running', tone: 'good' },
  adopted: { text: 'adopted', tone: 'warn' },
  failed: { text: 'failed', tone: 'danger' },
  unknown: { text: 'unknown', tone: 'warn' },
};

export function servingBadge(m) {
  const b = BADGES[servingState(m)] || BADGES.stopped;
  return { text: b.text, tone: b.tone, title: m.reason || '' };
}

/* Start is for a tile with nothing running; a failed one may be a dead
 * process (restart it) or a hung one (Start answers "already running"). */
export function canStart(m) {
  const s = servingState(m);
  return s === 'stopped' || s === 'failed';
}

/* Stop is for a process of ours: loading, ready, or a failed one that may
 * still be alive and hung. Stopping a dead failed one clears the failure. */
export function canStop(m) {
  const s = servingState(m);
  return s === 'loading' || s === 'ready' || s === 'failed';
}

/* The word an on-demand row's startup label shows for this state, if any. */
const ON_DEMAND_WORD = { loading: 'loading', failed: 'failed', unknown: 'unknown' };

export function onDemandWord(m) {
  return ON_DEMAND_WORD[servingState(m)] || null;
}
