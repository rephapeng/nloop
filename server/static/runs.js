// Runs page: the run table + filters (status/task/search) + the ad-hoc loop form.
// Filters live in the URL query so a view stays shareable / bookmarkable.
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
  if (WS) p.set('ws', WS);          // don't lose it when the filters are rewritten
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
      <option value="">all tasks</option>
      ${taskOptions.map((t) => `<option value="${esc(t)}" ${
        filters.task === t ? 'selected' : ''}>${esc(t)}</option>`).join('')}
    </select>
    <input id="f-q" class="search" placeholder="search goal / run id…"
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
  q.addEventListener('input', () => {          // debounce: don't hit the API on every keystroke
    filters.q = q.value;
    writeFilters();
    clearTimeout(typing);
    typing = setTimeout(refresh, 200);
  });
}

const iterPct = (r) => Math.min(100, (r.iterations_done / (r.max_iterations || 1)) * 100);
const barCls = (r) => r.status === 'succeeded' ? 'ok' : r.status === 'failed' ? 'bad' : '';
const costCell = (r) => `<b>${fmtCost(r.cost_total)}</b><div class="sub">/ ${
  fmtCost(r.max_cost_usd)}</div>`;
const stopCell = (r) => ACTIVE.includes(r.status)
  ? `<button class="danger small" data-stop="${esc(r.id)}">Stop</button>` : '';

function runRow(r) {
  const goal = (r.goal || '').split('\n')[0];
  const src = r.fingerprint && r.fingerprint.startsWith('schedule:')
    ? `🗓 ${esc(r.fingerprint.slice(9))}` : '';
  return `
    <tr data-goto="/run/${esc(r.id)}" data-id="${esc(r.id)}">
      <td data-c="status">${badge(r.status)}</td>
      <td class="cell-goal">
        <div class="goal" title="${esc(r.goal)}">${esc(goal)}</div>
        <div class="sub"><code>${esc(r.id)}</code>${src ? ' · ' + src : ''}${
          r.role ? ' · role: ' + esc(r.role) : ''}</div>
      </td>
      <td>${r.task_id
        ? `<a class="chip task" href="/tasks/${esc(r.task_id)}">⚡ ${esc(r.task_id)}</a>`
        : '<span class="faint">—</span>'}</td>
      <td class="num" data-c="iter">
        <div class="nums"><b>${r.iterations_done}</b>/${r.max_iterations}</div>
        <div class="bar ${barCls(r)}"><i style="width:${iterPct(r)}%"></i></div>
      </td>
      <td class="num" data-c="cost">${costCell(r)}</td>
      <td class="num" data-c="dur">${runDuration(r)}</td>
      <td class="num" data-c="ago" title="${
        new Date((r.created_at || 0) * 1000).toLocaleString()}">${timeAgo(r.created_at)}</td>
      <td class="num" data-c="stop">${stopCell(r)}</td>
    </tr>`;
}

/** Update only the cells that changed, without tearing down the <tr>. */
function updateRow(tr, r) {
  if (!tr) return;
  const set = (c, html) => {
    const td = tr.querySelector(`[data-c="${c}"]`);
    if (td && td.innerHTML !== html) td.innerHTML = html;
  };
  set('status', badge(r.status));
  set('cost', costCell(r));
  set('dur', runDuration(r));
  set('ago', timeAgo(r.created_at));
  set('stop', stopCell(r));

  const cell = tr.querySelector('[data-c="iter"]');
  if (!cell) return;
  const nums = `<b>${r.iterations_done}</b>/${r.max_iterations}`;
  if (cell.querySelector('.nums').innerHTML !== nums) cell.querySelector('.nums').innerHTML = nums;
  // the bar is kept (not re-rendered) so that `transition: width` actually plays
  const bar = cell.querySelector('.bar');
  bar.className = `bar ${barCls(r)}`.trim();
  bar.querySelector('i').style.width = iterPct(r) + '%';
}

// The 3-second poll used to rewrite the whole tbody's innerHTML: the progress bar
// never got to transition, hover state reset, and selected text vanished. Now the
// DOM is only torn down when the list of runs genuinely differs.
let painted = null;

function paintRows(runs) {
  const tb = $('#runs');
  const ids = runs.map((r) => r.id).join(',');
  if (ids === painted) {
    runs.forEach((r) => updateRow(tb.querySelector(`tr[data-id="${CSS.escape(r.id)}"]`), r));
    return;
  }
  painted = ids;
  tb.innerHTML = runs.length ? runs.map(runRow).join('')
    : `<tr><td colspan="8"><div class="empty">No runs match yet.<br>
       Start one from <b>＋ New loop</b>, the <a href="/tasks">Tasks</a> page,
       the <code>bin/nloop run</code> CLI, a schedule, a webhook, or Telegram.</div></td></tr>`;
  revealChildren(tb, ':scope > tr');
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
  paintRows(runs);
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
    if (f.hidden) return;
    f.classList.remove('opening');
    void f.offsetWidth;                 // reflow: force the animation to restart on each open
    f.classList.add('opening');
    f.querySelector('[name=goal]').focus();
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
