"""Tier 1 (HOT): curate CLAUDE.md in the workdir + Tier 2 (EPISODIC): journal.jsonl.

ALWAYS local (plain file ops) — NOT part of the pluggable MemoryProvider (that's Fase 7).
`claude -p` auto-loads CLAUDE.md from the workdir on every request → survives compaction.
In full use since Fase 2 (the loop core).
"""
from __future__ import annotations

import json
import os

CLAUDEMD_CAP = 2048  # bytes — keep it small (Ralph Loop / Cherny CLAUDE.md pattern)
FACTS_HEADER = "## Verified facts"


def seed_claudemd(workdir: str, goal: str, lessons: list[str] | None = None) -> None:
    """Write the initial CLAUDE.md: GOAL on the first line (goal-lock) + recalled lessons."""
    lines = [
        f"# GOAL: {goal}",
        "",
        "Rule: work ONLY towards the GOAL above. "
        "Whether it is done is decided by an external verifier, not by your own judgement.",
        "",
    ]
    if lessons:
        lines += ["## Lessons from previous runs"] + [f"- {l}" for l in lessons] + [""]
    lines += [FACTS_HEADER, ""]
    _write_capped(os.path.join(workdir, "CLAUDE.md"), "\n".join(lines))


def append_fact(workdir: str, fact: str) -> None:
    """Append one verified fact to CLAUDE.md, keeping it under the cap."""
    path = os.path.join(workdir, "CLAUDE.md")
    text = _read(path)
    if FACTS_HEADER not in text:
        text = text.rstrip() + f"\n\n{FACTS_HEADER}\n"
    text = text.rstrip() + f"\n- {fact}\n"
    _write_capped(path, text)


def append_journal(workdir: str, entry: dict) -> None:
    """Tier 2: append one iteration entry to journal.jsonl."""
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
    """The 'WHAT HAS ALREADY BEEN TRIED' block injected into the prompt (anti-repeat)."""
    entries = recent_journal(workdir, n)
    if not entries:
        return ""
    lines = ["WHAT HAS ALREADY BEEN TRIED (do not repeat):"]
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
    """Write the file; over the cap, drop the oldest fact (the topmost bullet line)."""
    while len(text.encode()) > cap:
        lines = text.splitlines()
        # find the first bullet AFTER the facts header — that's the oldest one
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
