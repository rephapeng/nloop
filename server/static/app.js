// nloop dashboard — vanilla JS, no build. Halaman dipilih via <body data-page>.
'use strict';

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const fmtCost = (c) => '$' + (c || 0).toFixed(2);
const badge = (s) => `<span class="badge ${esc(s)}">${esc(s)}</span>`;
const ACTIVE = ['queued', 'running'];

function timeAgo(ts) {
  if (!ts) return '';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function duration(run) {
  if (!run.started_at) return '';
  const end = run.ended_at || Date.now() / 1000;
  const s = Math.max(0, Math.round(end - run.started_at));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${s % 60 ? (s % 60) + 's' : ''}`;
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()) || r.status);
  return r.json();
}

// ---------- index ----------

function statChips(runs) {
  const n = (st) => runs.filter((r) => r.status === st).length;
  const cost = runs.reduce((a, r) => a + (r.cost_total || 0), 0);
  const parts = [];
  if (n('running')) parts.push(`<span class="stat"><b>${n('running')}</b> running</span>`);
  if (n('queued')) parts.push(`<span class="stat"><b>${n('queued')}</b> queued</span>`);
  parts.push(`<span class="stat"><b>${n('succeeded')}</b> ok</span>`);
  if (n('failed')) parts.push(`<span class="stat"><b>${n('failed')}</b> failed</span>`);
  parts.push(`<span class="stat">Σ <b>${fmtCost(cost)}</b></span>`);
  return parts.join('');
}

function runRow(r) {
  const pct = Math.min(100, (r.iterations_done / (r.max_iterations || 1)) * 100);
  const barCls = r.status === 'succeeded' ? 'ok' : r.status === 'failed' ? 'bad' : '';
  const chips = [
    r.role ? `<span class="chip role">role: ${esc(r.role)}</span>` : '',
    r.gate_prompt ? '<span class="chip gate">gate</span>' : '',
    r.fingerprint && r.fingerprint.startsWith('schedule:')
      ? `<span class="chip">🗓 ${esc(r.fingerprint.slice(9))}</span>` : '',
    r.fingerprint && r.fingerprint.startsWith('sentry:')
      ? `<span class="chip gate">🐛 ${esc(r.fingerprint)}</span>` : '',
  ].join('');
  return `
    <div class="run-row card" data-goto="/run/${esc(r.id)}">
      <div class="goal" title="${esc(r.goal)}">${esc(r.goal)}</div>
      <div class="meta-line">
        ${badge(r.status)}
        <code>${esc(r.id)}</code>
        ${chips}
        <span>${timeAgo(r.created_at)}${r.status === 'running' ? ' · ' + duration(r) : ''}</span>
      </div>
      <div class="side">
        <div class="nums">
          <div><b>${r.iterations_done}</b>/${r.max_iterations} iters</div>
          <div><b>${fmtCost(r.cost_total)}</b> / ${fmtCost(r.max_cost_usd)}</div>
        </div>
        <div class="bar ${barCls}"><i style="width:${pct}%"></i></div>
        ${ACTIVE.includes(r.status)
          ? `<button class="danger small" data-stop="${esc(r.id)}">Stop</button>` : ''}
      </div>
    </div>`;
}

async function refreshList() {
  const runs = await api('/api/loops');
  $('#stats').innerHTML = statChips(runs);
  $('#runs').innerHTML = runs.length
    ? runs.map(runRow).join('')
    : '<div class="empty">No loops yet.<br>Create one with <b>＋ New loop</b>, the <code>bin/nloop new</code> CLI, a webhook, a schedule, or Telegram.</div>';
}

async function refreshSchedules() {
  let scheds;
  try { scheds = await api('/api/schedules'); } catch { return; }
  const names = Object.keys(scheds);
  if (!names.length) return;
  $('#schedules-section').hidden = false;
  $('#schedules').innerHTML = names.map((name) => {
    const s = scheds[name];
    const when = s.at ? `daily at ${esc(s.at)} UTC` : `every ${esc(s.every)}`;
    return `
      <div class="sched-row card">
        <span class="name">${esc(name)}</span>
        <span class="when">${when} · ${s.steps} step${s.steps > 1 ? 's' : ''}</span>
        <span class="spacer"></span>
        ${s.active_run
          ? `<a href="/run/${esc(s.active_run)}"><span class="badge running">active</span></a>`
          : `<button class="small" data-trigger="${esc(name)}">Run now</button>`}
      </div>`;
  }).join('');
}

async function refreshWatchdog() {
  let w;
  try { w = await api('/api/watchdog'); } catch { return; }
  const configured = w.enabled && w.organization && w.token_set;
  const state = w.enabled
    ? (configured ? '<span class="badge running">active</span>'
                  : '<span class="badge queued">misconfigured</span>')
    : '<span class="badge stopped">off</span>';
  const projects = Object.entries(w.projects)
    .map(([slug, proj]) => {
      const interval = (w.project_intervals || {})[slug] || w.interval;
      const ps = (w.project_status || {})[slug];
      const tick = ps && ps.last_tick_at
        ? ` · ${timeAgo(ps.last_tick_at)} · ${ps.last_checked} checked`
        : ' · no poll yet';
      return `<span class="chip">🐛 ${esc(slug)} → ${esc(proj)} (every ${esc(interval)})${tick}</span>`;
    })
    .join(' ') || '<span class="chip">no projects mapped</span>';
  const lastTick = w.last_tick_at
    ? `last poll ${timeAgo(w.last_tick_at)} · ${w.last_checked} issue${w.last_checked === 1 ? '' : 's'} checked`
    : 'no poll yet';
  const spawned = (w.last_spawned || []).map((id) =>
    `<a href="/run/${esc(id)}"><code>${esc(id)}</code></a>`).join(' ');
  const detail = [
    w.organization ? `org <code>${esc(w.organization)}</code>` : 'org not set',
    `default every ${esc(w.interval)}`,
    `cooldown ${esc(w.cooldown)}`,
    w.token_set ? 'token ✓' : 'token missing',
  ].join(' · ');

  $('#watchdog').innerHTML = `
    <div class="sched-row card" style="flex-wrap:wrap">
      ${state}
      <span class="when">${detail}</span>
      <span class="spacer"></span>
      ${w.enabled && w.organization
        ? '<button class="small" id="wd-tick">Poll now</button>' : ''}
      <div style="flex-basis:100%;display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-top:.45rem">
        ${projects}
        <span class="when">${lastTick}${spawned ? ' · spawned: ' + spawned : ''}</span>
        ${w.last_error ? `<span class="when" style="color:var(--amber)">⚠ ${esc(w.last_error)}</span>` : ''}
      </div>
    </div>`;

  const btn = $('#wd-tick');
  if (btn) btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'polling…';
    try { await api('/api/watchdog/tick', { method: 'POST' }); } catch {}
    await refreshWatchdog();
    refreshList();
  });
}

function initIndex() {
  refreshList();
  refreshSchedules();
  refreshWatchdog();
  setInterval(() => { refreshList(); refreshSchedules(); refreshWatchdog(); }, 3000);

  $('#toggle-form').addEventListener('click', () => {
    const f = $('#new-loop');
    f.hidden = !f.hidden;
    if (!f.hidden) f.querySelector('[name=goal]').focus();
  });

  $('#new-loop').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { goal: f.get('goal'), verify_cmd: f.get('verify_cmd') };
    for (const k of ['workdir', 'model', 'role', 'context_cmd', 'gate_prompt']) {
      if (f.get(k)) body[k] = f.get(k);
    }
    if (f.get('max_iterations')) body.max_iterations = +f.get('max_iterations');
    if (f.get('max_cost_usd')) body.max_cost_usd = +f.get('max_cost_usd');
    try {
      const r = await api('/api/loops', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      location.href = `/run/${r.run_id}`;
    } catch (err) {
      $('#form-error').textContent = err.message;
    }
  });

  document.addEventListener('click', async (e) => {
    const stopId = e.target.dataset && e.target.dataset.stop;
    if (stopId) {
      e.stopPropagation();
      await api(`/api/loops/${stopId}/stop`, { method: 'POST' });
      refreshList();
      return;
    }
    const trig = e.target.dataset && e.target.dataset.trigger;
    if (trig) {
      e.target.disabled = true;
      try { await api(`/api/schedules/${trig}/trigger`, { method: 'POST' }); } catch {}
      refreshSchedules();
      refreshList();
      return;
    }
    const row = e.target.closest('[data-goto]');
    if (row && !e.target.closest('a,button')) location.href = row.dataset.goto;
  });
}

// ---------- run detail ----------

function initRun() {
  const runId = location.pathname.split('/').pop();
  $('#run-id').textContent = runId;
  loadRun(runId);
  streamEvents(runId);
  $('#stop').addEventListener('click', () =>
    api(`/api/loops/${runId}/stop`, { method: 'POST' }));
}

function setBar(el, ratio, warnAt) {
  const pct = Math.min(100, ratio * 100);
  el.querySelector('i').style.width = pct + '%';
  el.className = 'bar' + (ratio >= 1 ? ' bad' : ratio >= (warnAt ?? 2) ? ' warn' : '');
}

async function loadRun(runId) {
  const run = await api(`/api/loops/${runId}`);
  $('#goal').textContent = run.goal;
  $('#status-badge').innerHTML = badge(run.status);
  $('#stop').style.display = ACTIVE.includes(run.status) ? '' : 'none';

  $('#chips').innerHTML = [
    `<span class="chip">verify: <code>${esc(run.verify_cmd)}</code></span>`,
    `<span class="chip">workdir: <code>${esc(run.workdir)}</code></span>`,
    run.model ? `<span class="chip">model: ${esc(run.model)}</span>` : '',
    run.role ? `<span class="chip role">role: ${esc(run.role)}</span>` : '',
    run.context_cmd ? `<span class="chip">grounding: <code>${esc(run.context_cmd)}</code></span>` : '',
    run.gate_prompt ? `<span class="chip gate" title="${esc(run.gate_prompt)}">quality gate on</span>` : '',
    duration(run) ? `<span class="chip">⏱ ${duration(run)}</span>` : '',
  ].join('');

  $('#iter').textContent = `${run.iterations_done}/${run.max_iterations}`;
  setBar($('#iter-bar'), run.iterations_done / (run.max_iterations || 1));
  $('#cost').textContent = `${fmtCost(run.cost_total)} / ${fmtCost(run.max_cost_usd)}`;
  setBar($('#cost-bar'), (run.cost_total || 0) / (run.max_cost_usd || 1), 0.8);

  if (run.iterations.length) {
    $('#iterations').innerHTML = run.iterations.map((it) => `
      <details class="iter card">
        <summary>
          <span class="n">${it.idx}</span>
          <span class="tick ${it.verifier_passed ? 'ok' : 'no'}">${it.verifier_passed ? '✓' : '✗'}</span>
          <span class="desc">${esc((it.result_text || it.reason || '').split('\n')[0])}</span>
          <span class="figures">${esc(it.reason)} · ${it.turns} turns · ${fmtCost(it.cost)}</span>
        </summary>
        <div class="body">
          <h3>${it.verifier_passed ? 'Verifier passed — gate feedback' : 'Verifier output (before action)'}</h3>
          <pre>${esc(it.verifier_output)}</pre>
          <h3>Action result</h3>
          <pre>${esc(it.result_text || '(empty)')}</pre>
        </div>
      </details>`).join('');
  }
}

function streamEvents(runId) {
  const live = $('#live');
  const add = (cls, text) => {
    const div = document.createElement('div');
    div.className = 'ev ' + cls;
    div.textContent = text;
    live.appendChild(div);
    live.scrollTop = live.scrollHeight;
  };
  const data = (e) => JSON.parse(e.data);

  const es = new EventSource(`/api/loops/${runId}/events`);
  es.addEventListener('init', () => add('status', '▶ claude session started'));
  es.addEventListener('turn', (e) => add('turn', data(e).text));
  es.addEventListener('tool', (e) => {
    const d = data(e);
    add('tool', `🔧 ${d.name} ${d.input || ''}`);
  });
  es.addEventListener('verify', (e) => {
    const d = data(e);
    add(d.passed ? 'pass' : 'fail',
      `verify: ${d.passed ? 'PASS ✓' : 'FAIL ✗'} (exit ${d.exit_code})`);
    loadRun(runId);
  });
  es.addEventListener('gate', (e) => {
    const d = data(e);
    add(d.passed ? 'gate-pass' : 'gate-fail',
      d.passed ? '🛡 quality gate: PASSED'
               : `🛡 quality gate: REJECTED — ${(d.reasons || []).join('; ')}`);
    loadRun(runId);
  });
  es.addEventListener('log', (e) => {
    const d = data(e);
    add(d.level === 'warn' ? 'warn' : 'status', `⚠ ${d.msg}`);
  });
  es.addEventListener('result', (e) => {
    const d = data(e);
    add('result',
      `iteration done: ${d.subtype} · ${d.num_turns} turns · ${fmtCost(d.cost_usd)}`);
    loadRun(runId);
  });
  es.addEventListener('status', (e) => {
    const d = data(e);
    add('status', `status → ${d.status}${d.reason ? ' (' + d.reason + ')' : ''}`);
    loadRun(runId);
  });
  es.addEventListener('done', () => {
    es.close();
    add('status', '■ stream closed');
    loadRun(runId);
  });
  es.onerror = () => add('fail', '⚠ stream disconnected — reconnecting…');
}

// ---------- boot ----------

({ index: initIndex, run: initRun })[document.body.dataset.page]();
