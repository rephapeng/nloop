// Halaman Run: waterfall span (trace) + log live SSE.
// Trace-nya dihitung server (engine/trace.py) — di sini murni rendering.
'use strict';

const runId = location.pathname.split('/').pop();
let traceData = null;
let selected = null;
let logFilter = 'all';
let poller = null;

const SPAN_ICON = {run: '∞', iteration: '↻', verify: '✓', act: '🤖', tool: '🔧',
                   gate: '🛡', postrun: '🚀'};

// ---------- header ----------

function setBar(el, ratio, warnAt) {
  el.querySelector('i').style.width = Math.min(100, ratio * 100) + '%';
  el.className = 'bar' + (ratio >= 1 ? ' bad' : ratio >= (warnAt ?? 2) ? ' warn' : '');
}

async function loadRun() {
  const run = await api(`/api/loops/${runId}`);
  $('#goal').textContent = run.goal;
  $('#status-badge').innerHTML = badge(run.status);
  $('#stop').style.display = ACTIVE.includes(run.status) ? '' : 'none';
  $('#crumb-task').innerHTML = run.task_id
    ? `<a href="/tasks/${esc(run.task_id)}">⚡ ${esc(run.task_id)}</a> /` : '';

  $('#chips').innerHTML = [
    run.payload ? `<span class="chip">payload: <code>${
      esc(JSON.stringify(run.payload))}</code></span>` : '',
    `<span class="chip">verify: <code>${esc(run.verify_cmd)}</code></span>`,
    `<span class="chip">workdir: <code>${esc(run.workdir)}</code></span>`,
    run.model ? `<span class="chip">model: ${esc(run.model)}</span>` : '',
    run.role ? `<span class="chip role">role: ${esc(run.role)}</span>` : '',
    run.context_cmd ? `<span class="chip">grounding: <code>${
      esc(run.context_cmd)}</code></span>` : '',
    run.gate_prompt ? `<span class="chip gate" title="${
      esc(run.gate_prompt)}">quality gate on</span>` : '',
    runDuration(run) ? `<span class="chip">⏱ ${runDuration(run)}</span>` : '',
  ].join('');

  $('#iter').textContent = `${run.iterations_done}/${run.max_iterations}`;
  setBar($('#iter-bar'), run.iterations_done / (run.max_iterations || 1));
  $('#cost').textContent = `${fmtCost(run.cost_total)} / ${fmtCost(run.max_cost_usd)}`;
  setBar($('#cost-bar'), (run.cost_total || 0) / (run.max_cost_usd || 1), 0.8);

  if (!ACTIVE.includes(run.status) && poller) {  // run final → berhenti polling
    clearInterval(poller);
    poller = null;
  }
  return run;
}

// ---------- waterfall ----------

/** Spans (flat, punya parent_id) → urutan DFS + kedalaman, biar bisa di-render baris. */
function orderSpans(spans) {
  const byParent = new Map();
  for (const s of spans) {
    const k = s.parent_id || '';
    if (!byParent.has(k)) byParent.set(k, []);
    byParent.get(k).push(s);
  }
  for (const list of byParent.values()) list.sort((a, b) => a.start - b.start);
  const out = [];
  const walk = (parent, depth) => {
    for (const s of byParent.get(parent) || []) {
      out.push({span: s, depth});
      walk(s.id, depth + 1);
    }
  };
  walk('', 0);
  return out;
}

function renderTrace(t) {
  traceData = t;
  const total = Math.max(0.001, t.end - t.start);
  const rows = orderSpans(t.spans);
  $('#waterfall').innerHTML = rows.map(({span: s, depth}) => {
    const left = ((s.start - t.start) / total) * 100;
    const width = Math.max(0.5, (s.duration / total) * 100);
    const share = s.duration / total;
    return `
      <div class="span-row ${s.status} ${selected === s.id ? 'sel' : ''}"
           data-span="${esc(s.id)}" title="${esc(s.name)} — ${fmtDur(s.duration)}">
        <div class="span-name" style="padding-left:${depth * 14}px">
          <span class="ico">${SPAN_ICON[s.kind] || '•'}</span>
          <span class="txt">${esc(s.name)}</span>
        </div>
        <div class="span-track">
          <i class="span-bar ${s.status} ${s.approx ? 'approx' : ''}"
             style="left:${left}%;width:${width}%"></i>
        </div>
        <div class="span-dur ${share > 0.4 ? 'hot' : ''}">${fmtDur(s.duration)}</div>
      </div>`;
  }).join('');

  $$('#waterfall .span-row').forEach((el) => el.addEventListener('click', () => {
    selected = el.dataset.span;
    renderTrace(traceData);
    renderSpanDetail();
  }));
  if (selected) renderSpanDetail();
}

function pre(label, text) {
  return text ? `<h4>${esc(label)}</h4><pre>${esc(text)}</pre>` : '';
}

