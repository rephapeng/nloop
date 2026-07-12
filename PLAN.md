# PLAN — nloop: Loop Engine + Dashboard (Claude CLI / subscription)

> Tool "loop engineering": loop otonom yang `observe → act → verify → recover` sampai
> goal terverifikasi tercapai. Engine-nya nge-spawn `claude -p` (auth ngikut subscription,
> BUKAN API berbayar). Ada dashboard monitoring real-time via SSE.

## Keputusan arsitektur (final)

| Aspek | Pilihan | Alasan |
|---|---|---|
| Bahasa | **Python 3.11+**, FastAPI (async) | Ringan, reuse pola `run_claude_code` dari refan |
| LLM | **`claude -p` (subprocess)** | Subscription-friendly; unset `CLAUDECODE` + `ANTHROPIC_API_KEY` |
| State + Queue | **SQLite** (WAL mode) | Zero-ops, no Redis/Postgres — hemat resource |
| Worker | **asyncio worker pool** + semaphore | Loop jalan di background, cap jumlah subprocess |
| Realtime | **SSE** (`stream-json` → event bus) | Nonton loop live tanpa WebSocket ribet |
| Frontend | **HTML + vanilla JS (EventSource)** | Tanpa Node/React build → hemat resource |

**Prinsip hemat resource (karena tiap loop = pohon subprocess Claude):**
- `MAX_CONCURRENT_LOOPS` (semaphore) — batasi jumlah `claude` yang jalan barengan.
- `--max-turns` + `timeout` per iterasi — cegah 1 iterasi meledak.
- Parse `stream-json` incremental (jangan buffer output gede).
- SQLite WAL + tanpa broker eksternal.

---

## Struktur repo

```
nloop/
├── engine/
│   ├── claude_cli.py     # adapter subprocess -> claude -p (subscription-safe, stream-json)
│   ├── verifier.py       # verifikasi goal deterministik (exit-code perintah shell)
│   ├── loop.py           # inti loop: observe->act->verify->recover + guardrails
│   ├── memory/
│   │   ├── base.py       # MemoryProvider (ABC) + tipe data Memory + factory get_memory_provider()
│   │   ├── hot.py        # Tier 1: kurasi CLAUDE.md + journal.jsonl (SELALU lokal, bukan provider)
│   │   ├── local.py      # provider: SQLite FTS5 (+opsional sqlite-vec) — offline, no server
│   │   ├── selfmem.py    # provider: selfmem MCP/HTTP (auto_recall/auto_save/search)
│   │   └── tencent.py    # provider: TencentDB VectorDB (STUB — future)
│   ├── store.py          # SQLite: runs, iterations, events (persist + replay)
│   ├── events.py         # in-memory pub/sub (asyncio.Queue per run) buat SSE
│   └── worker.py         # ambil job dari queue, jalanin loop, hormati semaphore
├── workspaces/           # per-run workdir; tiap run punya CLAUDE.md + journal.jsonl
├── server/
│   ├── app.py            # FastAPI: REST + SSE endpoint
│   └── static/
│       ├── index.html    # daftar loop + tombol "New loop"
│       ├── run.html      # detail 1 loop, live stream (EventSource)
│       └── app.js        # fetch + EventSource, render iterasi/turn/cost
├── config.yaml           # MAX_CONCURRENT_LOOPS, model, budget, memory.provider (local|selfmem)
├── requirements.txt      # fastapi, uvicorn, pyyaml  (SEDIKIT aja)
└── run.sh                # uvicorn server.app:app + start worker
```

---

## Data model (SQLite)

```sql
-- satu "loop run"
runs(
  id TEXT PK, goal TEXT, verify_cmd TEXT, workdir TEXT, model TEXT,
  status TEXT,          -- queued|running|succeeded|failed|stopped
  max_iterations INT, max_cost_usd REAL,
  cost_total REAL, iterations_done INT, session_id TEXT,
  created_at, started_at, ended_at
)
-- satu iterasi di dalam run
iterations(
  id PK, run_id FK, idx INT, prompt TEXT, result_text TEXT,
  cost REAL, turns INT, reason TEXT,           -- success|error_max_turns|timeout
  verifier_passed INT, verifier_output TEXT, started_at, ended_at
)
-- event stream buat SSE (replay + live)
events(
  id PK, run_id FK, ts, type TEXT,             -- log|turn|tool|token|verify|status
  payload TEXT                                 -- JSON
)
-- HINDSIGHT: memori lintas-run (pelajaran yang diendapkan)
lessons(
  id PK, run_id FK, scope TEXT,                -- domain/tag (mis. "pytest", "docker")
  text TEXT,                                   -- pelajaran singkat 1-3 kalimat
  kind TEXT,                                   -- fix|pitfall|fact|preference
  confidence REAL, uses INT, created_at, last_used_at
)
CREATE VIRTUAL TABLE lessons_fts USING fts5(text, scope, content='lessons');
```

