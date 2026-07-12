"""SQLite (WAL): runs, iterations, events — state + queue sekaligus.

Queue = kolom runs.status='queued' (nggak ada tabel jobs terpisah, tahan restart).
sqlite3 sync dipakai langsung dari event loop: write kecil & jarang (per iterasi/
event), WAL bikin reader nggak ke-block. Kalau kerasa berat, pindah ke thread
executor — semua akses DB udah ngumpul di class ini.
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
  reason TEXT,                             -- subtype dari claude: success|error_max_turns|timeout
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


class Store:
    def __init__(self, path: str = "nloop.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Migrasi ringan buat DB lama (SQLite: ADD COLUMN murah)."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}
        if "fingerprint" not in cols:  # Fase 7: dedup trigger webhook
            self.db.execute("ALTER TABLE runs ADD COLUMN fingerprint TEXT")

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
    ) -> str:
        run_id = uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO runs(id, goal, verify_cmd, workdir, model,"
            " max_iterations, max_cost_usd, fingerprint, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, goal, verify_cmd, workdir, model,
             max_iterations, max_cost_usd, fingerprint, time.time()),
        )
        self.db.commit()
        return run_id

    def find_active_by_fingerprint(self, fingerprint: str) -> str | None:
        """Dedup trigger: run aktif (queued/running) dengan fingerprint sama."""
        row = self.db.execute(
            "SELECT id FROM runs WHERE fingerprint=? AND status IN ('queued','running')"
            " LIMIT 1",
            (fingerprint,),
        ).fetchone()
        return row["id"] if row else None

    def get_run(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
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
        """Update progress berjalan — dipanggil tiap habis satu iterasi ACT."""
        self.db.execute(
            "UPDATE runs SET cost_total=?, iterations_done=?, session_id=? WHERE id=?",
            (cost_total, iterations_done, session_id, run_id),
        )
        self.db.commit()

    def claim_queued(self) -> str | None:
        """Ambil satu run 'queued' tertua secara atomic → status 'running'.

        Dipanggil worker; UPDATE..RETURNING bikin aman walau nanti ada
        lebih dari satu claimer.
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
