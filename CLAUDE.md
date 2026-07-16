# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

nloop is an autonomous loop engine ("loop engineering"): it runs `observe → act → verify → recover` cycles until a goal is verifiably achieved. The **act** step spawns `claude -p` as a subprocess (subscription auth, NOT API billing); the **verify** step is a deterministic shell command. A FastAPI server exposes REST + SSE and serves a vanilla-JS dashboard. `PLAN.md` is the authoritative design doc — phases, data model, and guardrails all live there; keep it in sync when architecture changes.

Code comments and docs are written in casual Indonesian — match that style.

## Commands

```bash
source .venv/bin/activate            # venv already exists at .venv/

pytest                               # run all tests (fast, no network/claude needed)
pytest tests/test_loop.py            # one file
pytest tests/test_loop.py -k name    # one test

./run.sh                             # start server + worker (host/port from config.yaml, override with HOST/PORT env)
bin/nloop new "goal" "verify_cmd"    # CLI over the REST API (also: ls/show/stop/schedules/trigger)
bin/nloop ask "question" [--role X]  # one-shot read-only claude Q&A (no server needed, costs a request)
python scripts/smoke.py              # verifier acceptance (free)
python scripts/smoke.py --with-claude   # + one real `claude -p` call (costs a subscription request)
python scripts/e2e_loop.py           # full loop e2e with real claude: buggy calc.py → loop fixes it
```

Deployed as systemd unit `deploy/nloop.service` (WorkingDirectory=/opt/nloop, runs `run.sh`). After changing server code on this box: `systemctl restart nloop`.

## Architecture

The whole system is **one process**: uvicorn runs `server/app.py`, whose FastAPI lifespan starts a `Worker` task. No Redis, no separate worker daemon.

**Flow of a run:** `POST /api/loops` (or a webhook) inserts a row in `runs` with `status='queued'` → `engine/worker.py` polls SQLite, claims queued runs under an `asyncio.Semaphore` (`loops.max_concurrent` — each loop is a whole tree of claude subprocesses) → `engine/loop.py` drives the iterations → each iteration calls `engine/verifier.py` (shell cmd, exit 0 = goal met) then, if failing, `engine/claude_cli.py` (spawns `claude -p`, parses `stream-json` line-by-line) → events are both persisted to the `events` table (via `Store`) and pushed to `engine/events.py` in-memory bus → the SSE endpoint replays persisted events from DB, then streams live from the bus, deduping by event id.

**Key modules and the invariants they own:**

