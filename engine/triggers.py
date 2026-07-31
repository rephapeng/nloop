"""Reactive triggers: a webhook payload (Sentry/PostHog/generic) → a loop goal.

Every vendor's webhook format differs (and changes often), so the extractor is
tolerant: it tries a handful of common paths and falls back to hashing the title
for a fingerprint. The fingerprint drives dedup — the same issue must never spawn
a second loop while an active run (queued/running) exists.

Repro-first mode (the default for issue runs): the project verifier alone is not
enough — runtime errors (most Sentry issues) don't turn the build red, so the loop
would "finish" without doing anything. That is why an issue run's verify_cmd is
combined with a repro script the agent MUST write first: the file doesn't exist →
the verifier fails → the loop is forced to ACT (investigate + write a repro + fix),
and "done" means the repro passes AND the project health check passes.
"""
from __future__ import annotations

import hashlib
import re

from engine import tasks

REPRO_DIR = ".nloop/repro"
ISSUE_TASK = "issue-fix"   # built-in task_id for runs from webhook/watchdog


def _dig(d: dict, *paths: str):
    """Return the first value found across several dotted paths."""
    for path in paths:
        cur = d
        found = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                found = False
                break
        if found and cur not in (None, "", {}):
            return cur
    return None


def extract_issue(source: str, payload: dict) -> dict:
    """Normalise a webhook payload → {fingerprint, title, url, detail}."""
    if source == "sentry":
        fp = _dig(payload, "data.issue.id", "data.event.issue_id", "issue_id", "id")
        title = _dig(payload, "data.issue.title", "data.event.title",
                     "event.title", "message", "title")
        url = _dig(payload, "data.issue.web_url", "data.event.web_url",
                   "data.issue.url", "url")
        detail = _dig(payload, "data.event.culprit", "data.issue.culprit",
                      "culprit", "data.issue.metadata.value")
    elif source == "posthog":
        fp = _dig(payload, "issue_id", "event.uuid", "uuid", "id")
        title = _dig(payload, "issue_name", "title",
                     "event.properties.$exception_message", "event.event", "message")
        url = _dig(payload, "issue_url", "url", "event.url")
        detail = _dig(payload, "description",
                      "event.properties.$exception_type", "detail")
    else:  # generic — usable from a manual curl / any other vendor
        fp = _dig(payload, "fingerprint", "issue_id", "id")
        title = _dig(payload, "title", "message", "name")
        url = _dig(payload, "url")
        detail = _dig(payload, "detail", "description")

    title = str(title) if title else "(untitled issue)"
    if not fp:  # no id → fingerprint from the title, so dedup still works
        fp = hashlib.sha1(f"{source}:{title}".encode()).hexdigest()[:16]
    return {
        "fingerprint": f"{source}:{fp}",
        "title": title,
        "url": str(url) if url else "",
        "detail": str(detail) if detail else "",
    }


def repro_path(fingerprint: str) -> str:
    """Path of the per-issue repro script inside the project workdir (relative)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", fingerprint)
    return f"{REPRO_DIR}/{safe}.sh"


def compose_verify(project_verify_cmd: str, rpath: str) -> str:
    """An issue run's verifier = the repro FIRST, then the project health check.
    No repro file yet → `sh` exits 127 → the verifier fails → the loop is forced to ACT."""
    return f"sh {rpath} && ({project_verify_cmd})"


def create_issue_run(store, cfg: dict, proj: dict, source: str, issue: dict) -> str:
    """Spawn one issue-fix run from a normalised issue — the shared path for both
    the webhook (push) and the watchdog (poll), so their behaviour is identical.

    A project may point at its own registry task (`task: <id>` under triggers.projects):
    its payload is the normalised issue + source. Without that, the built-in issue-fix
    pipeline (repro-first) below is used — still recorded as `task_id=issue-fix` so it
    groups in the dashboard.
    """
    payload = {"source": source, **issue}
    if proj.get("task"):
        out = tasks.trigger(
            store, cfg, proj["task"], payload,
            idempotency_key=issue["fingerprint"],
            overrides={k: proj.get(k) for k in tasks.OVERRIDABLE},
        )
        return out["run_id"]

    verify_cmd = proj["verify_cmd"]
    rpath = None
    if proj.get("repro", True):
        rpath = repro_path(issue["fingerprint"])
        verify_cmd = compose_verify(proj["verify_cmd"], rpath)
    return store.create_run(
        build_goal(source, issue, repro_path=rpath, verify_cmd=verify_cmd),
        verify_cmd,
        proj["workdir"],
        model=proj.get("model") or cfg["claude"].get("model"),
        max_iterations=proj.get("max_iterations") or cfg["loops"]["max_iterations"],
        max_cost_usd=proj.get("max_cost_usd") or cfg["loops"]["max_cost_usd"],
        fingerprint=issue["fingerprint"],
        role=proj.get("role"),
        context_cmd=proj.get("context_cmd"),
        gate_prompt=proj.get("gate_prompt"),
        on_success_cmd=proj.get("on_success_cmd"),
        task_id=ISSUE_TASK,
        payload=payload,
        workspace=cfg.get("workspace"),
    )


def build_goal(source: str, issue: dict, *, repro_path: str | None = None,
               verify_cmd: str | None = None) -> str:
    lines = [
        f"A new issue came in from {source}: {issue['title']}",
    ]
    if issue["url"]:
        lines.append(f"Issue link: {issue['url']}")
    if issue["detail"]:
        lines.append(f"Detail: {issue['detail']}")
    if repro_path:
        lines += [
            "",
            "Work this as an issue-fix loop:",
            "1. INVESTIGATE: read the stacktrace/title above and trace the related code "
            "in this working directory until you find the root cause.",
            f"2. REPRO: write a script `{repro_path}` that REPRODUCES this error — "
            "exit != 0 while the bug is present, exit 0 once it is fixed. It must be as "
            "specific to this error as possible (a unit test / a real scenario), NOT a "
            "placeholder `exit 0` — a lying repro verifier means the issue comes straight "
            "back from production.",
            "3. FIX: repair the root cause in the code, not merely make the repro pass.",
            f"4. Whether you are done is decided by an external verifier: `{verify_cmd}` — "
            "the repro script AND the project health check must both pass.",
        ]
    else:
        lines.append(
            "Investigate the root cause of this error in the project (this working "
            "directory), then fix it until the verifier passes. Write a reproduction "
            "test first if that helps."
        )
    return "\n".join(lines)
