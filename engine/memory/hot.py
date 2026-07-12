"""Tier 1 (HOT): kurasi CLAUDE.md di workdir + Tier 2 (EPISODIC): journal.jsonl.

SELALU lokal (file ops doang) — BUKAN bagian MemoryProvider pluggable (itu Fase 7).
`claude -p` otomatis muat CLAUDE.md dari workdir tiap request → kebal compaction.
Dipakai penuh mulai Fase 2 (loop core).
"""
from __future__ import annotations

import json
import os

CLAUDEMD_CAP = 2048  # bytes — jaga tetap kecil (pola Ralph Loop / Cherny CLAUDE.md)
FACTS_HEADER = "## Fakta terverifikasi"


def seed_claudemd(workdir: str, goal: str, lessons: list[str] | None = None) -> None:
    """Tulis CLAUDE.md awal: GOAL di baris atas (goal-lock) + lessons hasil recall."""
    lines = [
        f"# GOAL: {goal}",
        "",
        "Aturan: kerjakan HANYA untuk mencapai GOAL di atas. "
        "Selesai/tidaknya ditentukan verifier eksternal, bukan penilaianmu sendiri.",
        "",
    ]
    if lessons:
        lines += ["## Pelajaran dari run sebelumnya"] + [f"- {l}" for l in lessons] + [""]
    lines += [FACTS_HEADER, ""]
    _write_capped(os.path.join(workdir, "CLAUDE.md"), "\n".join(lines))


def append_fact(workdir: str, fact: str) -> None:
    """Tambah satu fakta terverifikasi ke CLAUDE.md, jaga tetap di bawah cap."""
    path = os.path.join(workdir, "CLAUDE.md")
    text = _read(path)
    if FACTS_HEADER not in text:
        text = text.rstrip() + f"\n\n{FACTS_HEADER}\n"
    text = text.rstrip() + f"\n- {fact}\n"
    _write_capped(path, text)


def append_journal(workdir: str, entry: dict) -> None:
    """Tier 2: append satu entry iterasi ke journal.jsonl."""
    with open(os.path.join(workdir, "journal.jsonl"), "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def recent_journal(workdir: str, n: int = 5) -> list[dict]:
    path = os.path.join(workdir, "journal.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        lines = f.readlines()
    return [json.loads(line) for line in lines[-n:] if line.strip()]


def journal_block(workdir: str, n: int = 5) -> str:
    """Blok 'APA YANG UDAH DICOBA' buat disuntik ke prompt (anti ngulang)."""
    entries = recent_journal(workdir, n)
    if not entries:
        return ""
    lines = ["APA YANG UDAH DICOBA (jangan diulang):"]
    for e in entries:
        status = "PASS" if e.get("verifier_passed") else "FAIL"
        lines.append(f"- iter {e.get('idx')}: {e.get('action_summary', '?')} → {status}")
    return "\n".join(lines)


def _read(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def _write_capped(path: str, text: str, cap: int = CLAUDEMD_CAP) -> None:
    """Tulis file; kalau lewat cap, buang fakta paling lama (baris bullet paling atas)."""
    while len(text.encode()) > cap:
        lines = text.splitlines()
        # cari bullet pertama SETELAH header fakta — itu yang paling lama
        try:
            start = lines.index(FACTS_HEADER)
        except ValueError:
            text = text.encode()[:cap].decode("utf-8", "ignore")
            break
        bullets = [i for i in range(start + 1, len(lines)) if lines[i].startswith("- ")]
        if not bullets:
            text = text.encode()[:cap].decode("utf-8", "ignore")
            break
        del lines[bullets[0]]
        text = "\n".join(lines)
    with open(path, "w") as f:
        f.write(text if text.endswith("\n") else text + "\n")
