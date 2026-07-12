// nloop dashboard — vanilla JS, no build. Halaman dipilih via <body data-page>.
'use strict';

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const fmtCost = (c) => '$' + (c || 0).toFixed(3);
const badge = (s) => `<span class="badge ${esc(s)}">${esc(s)}</span>`;
const ACTIVE = ['queued', 'running'];

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()) || r.status);
  return r.json();
}

// ---------- index ----------

async function refreshList() {
  const runs = await api('/api/loops');
  $('#runs').innerHTML = runs.length ? runs.map((r) => `
    <tr>
      <td><a href="/run/${esc(r.id)}"><code>${esc(r.id)}</code></a></td>
      <td class="goal" title="${esc(r.goal)}">${esc(r.goal)}</td>
      <td>${badge(r.status)}</td>
      <td>${r.iterations_done}/${r.max_iterations}</td>
      <td>${fmtCost(r.cost_total)}</td>
      <td>${ACTIVE.includes(r.status)
        ? `<button class="danger" data-stop="${esc(r.id)}">Stop</button>` : ''}</td>
    </tr>`).join('')
    : '<tr><td colspan="6" style="color:var(--muted)">belum ada loop — bikin di atas</td></tr>';
}

function initIndex() {
  refreshList();
  setInterval(refreshList, 3000);

  $('#new-loop').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { goal: f.get('goal'), verify_cmd: f.get('verify_cmd') };
    if (f.get('workdir')) body.workdir = f.get('workdir');
    if (f.get('model')) body.model = f.get('model');
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
    const id = e.target.dataset && e.target.dataset.stop;
    if (id) {
      await api(`/api/loops/${id}/stop`, { method: 'POST' });
      refreshList();
    }
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

async function loadRun(runId) {
  const run = await api(`/api/loops/${runId}`);
  $('#goal').textContent = run.goal;
  $('#status-badge').innerHTML = badge(run.status);
  $('#iter').textContent = `${run.iterations_done}/${run.max_iterations}`;
  $('#cost').textContent = fmtCost(run.cost_total);
  $('#verify-cmd').textContent = run.verify_cmd;
  $('#stop').style.display = ACTIVE.includes(run.status) ? '' : 'none';

  if (run.iterations.length) {
    $('#iterations').innerHTML = run.iterations.map((it) => `
      <details>
        <summary>
          iterasi ${it.idx} — ${esc(it.reason)} · ${it.turns} turns · ${fmtCost(it.cost)}
        </summary>
        <div class="body">
          <h2>Verifier output (sebelum aksi)</h2>
          <pre>${esc(it.verifier_output)}</pre>
          <h2>Hasil aksi</h2>
          <pre>${esc(it.result_text)}</pre>
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
  es.addEventListener('init', () => add('status', '▶ claude session mulai'));
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
  es.addEventListener('result', (e) => {
    const d = data(e);
    add('result',
      `iterasi selesai: ${d.subtype} · ${d.num_turns} turns · ${fmtCost(d.cost_usd)}`);
    loadRun(runId);
  });
  es.addEventListener('status', (e) => {
    const d = data(e);
    add('status', `status → ${d.status}${d.reason ? ' (' + d.reason + ')' : ''}`);
    loadRun(runId);
  });
  es.addEventListener('done', () => {
    es.close();
    add('status', '■ stream selesai');
    loadRun(runId);
  });
  es.onerror = () => add('fail', '⚠ koneksi stream putus — reconnect otomatis…');
}

// ---------- boot ----------

({ index: initIndex, run: initRun })[document.body.dataset.page]();
