// nloop dashboard — vanilla JS, tanpa build step.
// File ini: helper + shell (sidebar) yang dipakai SEMUA halaman.
'use strict';

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const ACTIVE = ['queued', 'running'];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

const fmtCost = (c) => '$' + (c || 0).toFixed(2);

/** Durasi detik → "820ms" / "12.4s" / "3m20s" / "1h4m" (ringkas, tetap kebaca). */
function fmtDur(sec) {
  if (sec === null || sec === undefined) return '';
  if (sec < 1) return Math.round(sec * 1000) + 'ms';
  if (sec < 60) return (sec < 10 ? sec.toFixed(1) : Math.round(sec)) + 's';
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m${Math.round(sec % 60) ? Math.round(sec % 60) + 's' : ''}`;
  return `${Math.floor(m / 60)}h${m % 60 ? (m % 60) + 'm' : ''}`;
}

function timeAgo(ts) {
  if (!ts) return '';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function runDuration(run) {
  if (!run.started_at) return '';
  return fmtDur((run.ended_at || Date.now() / 1000) - run.started_at);
}

const badge = (s) => `<span class="badge ${esc(s)}">${esc(s)}</span>`;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.status + '';
    try {
      const body = await r.json();
      msg = body.detail || JSON.stringify(body);
    } catch { /* body bukan JSON — pakai status code aja */ }
    throw new Error(msg);
  }
  return r.json();
}

const postJSON = (path, body) => api(path, {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify(body || {}),
});

// ---------- shell ----------

const NAV = [
  {href: '/', key: 'runs', icon: '▶', label: 'Runs'},
  {href: '/tasks', key: 'tasks', icon: '⚡', label: 'Tasks'},
  {href: '/schedules', key: 'schedules', icon: '🗓', label: 'Schedules'},
];

function renderShell() {
  const side = $('#sidebar');
  if (!side) return;
  const active = document.body.dataset.nav;
  side.innerHTML = `
    <a class="brand" href="/">
      <span class="logo">∞</span>
      <span class="brand-txt">nloop<small>loop engineering</small></span>
    </a>
    <nav class="nav">
      ${NAV.map((n) => `
        <a class="nav-item ${n.key === active ? 'active' : ''}" href="${n.href}">
          <span class="ico">${n.icon}</span>${n.label}
          <span class="nav-count" data-count="${n.key}"></span>
        </a>`).join('')}
    </nav>
    <div class="side-foot">
      <span class="dot" id="health-dot"></span><span id="health-txt">connecting…</span>
    </div>`;
  pollShell();
  setInterval(pollShell, 5000);
}

async function pollShell() {
  try {
    const runs = await api('/api/loops?limit=200');
    const running = runs.filter((r) => ACTIVE.includes(r.status)).length;
    const el = $('[data-count="runs"]');
    el.textContent = running || '';
    el.className = 'nav-count' + (running ? ' live' : '');
    $('#health-dot').className = 'dot ok';
    $('#health-txt').textContent = `${runs.length} runs · ${
      fmtCost(runs.reduce((a, r) => a + (r.cost_total || 0), 0))}`;
  } catch {
    $('#health-dot').className = 'dot bad';
    $('#health-txt').textContent = 'server unreachable';
  }
}

/** Panel error kecil di atas konten — dipakai semua halaman biar konsisten. */
function showError(msg) {
  let el = $('#page-error');
  if (!el) {
    el = document.createElement('div');
    el.id = 'page-error';
    el.className = 'page-error';
    $('.content').prepend(el);
  }
  el.textContent = '⚠ ' + msg;
  el.hidden = false;
}

renderShell();
