"""Trace: runs + iterations + events → pohon span buat waterfall dashboard (Fase 12).

Dashboard lama nampilin event sebagai log append-only — kelihatan APA yang terjadi,
tapi nggak kelihatan BERAPA LAMA tiap fase dan mana yang jadi bottleneck. Modul ini
nyusun ulang data yang udah ada jadi span berdurasi, ala trace trigger.dev:

    run
    └── iterasi 1
        ├── verify        (durasi asli dari payload event; run lama: ditaksir)
        ├── act           (durasi asli dari tabel iterations)
        │   ├── tool Bash (ditaksir: sampai event berikutnya — stream cuma punya 1 ts)
        │   └── tool Edit
        └── gate

Prinsip: span yang durasinya DITAKSIR ditandai `approx: true` — frontend nge-render
beda (bar samar) biar nggak keliatan lebih presisi dari kenyataannya.

Pembagian event ke iterasi pakai jendela waktu dari tabel `iterations`
(started_at/ended_at akurat), BUKAN hitung event `result` — satu iterasi bisa
punya beberapa result kalau retry transient kejadian.
"""
from __future__ import annotations

import time

# event yang jadi span sendiri (sisanya: turn/init/log → marker atau diabaikan)
_ACT_STATUS = {"success": "ok"}
TOOL_CAP = 80   # tool span per iterasi; sisanya diringkas jadi satu span "+N lagi"


def _span(span_id: str, parent: str | None, kind: str, name: str,
          start: float, end: float, status: str = "", *, approx: bool = False,
          detail: dict | None = None) -> dict:
    return {
        "id": span_id, "parent_id": parent, "kind": kind, "name": name,
        "start": start, "end": max(end, start), "duration": max(0.0, end - start),
        "status": status, "approx": approx, "detail": detail or {},
    }


def _run_status(run: dict) -> str:
    return {"succeeded": "ok", "failed": "fail", "stopped": "warn",
            "running": "running", "queued": "queued"}.get(run["status"], "")


def build(run: dict, iterations: list[dict], events: list[dict],
          now: float | None = None) -> dict:
    """Return {start, end, spans[]} — spans urut waktu, parent duluan."""
    now = now or time.time()
    t0 = run.get("started_at") or run.get("created_at") or now
    t1 = run.get("ended_at") or now
    root_name = run.get("task_id") or (run.get("goal") or "run").splitlines()[0][:60]
    spans = [_span("run", None, "run", root_name, t0, t1, _run_status(run),
                   detail={"cost": run.get("cost_total"),
                           "iterations": run.get("iterations_done")})]

    iterations = sorted(iterations, key=lambda it: it["idx"])
    windows, prev_end = [], t0
    for it in iterations:
        end = it.get("ended_at") or now
        windows.append((it, prev_end, end))
        prev_end = end

    buckets: list[list[dict]] = [[] for _ in windows]
    trailing: list[dict] = []
    for ev in sorted(events, key=lambda e: e["id"]):
        for i, (_it, _s, end) in enumerate(windows):
            if ev["ts"] <= end:
                buckets[i].append(ev)
                break
        else:
            trailing.append(ev)

    for (it, win_start, _end), evs in zip(windows, buckets):
        spans += _iteration_spans(it["idx"], it, evs, win_start, now)

    if trailing:
        if run["status"] in ("queued", "running"):
            # iterasi yang lagi jalan belum masuk tabel iterations (baris ditulis
            # setelah ACT kelar) — tetap digambar biar live view nggak bolong.
            spans += _iteration_spans(len(windows) + 1, None, trailing, prev_end, now)
        else:
            spans += _final_spans(trailing, prev_end, now)
    return {"start": t0, "end": max(t1, spans[-1]["end"] if spans else t1),
            "spans": spans}