`jobs` = pakai kolom `status='queued'` di `runs` (nggak perlu tabel queue terpisah). Worker
polling `SELECT ... WHERE status='queued' LIMIT n` → hemat & tahan restart.

---

## API (FastAPI)

| Method | Path | Fungsi |
|---|---|---|
| `POST` | `/api/loops` | Buat loop baru (goal, verify_cmd, workdir, guardrails) → `run_id`, status `queued` |
| `GET` | `/api/loops` | List semua run + status + cost |
| `GET` | `/api/loops/{id}` | Detail run + semua iterasi |
| `POST` | `/api/loops/{id}/stop` | Set flag stop (worker cek antar iterasi) |
| `GET` | `/api/loops/{id}/events` | **SSE** — replay event tersimpan lalu stream live |
| `GET` | `/` , `/run/{id}` | Serve dashboard statis |

**SSE flow:** worker emit event ke `events.py` (asyncio.Queue per run) **dan** persist ke
tabel `events`. Endpoint SSE: saat konek → kirim event lama dari DB → subscribe queue →
stream event baru. Kalau run udah selesai, kirim replay lalu tutup.

---

## Inti loop (engine/loop.py)

```
loop(run):
  session = None
  for i in 1..max_iterations:
    if stop_requested(run): -> status=stopped; break
    passed, out = verify(run.verify_cmd)        # OBSERVE
    emit(verify, passed, out)
    if passed: -> status=succeeded; break
    no_progress = (out == last_out and i>1)
    prompt = build_prompt(goal, out, no_progress)
    res = claude_cli.run(prompt, resume=session, on_event=emit)   # ACT (stream)
    session = res.session_id
    cost_total += res.cost
    persist_iteration(...)
    if cost_total > max_cost_usd: -> status=failed(budget); break
  final verify -> status
```

Kunci: **verifier deterministik terpisah** dari agent (agent nggak nilai dirinya selesai).

---

## Adapter subscription (engine/claude_cli.py) — inti kompatibilitas

```python
cmd = ["claude", "-p", prompt,
       "--output-format", "stream-json", "--verbose",
       "--permission-mode", "acceptEdits",
       "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep",
       "--max-turns", str(max_turns)]
if resume: cmd += ["--resume", resume]
if model:  cmd += ["--model", model]

env = os.environ.copy()
env.pop("CLAUDECODE", None)          # jgn kedeteksi nested session
env.pop("ANTHROPIC_API_KEY", None)   # PAKSA pakai login subscription, bukan API billing

# Popen + baca stdout baris-per-baris (stream-json) -> emit event turn/tool/token live
```

Baris `stream-json` yang dipetakan ke event SSE: `system/init` (session_id), `assistant`
(teks/turn), `tool_use`/`tool_result`, dan `result` final (cost, num_turns, subtype).

---

## Memory — biar konteks nggak buyar (engine/memory.py)

Masalah: `--resume` (session) itu volatile — kena **auto-compaction** pas panjang, jadi
konteks "buyar". Solusi: 3 tingkat memori, dari yang murah & selalu-ada sampai lintas-run.

### Tier 0 — Session resume (bawaan)
`--resume <session_id>` per run. Short-term, tapi JANGAN diandalkan sendiri (bisa ke-compact).

### Tier 1 — HOT memory: `CLAUDE.md` di workdir  ⭐ lever paling gede
`claude -p` **otomatis muat `CLAUDE.md`** dari workdir tiap request — jadi isinya kebal
compaction (reload tiap iterasi). Loop engine yang **meng-kurasi** file ini:
- Isi: GOAL, invariant/aturan, "fakta yang udah pasti", "jangan diulang: X".
- Ditulis ringkas & di-*cap* ukurannya (mis. maks ~2 KB) biar nggak bengkak.
- Ini pola **Ralph Loop / Cherny CLAUDE.md**: state nempel di file, bukan di context window.

