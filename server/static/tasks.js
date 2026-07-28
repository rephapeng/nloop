// Halaman Tasks (daftar) + Task detail (spec + form "Test task" + run terakhir).
// Satu file, dua halaman — dipilih lewat <body data-page>.
'use strict';

function taskCard(t) {
  const last = t.last_run;
  const req = (t.required || []).map((k) => `<code>${esc(k)}</code>`).join(' ') || '—';
  return `
    <a class="task-card card" href="/tasks/${esc(t.id)}">
      <div class="tc-head">
        <span class="tc-icon">⚡</span>
        <div>
          <div class="tc-name">${esc(t.name || t.id)}</div>
          <code class="tc-id">${esc(t.id)}</code>
        </div>
        ${t.triggerable === false ? '<span class="chip">built-in</span>' : ''}
      </div>
      ${t.description ? `<p class="tc-desc">${esc(t.description)}</p>` : ''}
      <div class="tc-foot">
        <span class="faint">payload: ${req}</span>
        <span class="spacer"></span>
        ${last ? `${badge(last.status)}<span class="faint">${timeAgo(last.created_at)}</span>`
               : '<span class="faint">belum pernah jalan</span>'}
      </div>
    </a>`;
}

async function initTasks() {
  let items;
  try {
    items = await api('/api/tasks');
  } catch (e) { return showError(e.message); }
  $('#tasks').innerHTML = items.length ? items.map(taskCard).join('')
    : `<div class="empty">Registry masih kosong.<br>
       Isi <code>tasks:</code> di <code>config.yaml</code> atau bikin
       <code>tasks/&lt;id&gt;.yaml</code>, terus restart nloop.</div>`;
}

// ---------- detail ----------

function specRow(label, value, mono) {
  if (!value) return '';
  return `<div><dt>${esc(label)}</dt><dd>${
    mono ? `<code>${esc(value)}</code>` : esc(value)}</dd></div>`;
}

function payloadForm(t) {
  const keys = [...new Set([...(t.required || []), ...Object.keys(t.defaults || {})])];
  if (t.triggerable === false) {
    return `<div class="hint-box">Task bawaan (dipicu webhook/watchdog) — nggak bisa
            di-trigger manual dari sini.</div>`;
  }
  return `
    <form id="test-form">
      ${keys.length ? `<div class="grid">${keys.map((k) => `
        <label class="field">${esc(k)}
          ${(t.required || []).includes(k) ? '<span class="hint">— wajib</span>' : ''}
          <input name="p-${esc(k)}" value="${esc((t.defaults || {})[k] ?? '')}"
                 ${(t.required || []).includes(k) ? 'required' : ''}>
        </label>`).join('')}</div>`
      : '<p class="faint">Task ini nggak butuh payload.</p>'}
      <details class="advanced">
        <summary>Payload tambahan (JSON) + override limit</summary>
        <div class="grid">
          <label class="field full">Extra payload (JSON object)
            <textarea name="extra" rows="2" placeholder='{"catatan": "sekali jalan"}'></textarea>
          </label>
          <label class="field">Max iterations
            <input name="max_iterations" type="number" min="1"
                   placeholder="${t.max_iterations ?? 'default'}">
          </label>
          <label class="field">Max cost (USD)
            <input name="max_cost_usd" type="number" step="0.5" min="0.5"
                   placeholder="${t.max_cost_usd ?? 'default'}">
          </label>
        </div>
      </details>
      <div class="form-actions">
        <button class="primary" type="submit">▶ Run task</button>
        <span id="test-msg" class="form-error"></span>
      </div>
    </form>`;
}

function bindTestForm(taskId) {
  const form = $('#test-form');
  if (!form) return;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    const payload = {};
    for (const [k, v] of f.entries()) {
      if (k.startsWith('p-') && v !== '') {
        try { payload[k.slice(2)] = JSON.parse(v); } catch { payload[k.slice(2)] = v; }
      }
    }
    const body = {payload};
    if (f.get('extra')) {
      try {
        Object.assign(payload, JSON.parse(f.get('extra')));
      } catch {
        $('#test-msg').textContent = 'extra payload bukan JSON valid';
        return;
      }
    }
    if (f.get('max_iterations')) body.max_iterations = +f.get('max_iterations');
    if (f.get('max_cost_usd')) body.max_cost_usd = +f.get('max_cost_usd');
    try {
      const out = await postJSON(`/api/tasks/${taskId}/trigger`, body);
      if (out.deduped) {
        $('#test-msg').innerHTML = `run <a href="/run/${esc(out.run_id)}">${
          esc(out.run_id)}</a> dengan key yang sama masih aktif — nggak bikin baru`;
        return;
      }
      location.href = `/run/${out.run_id}`;
    } catch (err) {
      $('#test-msg').textContent = err.message;
    }
  });
}

function runRowMini(r) {
  return `
    <tr data-goto="/run/${esc(r.id)}">
      <td>${badge(r.status)}</td>
      <td><code>${esc(r.id)}</code></td>
      <td class="cell-goal"><div class="sub">${esc(
        r.payload ? JSON.stringify(r.payload) : '')}</div></td>
      <td class="num">${r.iterations_done}/${r.max_iterations}</td>
      <td class="num">${fmtCost(r.cost_total)}</td>
      <td class="num">${runDuration(r)}</td>
      <td class="num">${timeAgo(r.created_at)}</td>
    </tr>`;
}

async function initTaskDetail() {
  const taskId = decodeURIComponent(location.pathname.split('/').pop());
  $('#task-id').textContent = taskId;
  let t;
  try {
    t = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
  } catch (e) {
    return showError(`task '${taskId}' nggak ketemu (${e.message})`);
  }
  $('#task-name').textContent = t.name || t.id;
  $('#task-desc').textContent = t.description || '';

  $('#spec').innerHTML = `
    <dl class="sd-rows">
      ${specRow('workdir', t.workdir, true)}
      ${specRow('verify_cmd', t.verify_cmd, true)}
      ${specRow('role', t.role)}
      ${specRow('context_cmd', t.context_cmd, true)}
      ${specRow('idempotency_key', t.idempotency_key, true)}
      ${specRow('on_success_cmd', t.on_success_cmd, true)}
      ${specRow('max_iterations', t.max_iterations)}
      ${specRow('max_cost_usd', t.max_cost_usd)}
    </dl>
    ${t.goal ? `<h4>goal template</h4><pre>${esc(t.goal)}</pre>` : ''}
    ${t.gate_prompt ? `<h4>quality gate</h4><pre>${esc(t.gate_prompt)}</pre>` : ''}`;

  $('#test-panel').innerHTML = payloadForm(t);
  bindTestForm(encodeURIComponent(taskId));

  const runs = t.runs || [];
  $('#task-runs').innerHTML = runs.length ? runs.map(runRowMini).join('')
    : '<tr><td colspan="7"><div class="empty">Belum ada run buat task ini.</div></td></tr>';
  document.addEventListener('click', (e) => {
    const row = e.target.closest('#task-runs [data-goto]');
    if (row) location.href = row.dataset.goto;
  });
}

({tasks: initTasks, task: initTaskDetail})[document.body.dataset.page]();