- `engine/claude_cli.py` — the subscription-compat adapter. It **must** strip `CLAUDECODE`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` from the env (avoid nested-session detection; force subscription auth, never API billing). Don't break this. Optional `claude.lock_file` wraps every invocation in `flock` — cross-process single-flight so multiple agents sharing one subscription on the box never run claude concurrently (dtc-agent's `.claude.lock` pattern). Also supports `--append-system-prompt`, `--session-id`, and `MAX_THINKING_TOKENS`. `last_json()` implements the "JSON last line" output contract used by the gate.
- `engine/loop.py` — loop core + guardrails: max_iterations, budget cap (+80% warning), no-progress detection (identical verifier output N× → stop), consecutive-claude-error cap, transient retry, stop-flag check between iterations. The verifier is deliberately separate from the agent — the agent never judges its own completion; the prompt tells it so. Two per-run extensions (ported from dtc-agent): a **role/grounding system prompt** built each iteration by `engine/grounding.py` (`roles/common.md` + fresh `context_cmd` stdout + `roles/<role>.md`), and an **LLM quality gate** (`gate_prompt`): after the verifier passes, an independent read-only claude session reviews the work against the criteria and must emit `{"pass": ...}` as its last JSON line; a reject feeds its reasons back into the next iteration (identical rejections trip the no-progress guardrail; gate cost counts against the budget; unparseable gate output is a reject — fail closed).
- `engine/scheduler.py` — recurring runs from `schedules:` in config (`at: "HH:MM"` UTC daily or `every: 30m/6h/1d`), each tick running its `steps` **sequentially**: a step only runs if the previous one succeeded, unless marked `always: true`. Dedup via run fingerprint `schedule:<name>` (a tick is skipped while the previous pipeline is still active). Manual fire: `POST /api/schedules/{name}/trigger`.
- `engine/watchdog.py` — Sentry watchdog task (`watchdog:` config): polls `api/0/projects/{org}/{slug}/issues/?query=is:unresolved` and spawns issue-fix runs through the same `triggers.create_issue_run()` path as the webhook, so behavior is identical. Each project in `watchdog.projects` runs its **own independent poll loop** (same pattern as `scheduler.py`'s per-schedule tasks), so intervals can differ per app: a short form `slug: name` uses the global `watchdog.interval`, or a dict `slug: {name, interval, max_per_tick}` overrides either per project. Guardrails: active-fingerprint dedup, per-issue cooldown (default 24h — a just-failed issue isn't retried every tick), `max_per_tick` cap (global or per-project override). `POST /api/watchdog/tick` forces one manual round across all projects immediately (ignores each project's own interval, keeps the global `max_per_tick` cap across the combined round).
- `engine/telegram.py` — optional Telegram bot task (enable `telegram.enabled` + `TELEGRAM_BOT_TOKEN` in `.env`): notifies terminal run statuses, control commands (`/loops /new /stop /status /reset`), and freeform messages become a real per-chat Claude Code session (`--resume` with a stale-session fresh retry, cheap-model tiering for smalltalk, secret redaction on all outgoing text, photos/documents downloaded to `incoming/` for the agent to Read). Allow-list (`TELEGRAM_ALLOWED_CHAT_IDS`) fails closed. Secrets live in `.env` (gitignored) via `config.load_env()` — never in config.yaml.
- `engine/store.py` — SQLite (WAL) is state **and** queue: queued jobs are just `runs.status='queued'` rows, so the queue survives restarts. On boot the worker requeues any `running` rows (orphans from a crashed process — safe only because the system is single-process). Sync sqlite3 is called from the event loop on purpose (writes are small/rare); all DB access is concentrated in `Store`.
- `engine/memory/hot.py` — memory tiers 1–2: it curates a `CLAUDE.md` inside each run's workdir (goal-locked, capped at ~2KB — `claude -p` auto-loads it every request, so it survives context compaction) and appends `journal.jsonl` per iteration (injected into the next prompt as "what was already tried", anti-repeat). `loop.py` never overwrites an existing workdir CLAUDE.md.
- `engine/triggers.py` + the `/api/hooks/{source}` endpoint — Sentry/PostHog/generic webhook → a full **issue-fix pipeline**, not just a goal string. Dedup by issue fingerprint (same issue never spawns a second loop while one is queued/running). By default (`repro: true`) the run is **repro-first**: its verify_cmd becomes `sh .nloop/repro/<issue>.sh && (project verify_cmd)` — the repro script doesn't exist yet, so the verifier fails and forces the agent to investigate the stacktrace, write a failing repro, then fix the root cause; "done" means repro AND project health check both pass. Without this, runtime errors (most Sentry issues) don't break the build and the loop would no-op. After success: the project's `on_success_cmd` runs (push/deploy; failure → run failed, reason `postrun_failed`), then `engine/sentry.py` marks the issue resolved via the Sentry API when `triggers.sentry.resolve` is on (`SENTRY_AUTH_TOKEN` in `.env`; resolve failure is a warning, never a run failure).
- `engine/config.py` — `DEFAULTS` overlaid by `config.yaml`. Every config value must have a default here; nothing reads `config.yaml` directly.

**Not yet built (Fase 8 in PLAN.md):** pluggable `MemoryProvider` (`memory/base.py`, `local.py` FTS5, `selfmem.py`) for cross-run lessons. `config.yaml` already has `memory.provider` and the `lessons` schema is specced in PLAN.md.

**Fase 9 (done):** the generalized port of dtc-agent's engine capabilities (roles/grounding, claude lock, LLM gate, scheduler pipelines, Telegram bot, `bin/nloop` CLI, `deploy/self_restart.sh`). devtocash-specific payload (SEO scripts, cross-posting) was deliberately NOT ported — that's project workload a loop can call via Bash. The dashboard does not yet expose the new run fields (role/context_cmd/gate_prompt) or schedules.

## Notes

- Tests are async-heavy and use fakes for the claude CLI — nothing in `tests/` shells out to real `claude`; keep it that way (real-claude checks belong in `scripts/`).
- `nloop.db` and `workspaces/*` are runtime artifacts (gitignored); the live server on this box uses them, so don't delete casually.
- The dashboard has **no auth**; server binds 0.0.0.0 for tailscale access only. `triggers.token` must be set if the webhook endpoint is ever exposed.
- Resource frugality is a design principle throughout (SQLite over Redis, vanilla JS over React, incremental stream-json parsing, capped outputs/journals) — keep new code in that spirit.
