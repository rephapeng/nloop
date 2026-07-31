// Schedules page: scheduled loops + the Sentry watchdog (moved off the Runs page).
'use strict';

// These panels refresh every 5 seconds. Rewriting innerHTML each time resets hover
// and cuts animations short, so the HTML is compared first — the DOM is only touched
// when the content actually changed.
const lastHtml = {};

function paint(sel, html) {
  if (lastHtml[sel] === html) return false;
  const first = lastHtml[sel] === undefined;
  lastHtml[sel] = html;
  $(sel).innerHTML = html;
  if (first) revealChildren($(sel));
  return true;
}

async function refreshSchedules() {
  let scheds;
  try {
    scheds = await api('/api/schedules');
  } catch (e) { return showError(e.message); }
  const names = Object.keys(scheds);
  paint('#schedules', names.length ? names.map((name) => {
    const s = scheds[name];
    const when = s.at ? `daily ${esc(s.at)} UTC` : `every ${esc(s.every)}`;
    const stepIcon = {succeeded: '✓', failed: '✕', running: '⋯', queued: '⋯', stopped: '⏹'};
    const flow = (s.last_tick || []).map((step, i) => `
      ${i > 0 ? '<span class="arrow">→</span>' : ''}
      <a class="chip step ${esc(step.status)}" href="/run/${esc(step.run_id)}"
         title="${esc(step.label)} · ${esc(step.status)}">
        ${stepIcon[step.status] || '·'} ${esc(step.label)}
        ${step.duration != null ? `<span class="faint">${fmtDur(step.duration)}</span>` : ''}
      </a>`).join('');
    return `
      <div class="sched-row card">
        <div class="head">
          <span class="name">${esc(name)}</span>
          <span class="when">${when} · ${s.steps} step${s.steps > 1 ? 's' : ''}</span>
          <span class="spacer"></span>
          ${s.active_run
            ? `<a href="/run/${esc(s.active_run)}">${badge('running')}</a>`
            : `<button class="small" data-trigger="${esc(name)}">Run now</button>`}
        </div>
        ${flow ? `<div class="sched-flow">${flow}</div>` : ''}
      </div>`;
  }).join('') : `<div class="empty">No schedules yet.<br>
    Fill in <code>schedules:</code> in <code>config.yaml</code> (a step can point at
    a <code>task:</code> from the registry).</div>`);
}

async function refreshWatchdog() {
  let w;
  try {
    w = await api('/api/watchdog');
  } catch { return; }
  const configured = w.enabled && w.organization && w.token_set;
  const state = w.enabled
    ? (configured ? badge('running') : badge('queued'))
    : badge('stopped');
  const projects = Object.entries(w.projects).map(([slug, proj]) => {
    const interval = (w.project_intervals || {})[slug] || w.interval;
    const ps = (w.project_status || {})[slug];
    const tick = ps && ps.last_tick_at
      ? ` · ${timeAgo(ps.last_tick_at)} · ${ps.last_checked} checked`
      : ' · not polled yet';
    return `<span class="chip">🐛 ${esc(slug)} → ${esc(proj)} (every ${
      esc(interval)})${tick}</span>`;
  }).join(' ') || '<span class="chip">no projects mapped</span>';
  const spawned = (w.last_spawned || []).map((id) =>
    `<a href="/run/${esc(id)}"><code>${esc(id)}</code></a>`).join(' ');
  const detail = [
    w.organization ? `org <code>${esc(w.organization)}</code>` : 'org not set',
    `default every ${esc(w.interval)}`,
    `cooldown ${esc(w.cooldown)}`,
    w.token_set ? 'token ✓' : 'token missing',
  ].join(' · ');
  const lastTick = w.last_tick_at
    ? `last poll ${timeAgo(w.last_tick_at)} · ${w.last_checked} issues checked`
    : 'never polled';

  const changed = paint('#watchdog', `
    <div class="sched-row card" style="flex-wrap:wrap">
      ${state}
      <span class="when">${detail}</span>
      <span class="spacer"></span>
      ${w.enabled && w.organization
        ? '<button class="small" id="wd-tick">Poll now</button>' : ''}
      <div class="wd-line">
        ${projects}
        <span class="when">${lastTick}${spawned ? ' · spawned: ' + spawned : ''}</span>
        ${w.last_error ? `<span class="when warn-txt">⚠ ${esc(w.last_error)}</span>` : ''}
      </div>
    </div>`);
  if (!changed) return;          // the old DOM is still mounted, and so are its listeners

  const btn = $('#wd-tick');
  if (btn) btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'polling…';
    try { await postJSON('/api/watchdog/tick'); } catch { /* status shows in the panel */ }
    await refreshWatchdog();
    const again = $('#wd-tick');   // panel unchanged → the button must be restored by hand
    if (again && again.disabled) { again.disabled = false; again.textContent = 'Poll now'; }
  });
}

function init() {
  refreshSchedules();
  refreshWatchdog();
  setInterval(() => { refreshSchedules(); refreshWatchdog(); }, 5000);

  document.addEventListener('click', async (e) => {
    const trig = e.target.dataset && e.target.dataset.trigger;
    if (!trig) return;
    const btn = e.target;
    btn.disabled = true;
    try { await postJSON(`/api/schedules/${trig}/trigger`); } catch { /* panel refreshes */ }
    await refreshSchedules();
    if (btn.isConnected) btn.disabled = false;   // panel wasn't re-rendered
  });
}

init();