def _iteration_spans(idx: int, it: dict | None, evs: list[dict],
                     win_start: float, now: float) -> list[dict]:
    parent = f"it{idx}"
    children: list[dict] = []
    prev_ts = win_start

    act_start = (it or {}).get("started_at")
    act_end = (it or {}).get("ended_at")
    tool_evs: list[dict] = []

    for ev in evs:
        typ, ts, p = ev["type"], ev["ts"], ev.get("payload") or {}
        if typ == "verify":
            children.append(_span(
                f"{parent}.verify", parent, "verify",
                "verify" + ("" if p.get("passed") else " (fail)"),
                ts - _dur(p, ts, prev_ts), ts,
                "ok" if p.get("passed") else "fail",
                approx="duration" not in p,
                detail={"exit_code": p.get("exit_code"), "output": p.get("output")}))
        elif typ == "gate":
            children.append(_span(
                f"{parent}.gate", parent, "gate", "quality gate",
                ts - _dur(p, ts, prev_ts), ts,
                "ok" if p.get("passed") else "fail",
                approx="duration" not in p,
                detail={"reasons": p.get("reasons"), "cost": p.get("cost")}))
        elif typ == "tool":
            tool_evs.append(ev)
        elif typ == "init" and act_start is None:
            act_start = ts        # iterasi berjalan: belum ada baris di tabel
        elif typ == "result" and act_end is None:
            act_end = ts
        prev_ts = ts

    if act_start is not None:
        act_end = act_end or now
        reason = (it or {}).get("reason") or ""
        children.append(_span(
            f"{parent}.act", parent, "act", "act (claude)", act_start, act_end,
            _ACT_STATUS.get(reason, "running" if it is None else "warn"),
            detail={"cost": (it or {}).get("cost"), "turns": (it or {}).get("turns"),
                    "reason": reason,
                    "result_text": (it or {}).get("result_text")}))
        children += _tool_spans(parent, tool_evs, act_end, now)

    if not children:
        return []
    start = min(c["start"] for c in children)
    end = max(c["end"] for c in children)
    # Warna iterasi ngikut hasil ACT-nya, BUKAN hasil verify: verify gagal di awal
    # iterasi itu kondisi normal (justru itu alasan iterasi jalan). Gate nolak →
    # warn: act-nya sukses tapi hasilnya ditolak reviewer.
    if it is None:
        status = "running"
    else:
        status = "ok" if (it.get("reason") or "") == "success" else "fail"
        if any(c["kind"] == "gate" and c["status"] == "fail" for c in children):
            status = "warn"
    head = _span(parent, "run", "iteration", f"iterasi {idx}", start, end, status,
                 detail={"verifier_passed": (it or {}).get("verifier_passed")})
    return [head] + children


def _tool_spans(parent: str, tool_evs: list[dict], act_end: float,
                now: float) -> list[dict]:
    """Event tool cuma punya SATU timestamp (waktu tool_use muncul di stream).
    Ujungnya ditaksir = tool berikutnya / akhir act → semua ditandai approx.

    Iterasi panjang bisa punya ratusan tool call; digambar semua = waterfall
    nggak kebaca. Dipotong di TOOL_CAP, sisanya DIBILANG (jangan diem-diem —
    trace yang motong tanpa bilang kelihatan kayak trace yang lengkap).
    """
    out = []
    shown = tool_evs[:TOOL_CAP]
    for i, ev in enumerate(shown):
        end = tool_evs[i + 1]["ts"] if i + 1 < len(tool_evs) else act_end
        p = ev.get("payload") or {}
        out.append(_span(
            f"{parent}.tool{i}", f"{parent}.act", "tool", p.get("name") or "tool",
            ev["ts"], max(end, ev["ts"]), "", approx=True,
            detail={"input": p.get("input")}))
    if len(tool_evs) > TOOL_CAP:
        rest = tool_evs[TOOL_CAP:]
        out.append(_span(
            f"{parent}.tool-rest", f"{parent}.act", "tool",
            f"+{len(rest)} tool call lagi (nggak digambar)",
            rest[0]["ts"], act_end, "", approx=True,
            detail={"input": f"{len(rest)} tool call disembunyiin biar waterfall "
                             f"kebaca — detail lengkapnya ada di panel Log."}))
    return out


def _final_spans(evs: list[dict], prev_end: float, now: float) -> list[dict]:
    """Event setelah iterasi terakhir: verify final, gate final, postrun (rilis)."""
    out, prev_ts = [], prev_end
    for i, ev in enumerate(evs):
        typ, ts, p = ev["type"], ev["ts"], ev.get("payload") or {}
        if typ == "verify":
            out.append(_span(f"final{i}", "run", "verify", "verify final",
                             ts - _dur(p, ts, prev_ts), ts,
                             "ok" if p.get("passed") else "fail",
                             approx="duration" not in p,
                             detail={"output": p.get("output")}))
        elif typ == "gate":
            out.append(_span(f"final{i}", "run", "gate", "quality gate",
                             ts - _dur(p, ts, prev_ts), ts,
                             "ok" if p.get("passed") else "fail",
                             approx="duration" not in p,
                             detail={"reasons": p.get("reasons")}))
        elif typ == "postrun":
            out.append(_span(f"final{i}", "run", "postrun", "rilis (on_success_cmd)",
                             ts - _dur(p, ts, prev_ts), ts,
                             "ok" if p.get("ok") else "fail",
                             approx="duration" not in p,
                             detail={"cmd": p.get("cmd"), "output": p.get("output")}))
        prev_ts = ts
    return out


def _dur(payload: dict, ts: float, prev_ts: float) -> float:
    """Durasi asli kalau dicatat; run lama → taksir dari jarak ke event sebelumnya."""
    d = payload.get("duration")
    if isinstance(d, (int, float)) and d >= 0:
        return float(d)
    return max(0.0, min(ts - prev_ts, 300.0))