### Tier 2 — EPISODIC: `workspaces/{run}/journal.jsonl`
Append tiap iterasi: `{idx, action_summary, verifier_passed, error_head, changed_files}`.
Fungsi: (a) bikin blok "APA YANG UDAH DICOBA" buat prompt berikutnya (anti ngulang),
(b) sumber buat dashboard timeline, (c) bahan mentah distilasi ke hindsight.

### Tier 3 — HINDSIGHT: pluggable `MemoryProvider` (backend bisa di-swap)
Biar loop BARU belajar dari loop LAMA. **Backend-agnostic** — pilih via `config.yaml`.
Semua provider implement interface yang sama, jadi loop nggak peduli backend-nya apa:

```python
class MemoryProvider(ABC):
    def recall(self, project_id, query, k=5) -> list[Memory]: ...   # sebelum loop / tiap iterasi
    def save(self, project_id, text, kind, confidence=0.6, source=None) -> str: ...
    def consolidate(self, project_id) -> None: ...   # opsional (dedup/compact)
    def forget(self, project_id, *, memory_id=None, decayed=True) -> None: ...  # opsional
    def is_available(self) -> bool: ...
```

Backend yang direncanain:

| Provider | Backend | Resource | Data | Status |
|---|---|---|---|---|
| `local` | SQLite FTS5 (+opsional `sqlite-vec`) | Nol eksternal, embedded | 100% lokal | v1 default (offline-safe) |
| `selfmem` | selfmem MCP/HTTP (`search_memory`/`save_memory`, GraphRAG, decay) | Nol lokal (hosted) | Ke server selfmem | v1 (project non-sensitif) |
| `tencent` | TencentDB VectorDB | Managed | Cloud lo sendiri | **future (stub)** |
| `hindsight-vec` | Embedding sendiri + vector store | Berat | Lokal | **future** |

Factory: `get_memory_provider(cfg)` — sama persis pola `get_llm_provider()` di refan.
Bisa **per-project**: project sensitif → `local`, sisanya → `selfmem`.

- **Recall (sebelum loop mulai):** `provider.recall(project_id, goal)` → top-K → suntik ke
  `CLAUDE.md` awal + prompt pertama.
- **Save/extract (akhir loop):** distilasi journal (1 `claude -p` murah, atau `auto_save`
  bawaan selfmem) → `provider.save(...)` dengan `confidence`.
- **Reinforce/decay/consolidate:** `provider.consolidate()` / `forget(decayed=True)`. Di
  `local` = update confidence + FTS reindex; di `selfmem` = `schedule_consolidation`/
  `request_forgetting` bawaannya. Loop cukup panggil interface, detail di provider.

> **Dua gaya integrasi selfmem** (didukung, konfigurable):
> **(a) engine-orchestrated** — loop yang manggil `provider.recall/save` (paling pluggable,
> seragam antar backend). **(b) agent-native** — kasih tool `mcp__selfmem__auto_recall`/
> `auto_save` ke `claude -p`, agent yang manggil sendiri. Default: **(a)**, biar swap backend
> nggak ngubah prompt.

### Alur memori di dalam loop
```
start run:
  provider = get_memory_provider(cfg)            # local | selfmem | tencent ...
  lessons  = provider.recall(project_id, goal)   # Tier 3
  hot.seed_claudemd(workdir, goal, lessons)      # Tier 1 (SELALU lokal)
each iteration:
  prompt = goal + verifier_output + hot.recent_journal(run)   # Tier 2 disuntik
  res = claude_cli.run(prompt, resume=session, cwd=workdir)   # Tier 0 + Tier 1 auto
  hot.append_journal(run, res, verifier)
  hot.curate_claudemd(workdir, res)              # fakta terverifikasi, cap ukuran
end run:
  lessons = distill(journal)                     # pilih yang LOLOS verifier (self-memory)
  for l in lessons: provider.save(project_id, l.text, l.kind, l.confidence)   # Tier 3
  provider.consolidate(project_id)               # opsional
```

