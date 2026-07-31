"""SQLite (WAL): runs, iterations, events — state and queue in one.

The queue IS the runs.status='queued' column (no separate jobs table, survives
restarts). Sync sqlite3 is called straight from the event loop: writes are small and
rare (per iteration/event) and WAL keeps readers unblocked. If it ever starts to hurt,
move it to a thread executor — all DB access is already concentrated in this class.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  verify_cmd TEXT NOT NULL,
  workdir TEXT NOT NULL,
  model TEXT,
  task_id TEXT,                            -- Fase 10: which task this run instantiates
  payload TEXT,                            -- JSON: trigger input (templates goal/verify)
  status TEXT NOT NULL DEFAULT 'queued',   -- queued|running|succeeded|failed|stopped
  stop_requested INTEGER NOT NULL DEFAULT 0,
  max_iterations INTEGER NOT NULL DEFAULT 10,
  max_cost_usd REAL NOT NULL DEFAULT 5.0,
  cost_total REAL NOT NULL DEFAULT 0,
  iterations_done INTEGER NOT NULL DEFAULT 0,
  session_id TEXT,
  created_at REAL NOT NULL,
  started_at REAL,
  ended_at REAL
);
CREATE TABLE IF NOT EXISTS iterations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  idx INTEGER NOT NULL,
  prompt TEXT,
  result_text TEXT,
  cost REAL,
  turns INTEGER,
  reason TEXT,                             -- subtype from claude: success|error_max_turns|timeout
  verifier_passed INTEGER,
  verifier_output TEXT,
  started_at REAL,
  ended_at REAL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  ts REAL NOT NULL,
  type TEXT NOT NULL,                      -- init|turn|tool|verify|result|status
  payload TEXT NOT NULL                    -- JSON
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
"""


def _run_row(row) -> dict | None:
    """A runs row → dict with the JSON payload decoded (callers needn't know the storage)."""
    if row is None:
        return None
    d = dict(row)
    if d.get("payload"):
        try:
            d["payload"] = json.loads(d["payload"])
        except (TypeError, ValueError):
            d["payload"] = None
    return d


