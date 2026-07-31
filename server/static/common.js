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

// ---------- motion: scroll reveal ----------
// IntersectionObserver polos, tanpa library. Elemen ber-class `reveal` mulai
// transparan (lihat style.css, digate html.motion) dan dianimasiin begitu masuk
// viewport. Aturan mainnya: animasi NGGAK BOLEH bikin konten ilang — kalau
// observer nggak ada / reduced-motion / nggak pernah nembak, semua langsung tampil.

const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
const STAGGER_CAP = 12;          // item ke-13 dst. nggak usah nunggu makin lama

const revealObs = (!REDUCED && 'IntersectionObserver' in window)
  ? new IntersectionObserver((entries, obs) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        e.target.classList.add('in');
        obs.unobserve(e.target);
      }
    }, {rootMargin: '0px 0px -6% 0px', threshold: 0.04})
  : null;

let safetyTimer = null;

/** Amati semua `.reveal` yang belum kebuka di dalam root. */
function reveal(root = document) {
  const els = $$('.reveal:not(.in)', root);
  if (!revealObs) {
    els.forEach((el) => el.classList.add('in'));
    return;
  }
  els.forEach((el) => revealObs.observe(el));
  clearTimeout(safetyTimer);      // jaring pengaman: apa pun yang kejadian, konten muncul
  safetyTimer = setTimeout(
    () => $$('.reveal:not(.in)').forEach((el) => el.classList.add('in')), 1500);
}

/** Konten baru di-render → kasih .reveal + index stagger ke tiap anak, terus amati. */
function revealChildren(container, sel = ':scope > *') {
  if (!container) return;
  $$(sel, container).forEach((el, i) => {
    el.classList.add('reveal');
    el.style.setProperty('--i', Math.min(i, STAGGER_CAP));
  });
  reveal(container);
}

// ---------- workspace ----------
// Satu proses nloop nampung banyak workspace (tenant). Pilihannya disimpan di URL
// (?ws=) biar link bisa di-share, DAN di localStorage biar nempel antar halaman.
let WS = new URLSearchParams(location.search).get('ws')
      || localStorage.getItem('nloop.ws') || '';
let WORKSPACES = [];

/** Tempel ?ws= ke link internal biar pindah halaman nggak balik ke workspace lain. */
function withWs(href) {
  if (!WS) return href;
  const u = new URL(href, location.origin);
  u.searchParams.set('ws', WS);
  return u.pathname + u.search;
}

/** Semua panggilan /api/ otomatis di-scope ke workspace aktif. */
function apiUrl(path) {
  if (!WS || !path.startsWith('/api/')) return path;
  return path + (path.includes('?') ? '&' : '?') + 'workspace=' + encodeURIComponent(WS);
}

function setWs(name) {
  WS = name || '';
  if (WS) localStorage.setItem('nloop.ws', WS);
  else localStorage.removeItem('nloop.ws');
  location.href = withWs(location.pathname);   // reload halaman ini di workspace baru
}

async function api(path, opts) {
  const r = await fetch(apiUrl(path), opts);
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
    <a class="brand" href="${withWs('/')}">
      <span class="logo">∞</span>
      <span class="brand-txt">nloop<small>loop engineering</small></span>
    </a>
    <div id="ws-switch" class="ws-switch"></div>
    <nav class="nav">
      ${NAV.map((n) => `
        <a class="nav-item ${n.key === active ? 'active' : ''}" href="${withWs(n.href)}">
          <span class="ico">${n.icon}</span>${n.label}
          <span class="nav-count" data-count="${n.key}"></span>
        </a>`).join('')}
    </nav>
    <div class="side-foot">
      <span class="dot" id="health-dot"></span><span id="health-txt">connecting…</span>
    </div>`;
  revealChildren($('.nav', side));
  renderWorkspaces();
  pollShell();
  setInterval(pollShell, 5000);
}

/** Switcher workspace. Satu workspace doang → label diam aja (nggak usah dropdown). */
async function renderWorkspaces() {
  const el = $('#ws-switch');
  if (!el) return;
  let data;
  try {
    data = await api('/api/workspaces');
  } catch {
    return;                       // server lama tanpa endpoint ini — sidebar tetap jalan
  }
  WORKSPACES = data.workspaces || [];
  if (!WS) WS = data.primary || '';
  if (WORKSPACES.length < 2) {
    const only = WORKSPACES[0];
    el.innerHTML = only
      ? `<span class="ws-one" title="workspace">🗂 ${esc(only.label)}</span>` : '';
    return;
  }
  el.innerHTML = `
    <select id="ws-select" class="select" title="workspace">
      ${WORKSPACES.map((w) => `
        <option value="${esc(w.name)}" ${w.name === WS ? 'selected' : ''}>
          🗂 ${esc(w.label)}${w.active ? ` (${w.active})` : ''}
        </option>`).join('')}
    </select>`;
  $('#ws-select').onchange = (e) => setWs(e.target.value);
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
reveal();                        // section statis yang udah nulis class="reveal" di HTML