### Anti-drift (biar goal nggak melenceng)
- GOAL selalu di baris atas `CLAUDE.md` **dan** tiap prompt (goal-lock).
- Verifier deterministik = sumber kebenaran "selesai", bukan klaim agent.
- Journal "udah dicoba" mencegah loop nyoba hal yang sama berulang.

---

## Dashboard (server/static)

- **index.html** — tabel run: goal, status (badge), iterasi ke-berapa, cost, tombol Stop + "New loop".
- **run.html** — detail 1 loop:
  - Header: goal, status live, cost berjalan, iterasi X/max.
  - Timeline iterasi (accordion): prompt, ringkasan aksi, hasil verifier (pass/fail + output).
  - Panel "Live" pakai `new EventSource('/api/loops/{id}/events')` → append turn/tool/token real-time.
  - Panel **Memory**: isi `CLAUDE.md` sekarang (hot), journal iterasi, lessons yang ke-recall.
  - Tombol **Stop**.
- Semua vanilla JS, no bundler. Styling 1 file CSS kecil.

---

## Fase implementasi (tiap fase punya acceptance test)

**Fase 0 — Scaffold.** Repo, `requirements.txt`, `config.yaml`, `run.sh`.
✅ `uvicorn` nyala, `/` balikin halaman kosong.

**Fase 1 — Claude adapter + verifier.** `claude_cli.run()` subscription-safe, parse `stream-json`; `verifier.verify()`.
✅ Script CLI kecil: `claude -p "echo hi"` jalan tanpa API key, balikin cost & session_id. Verifier `pytest -q` balikin exit-code benar.

**Fase 2 — Loop core + store (SQLite).** `loop.py` + `store.py`.
✅ Jalanin loop di repo test yang sengaja 1 test gagal → loop benerin → `succeeded`, tercatat di DB.

**Fase 3 — Worker + queue + semaphore.** `worker.py` ambil run `queued`, hormati `MAX_CONCURRENT_LOOPS`, cek flag stop, tahan restart.
✅ Antri 3 loop, cuma N jalan barengan; restart server → run `queued` lanjut.

**Fase 4 — API + SSE.** Endpoint REST + `/events` (replay + live).
✅ `curl -N /api/loops/{id}/events` streaming event saat loop jalan.

**Fase 5 — Dashboard.** index + run + app.js.
✅ Buka browser, bikin loop, nonton iterasi & cost update live, bisa Stop.

**Fase 5.5 — Memory (pluggable).**
- `memory/hot.py` — Tier 1 kurasi `CLAUDE.md` + Tier 2 journal (selalu lokal).
- `memory/base.py` — `MemoryProvider` ABC + factory.
- `memory/local.py` — provider default (SQLite FTS5). `memory/selfmem.py` — provider hosted.
- `memory/tencent.py` — stub interface (future).
✅ Ganti `memory.provider: local` ⇄ `selfmem` di config → loop jalan sama tanpa ubah kode.
✅ Loop A nabrak pitfall → tersimpan via provider. Loop B (goal mirip) auto-recall lesson itu di iterasi pertama & nggak ngulang. `CLAUDE.md` tetap kecil (ter-cap).

**Fase 6 — Hardening.** Budget alert, no-progress → ganti strategi/stop, timeout per iterasi, retry transient, log rotation.
✅ Loop rusak (goal mustahil) berhenti rapi di guardrail, bukan infinite/boros.

---

## Guardrails (wajib, dari prinsip loop engineering)

- Hard cap: `max_iterations`, `max_cost_usd`, `timeout` per iterasi, `--max-turns`.
- No-progress detection: verifier output identik 2x → ganti pendekatan / stop.
- Human checkpoint (opsional): flag `require_approval` sebelum aksi irreversible.
- Concurrency cap: `MAX_CONCURRENT_LOOPS` — lindungi resource dari ledakan subprocess.
- Semua biaya tercatat per-iterasi → dashboard nampilin total + per-run.

## Nanti (opsional, layer atas)

- Event-driven trigger (cron/webhook) → loop jalan otomatis.
- Hill-climbing loop: analisa run gagal → auto-perbaiki prompt/skill.
- Multi-agent: 1 loop nge-spawn sub-agent khusus (ingat: token ~4x–15x, ukur dulu).