class Store:
    def __init__(self, path: str = "nloop.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Lightweight migration for older DBs (SQLite: ADD COLUMN is cheap)."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}
        if "fingerprint" not in cols:  # Fase 7: dedup trigger webhook
            self.db.execute("ALTER TABLE runs ADD COLUMN fingerprint TEXT")
        # Fase 9 (port dtc-agent): role prompt, grounding cmd, LLM gate.
        # on_success_cmd: langkah rilis setelah fix terverifikasi (push+deploy).
        # Fase 10: task_id + payload (a run instantiates a task, it isn't a one-off).
        # Fase 13: workspace — tenant pemilik run. Run lama NULL sampai diadopsi
        # workspace primary (lihat adopt_orphan_runs).
        for col in ("role", "context_cmd", "gate_prompt", "on_success_cmd",
                    "task_id", "payload", "workspace"):
            if col not in cols:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
        # the index follows the ALTER — an older DB lacks the column when SCHEMA runs
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, created_at)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_ws ON runs(workspace, created_at)")

    def adopt_orphan_runs(self, workspace: str) -> int:
        """Pre-workspace runs (NULL column) are adopted by the primary workspace, so
        the old history doesn't vanish from the dashboard once workspaces are on."""
        cur = self.db.execute(
            "UPDATE runs SET workspace=? WHERE workspace IS NULL", (workspace,))
        self.db.commit()
        return cur.rowcount

    # ---- runs ----

    def create_run(
        self,
        goal: str,
        verify_cmd: str,
        workdir: str,
        *,
        model: str | None = None,
        max_iterations: int = 10,
        max_cost_usd: float = 5.0,
        fingerprint: str | None = None,
        role: str | None = None,
        context_cmd: str | None = None,
        gate_prompt: str | None = None,
        on_success_cmd: str | None = None,
        task_id: str | None = None,
        payload: dict | None = None,
        workspace: str | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO runs(id, goal, verify_cmd, workdir, model,"
            " max_iterations, max_cost_usd, fingerprint, role, context_cmd,"
            " gate_prompt, on_success_cmd, task_id, payload, workspace, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, goal, verify_cmd, workdir, model,
             max_iterations, max_cost_usd, fingerprint, role, context_cmd,
             gate_prompt, on_success_cmd, task_id,
             json.dumps(payload, ensure_ascii=False) if payload else None,
             workspace, time.time()),
        )
        self.db.commit()
        return run_id

    @staticmethod
    def _ws_clause(workspace: str | None) -> tuple[str, tuple]:
        """The workspace filter for fingerprint lookups. None = across workspaces
        (the legacy path and tests); set = dedup stays inside that tenant, so two
        workspaces may hold a schedule/issue with the same fingerprint."""
        return (" AND workspace=?", (workspace,)) if workspace else ("", ())

    def find_active_by_fingerprint(self, fingerprint: str,
                                   workspace: str | None = None) -> str | None:
        """Trigger dedup: an active run (queued/running) with the same fingerprint."""
        clause, params = self._ws_clause(workspace)
        row = self.db.execute(
            "SELECT id FROM runs WHERE fingerprint=? AND status IN ('queued','running')"
            f"{clause} LIMIT 1",
            (fingerprint, *params),
        ).fetchone()
        return row["id"] if row else None

    def last_run_for_fingerprint(self, fingerprint: str,
                                 workspace: str | None = None) -> dict | None:
        """The latest run (any status) for a fingerprint — used by the watchdog cooldown."""
        clause, params = self._ws_clause(workspace)
        row = self.db.execute(
            f"SELECT * FROM runs WHERE fingerprint=?{clause}"
            " ORDER BY created_at DESC LIMIT 1",
            (fingerprint, *params),
        ).fetchone()
        return _run_row(row)

    def last_runs_for_fingerprint(self, fingerprint: str, limit: int,
                                  workspace: str | None = None) -> list[dict]:
        """The latest `limit` runs (any status) for a fingerprint, in CHRONOLOGICAL
        order (oldest -> newest) — the dashboard draws the last schedule tick's steps
        as a flow from this. Best-effort: a skipped step (the previous one failed and
        it isn't `always: true`) never became a run row at all, so the chip count can
        be lower than the step count in config — that is accurate, not a bug."""
        clause, params = self._ws_clause(workspace)
        rows = self.db.execute(
            f"SELECT * FROM runs WHERE fingerprint=?{clause}"
            " ORDER BY created_at DESC LIMIT ?",
            (fingerprint, *params, limit),
        ).fetchall()
        return [_run_row(r) for r in reversed(rows)]

    def get_run(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _run_row(row)

    def list_runs(self, *, task_id: str | None = None, status: str | None = None,
                  limit: int | None = None, workspace: str | None = None) -> list[dict]:
        sql = "SELECT * FROM runs"
        where, params = [], []
        if task_id:
            where.append("task_id=?")
            params.append(task_id)
        if status:
            where.append("status=?")
            params.append(status)
        if workspace:
            where.append("workspace=?")
            params.append(workspace)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [_run_row(r) for r in self.db.execute(sql, params).fetchall()]

    def task_ids(self, workspace: str | None = None) -> list[dict]:
        """task_ids that have ever run + their run counts. The dashboard uses this to
        show built-in tasks (e.g. issue-fix) that aren't registered in the config registry."""
        clause, params = self._ws_clause(workspace)
        rows = self.db.execute(
            "SELECT task_id, COUNT(*) AS runs, MAX(created_at) AS last_at FROM runs"
            f" WHERE task_id IS NOT NULL{clause}"
            " GROUP BY task_id ORDER BY last_at DESC", params
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_started(self, run_id: str) -> None:
        self.db.execute(
            "UPDATE runs SET status='running', started_at=? WHERE id=?",
            (time.time(), run_id),
        )
        self.db.commit()

    def finish(self, run_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE runs SET status=?, ended_at=? WHERE id=?",
            (status, time.time(), run_id),
        )
        self.db.commit()

    def bump(self, run_id: str, *, cost_total: float, iterations_done: int,
             session_id: str | None) -> None:
        """Update running progress — called after each ACT iteration."""
        self.db.execute(
            "UPDATE runs SET cost_total=?, iterations_done=?, session_id=? WHERE id=?",
            (cost_total, iterations_done, session_id, run_id),
        )
        self.db.commit()

    def update_cost(self, run_id: str, cost_total: float) -> None:
        """Update cost di luar siklus iterasi (mis. biaya LLM gate)."""
        self.db.execute("UPDATE runs SET cost_total=? WHERE id=?",
                        (cost_total, run_id))
        self.db.commit()

    def claim_queued(self) -> str | None:
        """Atomically claim the oldest 'queued' run → status 'running'.

        Called by the worker; UPDATE..RETURNING keeps it safe even if there is ever
        more than one claimer.
        """
        cur = self.db.execute(
            "UPDATE runs SET status='running' WHERE id="
            "(SELECT id FROM runs WHERE status='queued' ORDER BY created_at LIMIT 1)"
            " RETURNING id"
        )
        row = cur.fetchone()
        self.db.commit()
        return row["id"] if row else None

    def requeue_running(self) -> int:
        """Saat boot: run 'running' pasti orphan proses lama (crash/restart) → requeue."""
        cur = self.db.execute("UPDATE runs SET status='queued' WHERE status='running'")
        self.db.commit()
        return cur.rowcount

    def request_stop(self, run_id: str) -> None:
        self.db.execute("UPDATE runs SET stop_requested=1 WHERE id=?", (run_id,))
        self.db.commit()

    def stop_requested(self, run_id: str) -> bool:
        row = self.db.execute(
            "SELECT stop_requested FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        return bool(row and row["stop_requested"])

    # ---- iterations ----

    def add_iteration(
        self, run_id: str, *, idx: int, prompt: str, result_text: str,
        cost: float, turns: int, reason: str, verifier_passed: bool,
        verifier_output: str, started_at: float, ended_at: float,
    ) -> None:
        self.db.execute(
            "INSERT INTO iterations(run_id, idx, prompt, result_text, cost, turns,"
            " reason, verifier_passed, verifier_output, started_at, ended_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, idx, prompt, result_text, cost, turns, reason,
             int(verifier_passed), verifier_output, started_at, ended_at),
        )
        self.db.commit()

    def iterations(self, run_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM iterations WHERE run_id=? ORDER BY idx", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- events ----

    def add_event(self, run_id: str, type_: str, payload: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
            (run_id, time.time(), type_, json.dumps(payload, ensure_ascii=False)),
        )
        self.db.commit()
        return cur.lastrowid or 0

    def events_since(self, run_id: str, after_id: int = 0) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id",
            (run_id, after_id),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out
