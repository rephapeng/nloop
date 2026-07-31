# PLAN — nloop: Loop Engine + Dashboard (Claude CLI / subscription)

> A "loop engineering" tool: an autonomous loop that goes `observe → act → verify → recover` until
> the goal is verifiably reached. The engine spawns `claude -p` (auth follows the subscription,
> NOT the paid API). Comes with a real-time monitoring dashboard over SSE.

## Architecture decisions (final)

| Aspect | Choice | Why |
|---|---|---|
| Language | **Python 3.11+**, FastAPI (async) | Lightweight, reuses the `run_claude_code` pattern from refan |
| LLM | **`claude -p` (subprocess)** | Subscription-friendly; unset `CLAUDECODE` + `ANTHROPIC_API_KEY` |
| State + Queue | **SQLite** (WAL mode) | Zero-ops, no Redis/Postgres — saves resources |
| Worker | **asyncio worker pool** + semaphore | Loops run in the background, caps the number of subprocesses |
| Realtime | **SSE** (`stream-json` → event bus) | Watch a loop live without the WebSocket hassle |
| Frontend | **HTML + vanilla JS (EventSource)** | No Node/React build → saves resources |

Guardrails & the resource-frugality principles: **one single source**, see the [Guardrails](#guardrails-required) section below.

---

## Repo structure

```
nloop/
├── engine/
│   ├── config.py         # load config.yaml + defaults
│   ├── claude_cli.py     # subprocess adapter -> claude -p (subscription-safe, stream-json)
│   ├── verifier.py       # deterministic goal verification (exit code of a shell command)
│   ├── loop.py           # loop core: observe->act->verify->recover + guardrails      (Fase 2)
│   ├── store.py          # SQLite: runs, iterations, events (persist + replay)        (Fase 2)
│   ├── events.py         # in-memory pub/sub (asyncio.Queue per run) for SSE          (Fase 4)
│   ├── worker.py         # pick up queued jobs, run the loop, respect the semaphore   (Fase 3)
│   └── memory/
│       ├── hot.py        # Tier 1: curated CLAUDE.md + Tier 2: journal.jsonl (ALWAYS local)
│       ├── base.py       # MemoryProvider ABC + factory                               (Fase 7)
│       ├── local.py      # SQLite FTS5 provider — offline, no server                  (Fase 7)
│       └── selfmem.py    # selfmem HTTP/MCP provider                                  (Fase 7)
├── workspaces/           # per-run workdir; every run gets its own CLAUDE.md + journal.jsonl
├── server/
│   ├── app.py            # FastAPI: REST + SSE endpoint
│   └── static/           # index.html, run.html, app.js                               (Fase 5)
├── scripts/
│   └── smoke.py          # Fase 1 acceptance: verifier + subscription-safe claude -p
├── config.yaml           # MAX_CONCURRENT_LOOPS, model, budget, memory.provider
├── requirements.txt      # fastapi, uvicorn, pyyaml  (keep it SMALL)
└── run.sh                # uvicorn server.app:app (+ worker from Fase 3 onward)
```

---

## Data model (SQLite)

```sql
-- one "loop run"
runs(
  id TEXT PK, goal TEXT, verify_cmd TEXT, workdir TEXT, model TEXT,
  status TEXT,          -- queued|running|succeeded|failed|stopped
  stop_requested INT,
  max_iterations INT, max_cost_usd REAL,
  cost_total REAL, iterations_done INT, session_id TEXT,
  created_at, started_at, ended_at
)
-- one iteration inside a run
iterations(
  id PK, run_id FK, idx INT, prompt TEXT, result_text TEXT,
  cost REAL, turns INT, reason TEXT,           -- success|error_max_turns|timeout
  verifier_passed INT, verifier_output TEXT, started_at, ended_at
)
-- event stream for SSE (replay + live)
events(
  id PK, run_id FK, ts, type TEXT,             -- log|turn|tool|token|verify|status
  payload TEXT                                 -- JSON
)
-- (Fase 7) cross-run HINDSIGHT — minimal version; confidence/decay comes later (see "Later")
lessons(id PK, run_id FK, scope TEXT, text TEXT, kind TEXT, created_at)
CREATE VIRTUAL TABLE lessons_fts USING fts5(text, scope, content='lessons');
```

`jobs` = just use the `status='queued'` column on `runs` (no separate queue table needed). The worker
polls `SELECT ... WHERE status='queued' LIMIT n` → cheap and survives restarts.

---

## API (FastAPI)

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/loops` | Create a new loop (goal, verify_cmd, workdir, guardrails) → `run_id`, status `queued` |
| `GET` | `/api/loops` | List every run + status + cost |
| `GET` | `/api/loops/{id}` | Run detail + all its iterations |
| `POST` | `/api/loops/{id}/stop` | Set the stop flag (the worker checks it between iterations) |
| `GET` | `/api/loops/{id}/events` | **SSE** — replay stored events, then stream live |
| `GET` | `/` , `/run/{id}` | Serve the static dashboard |

**SSE flow:** the worker emits events to `events.py` (one asyncio.Queue per run) **and** persists them to
the `events` table. The SSE endpoint: on connect → send the old events from the DB → subscribe to the queue →
stream new events. If the run is already finished, send the replay and then close.

---

## Loop core (engine/loop.py)

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

The key point: **a deterministic verifier, kept separate** from the agent (the agent never decides it is done).

---

## Subscription adapter (engine/claude_cli.py) — the heart of compatibility

```python
cmd = ["claude", "-p", prompt,
       "--output-format", "stream-json", "--verbose",
       "--permission-mode", "acceptEdits",
       "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep",
       "--max-turns", str(max_turns)]
if resume: cmd += ["--resume", resume]
if model:  cmd += ["--model", model]

env = os.environ.copy()
env.pop("CLAUDECODE", None)          # don't get detected as a nested session
env.pop("ANTHROPIC_API_KEY", None)   # FORCE subscription login, not API billing

# Popen + read stdout line by line (stream-json) -> emit turn/tool/token events live
```

The `stream-json` lines mapped onto SSE events: `system/init` (session_id), `assistant`
(text/turn), `tool_use`/`tool_result`, and the final `result` (cost, num_turns, subtype).

---

## Memory — so the context doesn't fall apart

The problem: `--resume` (session) is volatile — it gets hit by **auto-compaction** once it grows long, so the
context "falls apart". The fix is layered; **v1 core = Tier 0–2** (cheap, always available, plain file ops),
Tier 3 comes later in Fase 7, once the loop has proven itself and been hardened.

### Tier 0 — Session resume (built in)
`--resume <session_id>` per run. Short-term, but DON'T rely on it alone (it can get compacted away).

### Tier 1 — HOT memory: `CLAUDE.md` in the workdir  ⭐ the biggest lever
`claude -p` **auto-loads `CLAUDE.md`** from the workdir on every request — so its contents are immune to
compaction (reloaded every iteration). The loop engine is what **curates** this file:
- Contents: GOAL, invariants/rules, "facts that are now settled", "don't do X again".
- Written tersely and size-*capped* (max ~2 KB) so it doesn't balloon.
- This is the **Ralph Loop / Cherny CLAUDE.md** pattern: state lives in a file, not in the context window.

### Tier 2 — EPISODIC: `workspaces/{run}/journal.jsonl`
Appended every iteration: `{idx, action_summary, verifier_passed, error_head, changed_files}`.
Its jobs: (a) build the "WHAT HAS ALREADY BEEN TRIED" block for the next prompt (anti-repeat),
(b) feed the dashboard timeline, (c) be the raw material distilled into hindsight.

### Tier 3 — HINDSIGHT (Fase 7): pluggable `MemoryProvider`
So a NEW loop learns from OLD ones. Backend-agnostic via `config.yaml`, minimal interface:

```python
class MemoryProvider(ABC):
    def recall(self, project_id, query, k=5) -> list[Memory]: ...
    def save(self, project_id, text, kind, source=None) -> str: ...
    def is_available(self) -> bool: ...
```

v1 backends: **`local`** (SQLite FTS5, the offline-safe default) and **`selfmem`** (hosted,
`https://selfmem.com/mcp`, `X-API-Key` header). Can be set per project: sensitive → `local`.
Factory `get_memory_provider(cfg)` — the `get_llm_provider()` pattern from refan.

Integration: **engine-orchestrated** — the loop is what calls `provider.recall/save`, so it's uniform
across backends and swapping backends doesn't change the prompt.

- **Recall (before the loop):** `provider.recall(project_id, goal)` → top-K → injected into the
  initial `CLAUDE.md` + the first prompt.
- **Save (end of the loop):** distill the journal → ONLY lessons from runs that **passed the verifier**
  get stored (the self-memory idea: verified-only promotion).

Deliberately PUSHED to "Later": the tencent/hindsight-vec providers, the agent-native style
(`mcp__selfmem__auto_*`), and the `consolidate`/`forget`/decay/confidence lifecycle.

### How memory flows inside the loop
```
start run:
  lessons = provider.recall(project_id, goal)    # Tier 3 (Fase 7; before that: skip)
  hot.seed_claudemd(workdir, goal, lessons)      # Tier 1 (ALWAYS local)
each iteration:
  prompt = goal + verifier_output + hot.journal_block(run)    # Tier 2 injected here
  res = claude_cli.run(prompt, resume=session, cwd=workdir)   # Tier 0 + Tier 1 automatic
  hot.append_journal(run, res, verifier)
  hot.append_fact(workdir, ...)                  # verified facts, size-capped
end run (Fase 7):
  for l in distill(journal_entries_that_passed_the_verifier): provider.save(...)
```

### Anti-drift (so the goal doesn't wander)
- The GOAL always sits on the top line of `CLAUDE.md` **and** in every prompt (goal-lock).
- The deterministic verifier is the source of truth for "done", not the agent's claim.
- The "already tried" journal stops the loop from attempting the same thing over and over.

---

## Dashboard (server/static)

- **index.html** — run table: goal, status (badge), which iteration it's on, cost, a Stop button + "New loop".
- **run.html** — detail for one loop:
  - Header: goal, live status, running cost, iteration X/max.
  - Iteration timeline (accordion): prompt, action summary, verifier result (pass/fail + output).
  - A "Live" panel using `new EventSource('/api/loops/{id}/events')` → appends turn/tool/token in real time.
  - A **Stop** button.
- All vanilla JS, no bundler. Styling in one small CSS file.
- A Memory panel (CLAUDE.md contents, journal, recalled lessons) → "Later", after Fase 7.

---

## Implementation phases (each phase has an acceptance test)

**Fase 0 — Scaffold.** Repo, `requirements.txt`, `config.yaml`, `run.sh`, minimal app.
✅ `uvicorn` comes up, `/api/health` returns `{"ok": true}`.

**Fase 1 — Claude adapter + verifier.** Subscription-safe `claude_cli.run()`, parses `stream-json`; `verifier.verify()`.
✅ `scripts/smoke.py`: the verifier returns the right exit code; `claude -p` runs WITHOUT an API key and returns cost & session_id.

**Fase 2 — Loop core + store (SQLite).** `loop.py` + `store.py` + `memory/hot.py` (Tier 1–2).
✅ Run the loop against a test repo with one deliberately failing test → the loop fixes it → `succeeded`, recorded in the DB.

**Fase 3 — Worker + queue + semaphore.** `worker.py` picks up `queued` runs, respects `MAX_CONCURRENT_LOOPS`, checks the stop flag, survives restarts.
✅ Queue up 3 loops, only N run at once; restart the server → the `queued` runs carry on.

**Fase 4 — API + SSE.** REST endpoints + `/events` (replay + live).
✅ `curl -N /api/loops/{id}/events` streams events while the loop is running.

**Fase 5 — Dashboard.** index + run + app.js.
✅ Open a browser, create a loop, watch the iterations & cost update live, be able to Stop it.

**Fase 6 — Hardening.** Budget alerts, no-progress → switch strategy/stop, per-iteration timeout, transient retries, log rotation.
✅ A broken loop (impossible goal) stops cleanly at a guardrail instead of running forever and burning money.
*(Deliberately BEFORE memory: don't let lessons settle out of runs whose behavior is still broken.)*

**Fase 7 — Reactive triggers (Sentry/PostHog webhooks).** *(moved up — it's the main use case:
an issue comes in → a loop fixes the app automatically)* `POST /api/hooks/{source}`: payload → goal
(error title + stacktrace + issue link), workdir/verify_cmd from the per-project mapping in
config, dedup per issue fingerprint (the same issue never spawns a duplicate loop while one is still active).
✅ Send a sample Sentry payload → a new run appears and runs; send it again → no duplicate.

**Fase 8 — Pluggable memory (Tier 3).** `memory/base.py` (ABC + factory), `memory/local.py` (FTS5), `memory/selfmem.py`.
✅ Flip `memory.provider: local` ⇄ `selfmem` in config → the loop behaves the same with no code changes.
✅ Loop A hits a pitfall → it gets stored. Loop B (similar goal) auto-recalls the lesson on its first iteration and doesn't repeat it. `CLAUDE.md` stays small (capped).

**Fase 9 — Port dtc-agent capabilities (DONE 2026-07-15).** Generalize the dtc-agent engine
(github.com/rephapeng/dtc-agent) into nloop; the devtocash-specific payload
(SEO/cross-post) was DELIBERATELY not ported — that's project work, and a loop can call it via Bash.
- Roles + grounding: `engine/grounding.py` — `roles/common.md` + the output of `context_cmd`
  (fresh every iteration) + `roles/<role>.md` → `--append-system-prompt`.
- Subscription hygiene: `claude.lock_file` (cross-process single-flight via flock, the
  `.claude.lock` pattern), also strips `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL`.
- LLM quality gate: `gate_prompt` per run — verifier passes → a separate reviewer
  (read-only, its own session, JSON last-line contract `{"pass":...}`); on reject →
  the loop continues with the reasons as feedback; the gate's cost counts against the budget.
- Scheduler: `engine/scheduler.py` — `schedules:` (at HH:MM UTC / every 6h),
  sequential steps + `always: true` (the daily_pipeline pattern), fingerprint dedup
  `schedule:<name>`, endpoints `GET /api/schedules` + `POST /api/schedules/{n}/trigger`.
- Telegram: `engine/telegram.py` (a task in the lifespan) — notifies when a run finishes, /loops /new
  /stop /status /reset, freeform chat → a per-chat Claude Code session (`--resume` +
  fresh retry), model tiering (smalltalk → cheap), secret redaction, photos/documents →
  `incoming/`, allow-list fails closed. Secrets in `.env` (TELEGRAM_BOT_TOKEN,
  TELEGRAM_ALLOWED_CHAT_IDS).
- CLI `bin/nloop` (new/ls/show/stop/schedules/trigger/ask) + `deploy/self_restart.sh`
  (restart via systemd-run outside the cgroup so the Telegram reply survives).

**Fase 9b — Full issue-fix pipeline (DONE 2026-07-15).** A webhook run isn't just
"verify the build" — it's the whole cycle: repro → fix → validate → release → close the issue.
- Repro-first (`repro: true`, the default for webhooks): the issue run's verify_cmd is
  `sh .nloop/repro/<issue>.sh && (project verify)`. The script doesn't exist yet → the verifier
  fails → the loop is FORCED to investigate, write a repro from the stacktrace, and actually fix it.
  (Without this, a runtime error doesn't turn the build red → the loop no-ops.)
- `on_success_cmd` per project: a fix verified 100% → release steps (git push,
  docker compose up --build, etc.). On failure → the run is FAILED with reason `postrun_failed`.
- `triggers.sentry.resolve: true` + SENTRY_AUTH_TOKEN in .env → run succeeds →
  the issue is marked resolved via the Sentry API (a failed resolve is a warning, not a failure).
- Sentry watchdog (`engine/watchdog.py`, the `watchdog:` section): polls the
  `projects/{org}/{slug}/issues/?query=is:unresolved` API every interval → spawns
  issue-fix runs through the same path as the webhook (`triggers.create_issue_run`).
  Guardrails: active-fingerprint dedup, per-issue cooldown (default 24h), a
  `max_per_tick` cap. Endpoints: `GET /api/watchdog`, `POST /api/watchdog/tick`.

**Fase 10 — Task registry + payload (DONE 2026-07-28).** A new direction: get nloop
as close as possible to trigger.dev's work model. Up to Fase 9b, a "run" was a single-use
thing — a goal + verify_cmd string reassembled at every entry path (REST,
scheduler, webhook, Telegram), with no "task" object you could look at, call
again with different input, or test. Fase 10 adds that unit of work.
- `engine/tasks.py` — the registry: `tasks:` in config.yaml OR `tasks/<id>.yaml`
  (one file per task; files win over config). A broken spec is logged and skipped
  instead of killing the server (the scheduler's `_validate` pattern).
- `{{var}}` / `{{payload.var}}` templates in goal/verify_cmd/workdir/context_cmd/
  gate_prompt/on_success_cmd/idempotency_key. A variable missing from the payload is a
  `TaskError` at trigger time, NOT an empty string — a hole in the goal makes the agent invent things.
- `payload: {required: [...], defaults: {...}}` — validated before the run is created.
- `tasks.trigger(store, cfg, task_id, payload)` = THE single door for every entry path.
  Dedup uses the idempotency key (the existing `runs.fingerprint` column): while a run
  with the same key is still active, a second trigger points at that run instead of creating a new one.
- Per-trigger overrides are limited to `OVERRIDABLE` (model/limit/workdir). A payload must not
  be able to change `role`/`verify_cmd` — that's an escalation path, not a parameter.
- Data model: `runs.task_id` + `runs.payload` (JSON, decoded in `Store`).
- API: `GET /api/tasks`, `GET /api/tasks/{id}`, `POST /api/tasks/{id}/trigger`,
  `POST /api/loops {task, payload}`, filters `GET /api/loops?task=&status=&limit=`.
  CLI: `nloop tasks`, `nloop run <task> k=v`. Schedule step: `task:` + `payload:`.
- Webhook/watchdog: the built-in issue-fix pipeline stays as is (the repro-first goal built by
  `build_goal`), it's just recorded as `task_id='issue-fix'` so it groups in the dashboard;
  a project may point at its own registry task via `triggers.projects.<x>.task`.
✅ `promo-pagi`/`promo-sore`, which used to be two near-identical goal blocks → one `promo-post` task
   called twice with the payload `{slot: pagi|sore}`.
✅ Triggering the same task twice while it's active → the second run isn't created (deduped).
✅ A missing payload field → 400 before the run enters the queue, instead of failing mid-loop.

**Fase 11 — Queue + retry + replay + run tree (NOT STARTED).** More of the
trigger.dev approach, on the execution side:
- Named queues (`queue:` on a task) + a per-queue concurrency cap, replacing the single
  global `asyncio.Semaphore` — right now promo loops and issue-fix loops fight over the same
  slots. Plus simple priorities.
- RUN-level retries + backoff (`retry: {max_attempts, backoff}`) + an `attempt` column.
  What exists today is only in-iteration retries for transient claude errors.
- `POST /api/loops/{id}/replay` — re-run the same task+payload as a new run
  that points at its parent.
- `runs.parent_run_id` — a schedule step becomes a child of a single pipeline run (a subtask),
  instead of a sibling run linked only by fingerprint.

**Fase 12 — trigger.dev-style dashboard (DONE 2026-07-28, pulled ahead of Fase 11).**
Still vanilla JS with no build step; the old `app.js` is split into `common.js` (helpers +
shell) plus one script per page.
- Sidebar shell: Runs / Tasks / Schedules + indicators for active runs & server health.
- **Runs**: a filterable table (status pills, task filter, search by goal/id).
  Filters are stored in the URL query → a view can be shared. Watchdog & schedules
  moved to their own pages so the runs page is only about runs.
- **Tasks**: registry cards + a detail page with the spec, recent runs, and a
  **Test task** form built from `payload.required`/`defaults` — trigger manually
  without needing curl.
- **Run detail**: `engine/trace.py` assembles spans from `iterations` + `events` (no
  new tables, no writes at all), served by `GET /api/loops/{id}/trace`, drawn
  as a waterfall + a span detail panel. The old log is still there, and can now be filtered
  (turn / tool / verify+gate / warning).
- Honest durations: verify/gate/postrun now store their real duration in the event
  payload; act reads it from the `iterations` table. A span whose end can only be ESTIMATED
  (tool — the stream only has a single timestamp) is flagged `approx` and drawn
  hatched. Old runs (from before this phase) have approximate verify spans too.
- Tool spans are capped at `TOOL_CAP` per iteration, with the rest summarized into a
  "+N more tool calls" span — not silently truncated.

---

## Guardrails (required)

The one and only guardrails section — other phases and sections refer back here.

- Hard caps: `max_iterations`, `max_cost_usd`, per-iteration `timeout`, `--max-turns`.
- No-progress detection: identical verifier output twice → change approach / stop.
- Concurrency cap: `MAX_CONCURRENT_LOOPS` (semaphore) — every loop is a tree of claude subprocesses.
- Parse `stream-json` incrementally (don't buffer huge output).
- Human checkpoint (optional): a `require_approval` flag before irreversible actions.
- Every cost is recorded per iteration → the dashboard shows the total + per run.

## Later (optional, upper layers)

- Memory: the `tencent` (TencentDB VectorDB) & `hindsight-vec` (local embeddings) providers;
  the agent-native selfmem style (`mcp__selfmem__auto_recall/auto_save` as agent tools);
  the `consolidate`/`forget`/decay/confidence lifecycle; a Memory panel in the dashboard.
- Event-driven triggers (cron/webhook) → loops start automatically.
- Hill-climbing loop: analyze failed runs → auto-improve the prompt/skill.
- Multi-agent: one loop spawns specialized sub-agents (remember: ~4x–15x the tokens, measure first).
