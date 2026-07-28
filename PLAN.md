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

Guardrails & prinsip hemat resource: **satu sumber**, lihat section [Guardrails](#guardrails-wajib) di bawah.

---

## Struktur repo

```
nloop/
├── engine/
│   ├── config.py         # load config.yaml + defaults
│   ├── claude_cli.py     # adapter subprocess -> claude -p (subscription-safe, stream-json)
│   ├── verifier.py       # verifikasi goal deterministik (exit-code perintah shell)
│   ├── loop.py           # inti loop: observe->act->verify->recover + guardrails   (Fase 2)
│   ├── store.py          # SQLite: runs, iterations, events (persist + replay)     (Fase 2)
│   ├── events.py         # in-memory pub/sub (asyncio.Queue per run) buat SSE      (Fase 4)
│   ├── worker.py         # ambil job queued, jalanin loop, hormati semaphore       (Fase 3)
│   └── memory/
│       ├── hot.py        # Tier 1: kurasi CLAUDE.md + Tier 2: journal.jsonl (SELALU lokal)
│       ├── base.py       # MemoryProvider ABC + factory                            (Fase 7)
│       ├── local.py      # provider SQLite FTS5 — offline, no server               (Fase 7)
│       └── selfmem.py    # provider selfmem HTTP/MCP                               (Fase 7)
├── workspaces/           # per-run workdir; tiap run punya CLAUDE.md + journal.jsonl
├── server/
│   ├── app.py            # FastAPI: REST + SSE endpoint
│   └── static/           # index.html, run.html, app.js                            (Fase 5)
├── scripts/
│   └── smoke.py          # acceptance Fase 1: verifier + claude -p subscription-safe
├── config.yaml           # MAX_CONCURRENT_LOOPS, model, budget, memory.provider
├── requirements.txt      # fastapi, uvicorn, pyyaml  (SEDIKIT aja)
└── run.sh                # uvicorn server.app:app (+ worker mulai Fase 3)
```

---

## Data model (SQLite)

```sql
-- satu "loop run"
runs(
  id TEXT PK, goal TEXT, verify_cmd TEXT, workdir TEXT, model TEXT,
  status TEXT,          -- queued|running|succeeded|failed|stopped
  stop_requested INT,
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
-- (Fase 7) HINDSIGHT lintas-run — versi minimal; confidence/decay nyusul (lihat "Nanti")
lessons(id PK, run_id FK, scope TEXT, text TEXT, kind TEXT, created_at)
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

## Memory — biar konteks nggak buyar

Masalah: `--resume` (session) itu volatile — kena **auto-compaction** pas panjang, jadi
konteks "buyar". Solusi bertingkat; **v1 core = Tier 0–2** (murah, selalu ada, file ops
doang), Tier 3 nyusul di Fase 7 setelah loop-nya terbukti jalan & ter-hardening.

### Tier 0 — Session resume (bawaan)
`--resume <session_id>` per run. Short-term, tapi JANGAN diandalkan sendiri (bisa ke-compact).

### Tier 1 — HOT memory: `CLAUDE.md` di workdir  ⭐ lever paling gede
`claude -p` **otomatis muat `CLAUDE.md`** dari workdir tiap request — jadi isinya kebal
compaction (reload tiap iterasi). Loop engine yang **meng-kurasi** file ini:
- Isi: GOAL, invariant/aturan, "fakta yang udah pasti", "jangan diulang: X".
- Ditulis ringkas & di-*cap* ukurannya (maks ~2 KB) biar nggak bengkak.
- Ini pola **Ralph Loop / Cherny CLAUDE.md**: state nempel di file, bukan di context window.

### Tier 2 — EPISODIC: `workspaces/{run}/journal.jsonl`
Append tiap iterasi: `{idx, action_summary, verifier_passed, error_head, changed_files}`.
Fungsi: (a) bikin blok "APA YANG UDAH DICOBA" buat prompt berikutnya (anti ngulang),
(b) sumber buat dashboard timeline, (c) bahan mentah distilasi ke hindsight.

### Tier 3 — HINDSIGHT (Fase 7): pluggable `MemoryProvider`
Biar loop BARU belajar dari loop LAMA. Backend-agnostic via `config.yaml`, interface minimal:

```python
class MemoryProvider(ABC):
    def recall(self, project_id, query, k=5) -> list[Memory]: ...
    def save(self, project_id, text, kind, source=None) -> str: ...
    def is_available(self) -> bool: ...
```

Backend v1: **`local`** (SQLite FTS5, default offline-safe) dan **`selfmem`** (hosted,
`https://selfmem.com/mcp`, header `X-API-Key`). Bisa per-project: sensitif → `local`.
Factory `get_memory_provider(cfg)` — pola `get_llm_provider()` di refan.

Integrasi: **engine-orchestrated** — loop yang manggil `provider.recall/save`, seragam
antar backend, swap backend nggak ngubah prompt.

- **Recall (sebelum loop):** `provider.recall(project_id, goal)` → top-K → suntik ke
  `CLAUDE.md` awal + prompt pertama.
- **Save (akhir loop):** distilasi journal → HANYA lessons dari run yang **lolos verifier**
  yang disimpan (ide self-memory: verified-only promotion).

Sengaja DITUNDA ke "Nanti": provider tencent/hindsight-vec, gaya agent-native
(`mcp__selfmem__auto_*`), lifecycle `consolidate`/`forget`/decay/confidence.

### Alur memori di dalam loop
```
start run:
  lessons = provider.recall(project_id, goal)    # Tier 3 (Fase 7; sebelum itu: skip)
  hot.seed_claudemd(workdir, goal, lessons)      # Tier 1 (SELALU lokal)
each iteration:
  prompt = goal + verifier_output + hot.journal_block(run)    # Tier 2 disuntik
  res = claude_cli.run(prompt, resume=session, cwd=workdir)   # Tier 0 + Tier 1 auto
  hot.append_journal(run, res, verifier)
  hot.append_fact(workdir, ...)                  # fakta terverifikasi, cap ukuran
end run (Fase 7):
  for l in distill(journal_yang_lolos_verifier): provider.save(...)
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
  - Tombol **Stop**.
- Semua vanilla JS, no bundler. Styling 1 file CSS kecil.
- Panel Memory (isi CLAUDE.md, journal, lessons ke-recall) → "Nanti", setelah Fase 7.

---

## Fase implementasi (tiap fase punya acceptance test)

**Fase 0 — Scaffold.** Repo, `requirements.txt`, `config.yaml`, `run.sh`, app minimal.
✅ `uvicorn` nyala, `/api/health` balikin `{"ok": true}`.

**Fase 1 — Claude adapter + verifier.** `claude_cli.run()` subscription-safe, parse `stream-json`; `verifier.verify()`.
✅ `scripts/smoke.py`: verifier balikin exit-code benar; `claude -p` jalan TANPA API key, balikin cost & session_id.

**Fase 2 — Loop core + store (SQLite).** `loop.py` + `store.py` + `memory/hot.py` (Tier 1–2).
✅ Jalanin loop di repo test yang sengaja 1 test gagal → loop benerin → `succeeded`, tercatat di DB.

**Fase 3 — Worker + queue + semaphore.** `worker.py` ambil run `queued`, hormati `MAX_CONCURRENT_LOOPS`, cek flag stop, tahan restart.
✅ Antri 3 loop, cuma N jalan barengan; restart server → run `queued` lanjut.

**Fase 4 — API + SSE.** Endpoint REST + `/events` (replay + live).
✅ `curl -N /api/loops/{id}/events` streaming event saat loop jalan.

**Fase 5 — Dashboard.** index + run + app.js.
✅ Buka browser, bikin loop, nonton iterasi & cost update live, bisa Stop.

**Fase 6 — Hardening.** Budget alert, no-progress → ganti strategi/stop, timeout per iterasi, retry transient, log rotation.
✅ Loop rusak (goal mustahil) berhenti rapi di guardrail, bukan infinite/boros.
*(Sengaja SEBELUM memory: lessons jangan diendapkan dari run yang perilakunya masih rusak.)*

**Fase 7 — Reactive triggers (webhook Sentry/PostHog).** *(dinaikkan — use case utama:
issue masuk → loop benerin app otomatis)* `POST /api/hooks/{source}`: payload → goal
(judul error + stacktrace + link issue), workdir/verify_cmd dari mapping per-project di
config, dedup per issue fingerprint (issue sama nggak spawn loop dobel selama masih aktif).
✅ Kirim payload Sentry contoh → run baru muncul & jalan; kirim ulang → nggak dobel.

**Fase 8 — Memory pluggable (Tier 3).** `memory/base.py` (ABC + factory), `memory/local.py` (FTS5), `memory/selfmem.py`.
✅ Ganti `memory.provider: local` ⇄ `selfmem` di config → loop jalan sama tanpa ubah kode.
✅ Loop A nabrak pitfall → tersimpan. Loop B (goal mirip) auto-recall lesson di iterasi pertama & nggak ngulang. `CLAUDE.md` tetap kecil (ter-cap).

**Fase 9 — Port kapabilitas dtc-agent (SELESAI 2026-07-15).** Generalisasi engine
dtc-agent (github.com/rephapeng/dtc-agent) ke nloop; payload devtocash-specific
(SEO/cross-post) SENGAJA nggak diport — itu urusan project, loop bisa manggil via Bash.
- Roles + grounding: `engine/grounding.py` — `roles/common.md` + output `context_cmd`
  (segar tiap iterasi) + `roles/<role>.md` → `--append-system-prompt`.
- Subscription hygiene: `claude.lock_file` (flock single-flight lintas proses, pola
  `.claude.lock`), strip `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL` juga.
- LLM quality gate: `gate_prompt` per run — verifier lolos → reviewer terpisah
  (read-only, session sendiri, kontrak JSON last-line `{"pass":...}`); reject →
  loop lanjut dengan alasan sebagai feedback; biaya gate masuk budget.
- Scheduler: `engine/scheduler.py` — `schedules:` (at HH:MM UTC / every 6h),
  steps sekuensial + `always: true` (pola daily_pipeline), dedup fingerprint
  `schedule:<nama>`, endpoint `GET /api/schedules` + `POST /api/schedules/{n}/trigger`.
- Telegram: `engine/telegram.py` (task di lifespan) — notif run selesai, /loops /new
  /stop /status /reset, chat freeform → session Claude Code per-chat (`--resume` +
  retry fresh), tiering model (smalltalk → murah), redaksi secret, foto/dokumen →
  `incoming/`, allow-list fails closed. Secrets di `.env` (TELEGRAM_BOT_TOKEN,
  TELEGRAM_ALLOWED_CHAT_IDS).
- CLI `bin/nloop` (new/ls/show/stop/schedules/trigger/ask) + `deploy/self_restart.sh`
  (restart via systemd-run di luar cgroup biar balasan Telegram selamat).

**Fase 9b — Issue-fix pipeline penuh (SELESAI 2026-07-15).** Webhook run bukan cuma
"verify build" — siklus lengkap: repro → fix → validasi → rilis → tutup issue.
- Repro-first (`repro: true`, default di webhook): verify_cmd issue run =
  `sh .nloop/repro/<issue>.sh && (verify project)`. Script belum ada → verifier
  gagal → loop DIPAKSA investigasi + nulis repro dari stacktrace + fix beneran.
  (Tanpa ini, error runtime nggak bikin build merah → loop no-op.)
- `on_success_cmd` per project: fix terverifikasi 100% → langkah rilis (git push,
  docker compose up --build, dst.). Gagal → run FAILED reason `postrun_failed`.
- `triggers.sentry.resolve: true` + SENTRY_AUTH_TOKEN di .env → run sukses →
  issue di-mark resolved via API Sentry (gagal resolve = warning, bukan fail).
- Watchdog Sentry (`engine/watchdog.py`, section `watchdog:`): poll API
  `projects/{org}/{slug}/issues/?query=is:unresolved` tiap interval → spawn
  issue-fix run lewat jalur yang sama dengan webhook (`triggers.create_issue_run`).
  Guardrail: dedup fingerprint aktif, cooldown per-issue (default 24h), cap
  `max_per_tick`. Endpoint: `GET /api/watchdog`, `POST /api/watchdog/tick`.

**Fase 10 — Task registry + payload (SELESAI 2026-07-28).** Arah baru: bikin nloop
sedekat mungkin ke model kerja trigger.dev. Sampai Fase 9b, "run" itu barang sekali
pakai — goal + verify_cmd string yang disusun ulang di tiap entry path (REST,
scheduler, webhook, Telegram), nggak ada objek "task" yang bisa dilihat, dipanggil
ulang dengan input beda, atau di-test. Fase 10 nambahin unit kerja itu.
- `engine/tasks.py` — registry: `tasks:` di config.yaml ATAU `tasks/<id>.yaml`
  (satu file satu task; file menang atas config). Spec rusak di-log & di-skip,
  bukan bikin server mati (pola scheduler `_validate`).
- Template `{{var}}` / `{{payload.var}}` di goal/verify_cmd/workdir/context_cmd/
  gate_prompt/on_success_cmd/idempotency_key. Variabel nggak ada di payload =
  `TaskError` waktu trigger, BUKAN string kosong — goal bolong bikin agent ngarang.
- `payload: {required: [...], defaults: {...}}` — divalidasi sebelum run dibikin.
- `tasks.trigger(store, cfg, task_id, payload)` = SATU pintu semua entry path.
  Dedup pakai idempotency key (kolom `runs.fingerprint` yang udah ada): selama run
  dengan key sama masih aktif, trigger kedua nunjuk run itu, bukan bikin baru.
- Override per-trigger dibatasi `OVERRIDABLE` (model/limit/workdir). Payload nggak
  boleh ngubah `role`/`verify_cmd` — itu jalur eskalasi, bukan parameter.
- Data model: `runs.task_id` + `runs.payload` (JSON, di-decode di `Store`).
- API: `GET /api/tasks`, `GET /api/tasks/{id}`, `POST /api/tasks/{id}/trigger`,
  `POST /api/loops {task, payload}`, filter `GET /api/loops?task=&status=&limit=`.
  CLI: `nloop tasks`, `nloop run <task> k=v`. Schedule step: `task:` + `payload:`.
- Webhook/watchdog: pipeline issue-fix bawaan tetap (goal repro-first bikinan
  `build_goal`), cuma dicatat `task_id='issue-fix'` biar kekelompok di dashboard;
  project boleh nunjuk task registry sendiri lewat `triggers.projects.<x>.task`.
✅ `promo-pagi`/`promo-sore` yang dulu dua blok goal kembar → satu task `promo-post`
   dipanggil dua kali dengan payload `{slot: pagi|sore}`.
✅ Trigger task yang sama dua kali selagi aktif → run kedua nggak dibikin (deduped).
✅ Payload kurang → 400 sebelum run masuk antrian, bukan gagal di tengah loop.

**Fase 11 — Queue + retry + replay + run tree (BELUM).** Lanjutan pendekatan
trigger.dev, sisi eksekusi:
- Named queue (`queue:` di task) + cap concurrency per-queue, gantiin satu
  `asyncio.Semaphore` global — sekarang loop promo & loop issue-fix rebutan slot
  yang sama. Plus priority sederhana.
- Retry level-RUN + backoff (`retry: {max_attempts, backoff}`) + kolom `attempt`.
  Yang ada sekarang cuma retry dalam-iterasi buat error transient claude.
- `POST /api/loops/{id}/replay` — jalanin ulang task+payload yang sama sebagai run
  baru yang nunjuk induknya.
- `runs.parent_run_id` — step schedule jadi anak dari satu run pipeline (subtask),
  bukan run sejajar yang cuma nyambung lewat fingerprint.

**Fase 12 — Dashboard ala trigger.dev (SELESAI 2026-07-28, dimajuin sebelum Fase 11).**
Tetap vanilla JS tanpa build step; `app.js` lama dipecah jadi `common.js` (helper +
shell) + satu script per halaman.
- Shell sidebar: Runs / Tasks / Schedules + indikator run aktif & health server.
- **Runs**: tabel yang bisa difilter (pill status, filter task, cari goal/id).
  Filter disimpan di query URL → tampilan bisa di-share. Watchdog & schedule
  pindah ke halaman sendiri biar halaman run cuma soal run.
- **Tasks**: kartu registry + halaman detail berisi spec, run terakhir, dan form
  **Test task** yang dibangun dari `payload.required`/`defaults` — trigger manual
  tanpa perlu curl.
- **Run detail**: `engine/trace.py` nyusun span dari `iterations` + `events` (tanpa
  tabel baru, tanpa nulis apa-apa), di-serve `GET /api/loops/{id}/trace`, digambar
  jadi waterfall + panel detail span. Log lama tetap ada, sekarang bisa difilter
  (turn / tool / verify+gate / warning).
- Kejujuran durasi: verify/gate/postrun sekarang nyimpen durasi asli di payload
  event; act ambil dari tabel `iterations`. Span yang ujungnya cuma bisa DITAKSIR
  (tool — stream cuma punya satu timestamp) ditandai `approx` dan digambar
  bergaris miring. Run lama (sebelum fase ini) verify-nya juga approx.
- Tool span dipotong di `TOOL_CAP` per iterasi, sisanya diringkas jadi span
  "+N tool call lagi" — bukan dipotong diem-diem.

---

## Guardrails (wajib)

Satu-satunya section guardrails — fase & section lain nge-refer ke sini.

- Hard cap: `max_iterations`, `max_cost_usd`, `timeout` per iterasi, `--max-turns`.
- No-progress detection: verifier output identik 2x → ganti pendekatan / stop.
- Concurrency cap: `MAX_CONCURRENT_LOOPS` (semaphore) — tiap loop = pohon subprocess claude.
- Parse `stream-json` incremental (jangan buffer output gede).
- Human checkpoint (opsional): flag `require_approval` sebelum aksi irreversible.
- Semua biaya tercatat per-iterasi → dashboard nampilin total + per-run.

## Nanti (opsional, layer atas)

- Memory: provider `tencent` (TencentDB VectorDB) & `hindsight-vec` (embedding lokal);
  gaya agent-native selfmem (`mcp__selfmem__auto_recall/auto_save` sebagai tool agent);
  lifecycle `consolidate`/`forget`/decay/confidence; panel Memory di dashboard.
- Event-driven trigger (cron/webhook) → loop jalan otomatis.
- Hill-climbing loop: analisa run gagal → auto-perbaiki prompt/skill.
- Multi-agent: 1 loop nge-spawn sub-agent khusus (ingat: token ~4x–15x, ukur dulu).