function renderSpanDetail() {
  const s = (traceData.spans || []).find((x) => x.id === selected);
  const el = $('#span-detail');
  if (!s) {
    el.innerHTML = '<div class="hint-box">Klik salah satu span buat lihat detailnya.</div>';
    return;
  }
  const d = s.detail || {};
  const rows = [
    ['durasi', fmtDur(s.duration) + (s.approx ? ' (taksiran)' : '')],
    ['mulai', new Date(s.start * 1000).toLocaleTimeString()],
    d.exit_code !== undefined && d.exit_code !== null ? ['exit code', d.exit_code] : null,
    d.turns ? ['turns', d.turns] : null,
    d.cost ? ['cost', fmtCost(d.cost)] : null,
    d.reason ? ['reason', d.reason] : null,
    d.cmd ? ['cmd', d.cmd] : null,
  ].filter(Boolean);

  el.innerHTML = `
    <div class="sd-head">
      <span class="ico">${SPAN_ICON[s.kind] || '•'}</span>
      <b>${esc(s.name)}</b>
      ${s.status ? badge(({ok: 'succeeded', fail: 'failed', warn: 'stopped',
                           running: 'running'})[s.status] || 'queued') : ''}
    </div>
    ${s.approx ? '<div class="hint-box small">Durasi span ini <b>ditaksir</b> dari jarak antar event — stream cuma nyimpen satu timestamp.</div>' : ''}
    <dl class="sd-rows">${rows.map(([k, v]) =>
      `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>
    ${d.reasons && d.reasons.length
      ? `<h4>alasan gate</h4><ul class="sd-list">${
          d.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}
    ${pre('input', typeof d.input === 'string' ? d.input : d.input && JSON.stringify(d.input))}
    ${pre('output', d.output)}
    ${pre('hasil act', d.result_text)}`;
}

async function loadTrace() {
  try {
    renderTrace(await api(`/api/loops/${runId}/trace`));
  } catch (e) {
    showError(e.message);
  }
}

// ---------- log ----------

function initLogFilter() {
  $$('#log-filter .pill').forEach((b) => b.addEventListener('click', () => {
    logFilter = b.dataset.filter;
    $$('#log-filter .pill').forEach((x) => x.classList.toggle('on', x === b));
    $$('#live .ev').forEach((ev) => {
      ev.hidden = logFilter !== 'all' && !ev.classList.contains(`f-${logFilter}`);
    });
  }));
}

function streamEvents() {
  const live = $('#live');
  const add = (cls, group, text) => {
    const div = document.createElement('div');
    div.className = `ev ${cls} f-${group}`;
    div.textContent = text;
    div.hidden = logFilter !== 'all' && group !== logFilter;
    live.appendChild(div);
    live.scrollTop = live.scrollHeight;
  };
  const data = (e) => JSON.parse(e.data);
  let dirty = false;
  const touch = () => { dirty = true; };
  setInterval(() => {           // trace di-refresh ter-throttle, bukan tiap event
    if (!dirty) return;
    dirty = false;
    loadRun().catch(() => {});
    loadTrace();
  }, 1500);

  const es = new EventSource(`/api/loops/${runId}/events`);
  es.addEventListener('init', () => add('status', 'other', '▶ claude session started'));
  es.addEventListener('turn', (e) => add('turn', 'turn', data(e).text));
  es.addEventListener('tool', (e) => {
    const d = data(e);
    add('tool', 'tool', `🔧 ${d.name} ${d.input || ''}`);
    touch();
  });
  es.addEventListener('verify', (e) => {
    const d = data(e);
    add(d.passed ? 'pass' : 'fail', 'verify',
      `verify: ${d.passed ? 'PASS ✓' : 'FAIL ✗'} (exit ${d.exit_code})${
        d.duration ? ' · ' + fmtDur(d.duration) : ''}`);
    touch();
  });
  es.addEventListener('gate', (e) => {
    const d = data(e);
    add(d.passed ? 'gate-pass' : 'gate-fail', 'verify',
      d.passed ? '🛡 quality gate: PASSED'
               : `🛡 quality gate: REJECTED — ${(d.reasons || []).join('; ')}`);
    touch();
  });
  es.addEventListener('postrun', (e) => {
    const d = data(e);
    add(d.ok ? 'pass' : 'fail', 'verify',
      `🚀 rilis: ${d.ok ? 'OK' : 'GAGAL'} — ${d.cmd}`);
    touch();
  });
  es.addEventListener('log', (e) => {
    const d = data(e);
    add(d.level === 'warn' ? 'warn' : 'status', 'warn', `⚠ ${d.msg}`);
  });
  es.addEventListener('result', (e) => {
    const d = data(e);
    add('result', 'other',
      `iteration done: ${d.subtype} · ${d.num_turns} turns · ${fmtCost(d.cost_usd)}`);
    touch();
  });
  es.addEventListener('status', (e) => {
    const d = data(e);
    add('status', 'other', `status → ${d.status}${d.reason ? ' (' + d.reason + ')' : ''}`);
    touch();
  });
  es.addEventListener('done', () => {
    es.close();
    add('status', 'other', '■ stream closed');
    loadRun().catch(() => {});
    loadTrace();
  });
  es.onerror = () => add('fail', 'warn', '⚠ stream disconnected — reconnecting…');
}

async function init() {
  $('#run-id').textContent = runId;
  $('#stop').addEventListener('click', () => postJSON(`/api/loops/${runId}/stop`));
  initLogFilter();
  let run;
  try {
    run = await loadRun();
  } catch (e) {
    $('#waterfall').innerHTML = '';
    return showError(`run '${runId}' nggak kebaca: ${e.message}`);
  }
  await loadTrace();
  streamEvents();
  if (ACTIVE.includes(run.status)) {
    poller = setInterval(() => { loadRun().catch(() => {}); loadTrace(); }, 3000);
  }
}

init();
