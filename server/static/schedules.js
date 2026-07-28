// Halaman Schedules: loop terjadwal + watchdog Sentry (pindahan dari halaman Runs).
'use strict';

async function refreshSchedules() {
  let scheds;
  try {
    scheds = await api('/api/schedules');
  } catch (e) { return showError(e.message); }
  const names = Object.keys(scheds);
  $('#schedules').innerHTML = names.length ? names.map((name) => {
    const s = scheds[name];
    const when = s.at ? `harian ${esc(s.at)} UTC` : `tiap ${esc(s.every)}`;
    return `
      <div class="sched-row card">
        <span class="name">${esc(name)}</span>
        <span class="when">${when} · ${s.steps} step${s.steps > 1 ? 's' : ''}</span>
        <span class="spacer"></span>
        ${s.active_run
          ? `<a href="/run/${esc(s.active_run)}">${badge('running')}</a>`
          : `<button class="small" data-trigger="${esc(name)}">Run now</button>`}
      </div>`;
  }).join('') : `<div class="empty">Belum ada schedule.<br>
    Isi <code>schedules:</code> di <code>config.yaml</code> (step bisa nunjuk
    <code>task:</code> dari registry).</div>`;
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
      : ' · belum poll';
    return `<span class="chip">🐛 ${esc(slug)} → ${esc(proj)} (tiap ${
      esc(interval)})${tick}</span>`;
  }).join(' ') || '<span class="chip">nggak ada project ke-mapping</span>';
  const spawned = (w.last_spawned || []).map((id) =>
    `<a href="/run/${esc(id)}"><code>${esc(id)}</code></a>`).join(' ');
  const detail = [
    w.organization ? `org <code>${esc(w.organization)}</code>` : 'org belum diisi',
    `default tiap ${esc(w.interval)}`,
    `cooldown ${esc(w.cooldown)}`,
    w.token_set ? 'token ✓' : 'token belum ada',
  ].join(' · ');
  const lastTick = w.last_tick_at
    ? `poll terakhir ${timeAgo(w.last_tick_at)} · ${w.last_checked} issue dicek`
    : 'belum pernah poll';

  $('#watchdog').innerHTML = `
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
    </div>`;

  const btn = $('#wd-tick');
  if (btn) btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'polling…';
    try { await postJSON('/api/watchdog/tick'); } catch { /* status ada di panel */ }
    refreshWatchdog();
  });
}

function init() {
  refreshSchedules();
  refreshWatchdog();
  setInterval(() => { refreshSchedules(); refreshWatchdog(); }, 5000);

  document.addEventListener('click', async (e) => {
    const trig = e.target.dataset && e.target.dataset.trigger;
    if (!trig) return;
    e.target.disabled = true;
    try { await postJSON(`/api/schedules/${trig}/trigger`); } catch { /* panel refresh */ }
    refreshSchedules();
  });
}

init();
