// Halaman Runs: tabel run + filter (status/task/cari) + form loop ad-hoc.
// Filter disimpan di query URL biar tautannya bisa di-share / di-bookmark.
'use strict';

const STATUSES = ['running', 'queued', 'succeeded', 'failed', 'stopped'];
let filters = {status: '', task: '', q: ''};
let taskOptions = [];

function readFilters() {
  const p = new URLSearchParams(location.search);
  filters = {status: p.get('status') || '', task: p.get('task') || '',
             q: p.get('q') || ''};
}

function writeFilters() {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) if (v) p.set(k, v);
  const qs = p.toString();
  history.replaceState(null, '', qs ? `/?${qs}` : '/');
}

function renderFilterBar() {
  $('#filters').innerHTML = `
    <div class="pills">
      <button class="pill ${!filters.status ? 'on' : ''}" data-status="">All</button>
      ${STATUSES.map((s) => `<button class="pill ${filters.status === s ? 'on' : ''}"
        data-status="${s}"><span class="dot ${s}"></span>${s}</button>`).join('')}
    </div>
    <span class="spacer"></span>
    <select id="f-task" class="select">
      <option value="">semua task</option>
      ${taskOptions.map((t) => `<option value="${esc(t)}" ${
        filters.task === t ? 'selected' : ''}>${esc(t)}</option>`).join('')}
    </select>
    <input id="f-q" class="search" placeholder="cari goal / run id…"
           value="${esc(filters.q)}">`;

  $$('#filters .pill').forEach((b) => b.addEventListener('click', () => {
    filters.status = b.dataset.status;
    writeFilters(); renderFilterBar(); refresh();
  }));
  $('#f-task').addEventListener('change', (e) => {
    filters.task = e.target.value; writeFilters(); refresh();
  });
  const q = $('#f-q');
  let typing = null;
  q.addEventListener('input', () => {          // debounce: jangan nembak API tiap ketikan
    filters.q = q.value;
    writeFilters();
    clearTimeout(typing);
    typing = setTimeout(refresh, 200);
  });
}

function runRow(r) {
  const pct = Math.min(100, (r.iterations_done / (r.max_iterations || 1)) * 100);
  const barCls = r.status === 'succeeded' ? 'ok' : r.status === 'failed' ? 'bad' : '';
  const goal = (r.goal || '').split('\n')[0];
  const src = r.fingerprint && r.fingerprint.startsWith('schedule:')
    ? `🗓 ${esc(r.fingerprint.slice(9))}` : '';
  return `
    <tr data-goto="/run/${esc(r.id)}">
      <td>${badge(r.status)}</td>
      <td class="cell-goal">
        <div class="goal" title="${esc(r.goal)}">${esc(goal)}</div>
        <div class="sub"><code>${esc(r.id)}</code>${src ? ' · ' + src : ''}${
          r.role ? ' · role: ' + esc(r.role) : ''}</div>
      </td>
      <td>${r.task_id
        ? `<a class="chip task" href="/tasks/${esc(r.task_id)}">⚡ ${esc(r.task_id)}</a>`
        : '<span class="faint">—</span>'}</td>
      <td class="num">
        <div class="nums"><b>${r.iterations_done}</b>/${r.max_iterations}</div>
        <div class="bar ${barCls}"><i style="width:${pct}%"></i></div>
      </td>
      <td class="num"><b>${fmtCost(r.cost_total)}</b><div class="sub">/ ${
        fmtCost(r.max_cost_usd)}</div></td>
      <td class="num">${runDuration(r)}</td>
      <td class="num" title="${new Date((r.created_at || 0) * 1000).toLocaleString()}">${
        timeAgo(r.created_at)}</td>
      <td class="num">${ACTIVE.includes(r.status)
        ? `<button class="danger small" data-stop="${esc(r.id)}">Stop</button>` : ''}</td>
    </tr>`;
}

function matches(r) {
  if (!filters.q) return true;
  const q = filters.q.toLowerCase();
  return (r.goal || '').toLowerCase().includes(q) || r.id.includes(q)
      || (r.task_id || '').toLowerCase().includes(q);
}

async function refresh() {
  const p = new URLSearchParams({limit: '200'});
  if (filters.status) p.set('status', filters.status);
  if (filters.task) p.set('task', filters.task);
  let runs;
  try {
    runs = (await api(`/api/loops?${p}`)).filter(matches);
  } catch (e) {
    showError(e.message);
    return;
  }
  $('#count').textContent = runs.length ? `${runs.length} run${runs.length > 1 ? 's' : ''}` : '';
  $('#runs').innerHTML = runs.length ? runs.map(runRow).join('')
    : `<tr><td colspan="8"><div class="empty">Belum ada run yang cocok.<br>
       Bikin lewat <b>＋ New loop</b>, halaman <a href="/tasks">Tasks</a>,
       CLI <code>bin/nloop run</code>, schedule, webhook, atau Telegram.</div></td></tr>`;
}

async function loadTaskOptions() {
  try {
    taskOptions = (await api('/api/tasks')).map((t) => t.id);
  } catch { taskOptions = []; }
  renderFilterBar();
}

function initForm() {
  $('#toggle-form').addEventListener('click', () => {
    const f = $('#new-loop');
    f.hidden = !f.hidden;
    if (!f.hidden) f.querySelector('[name=goal]').focus();
  });

  $('#new-loop').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = {goal: f.get('goal'), verify_cmd: f.get('verify_cmd')};
    for (const k of ['workdir', 'model', 'role', 'context_cmd', 'gate_prompt']) {
      if (f.get(k)) body[k] = f.get(k);
    }
    if (f.get('max_iterations')) body.max_iterations = +f.get('max_iterations');
    if (f.get('max_cost_usd')) body.max_cost_usd = +f.get('max_cost_usd');
    try {
      const r = await postJSON('/api/loops', body);
      location.href = `/run/${r.run_id}`;
    } catch (err) {
      $('#form-error').textContent = err.message;
    }
  });
}

function initIndex() {
  readFilters();
  renderFilterBar();
  loadTaskOptions();
  refresh();
  setInterval(refresh, 3000);
  initForm();

  document.addEventListener('click', async (e) => {
    const stopId = e.target.dataset && e.target.dataset.stop;
    if (stopId) {
      e.stopPropagation();
      await postJSON(`/api/loops/${stopId}/stop`);
      refresh();
      return;
    }
    const row = e.target.closest('[data-goto]');
    if (row && !e.target.closest('a,button')) location.href = row.dataset.goto;
  });
}

initIndex();
