"""Acceptance Fase 1: verifier + adapter claude -p (subscription-safe).

Usage:
    python scripts/smoke.py                 # verifier only (gratis)
    python scripts/smoke.py --with-claude   # + spawn 1x `claude -p` beneran
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import claude_cli, verifier  # noqa: E402


async def main() -> None:
    v = await verifier.verify("exit 0", cwd=".")
    assert v.passed and v.exit_code == 0, v
    v = await verifier.verify("echo boom; exit 3", cwd=".")
    assert not v.passed and v.exit_code == 3 and "boom" in v.output, v
    print("verifier: OK (exit 0 → pass, exit 3 → fail, output kebaca)")

    if "--with-claude" in sys.argv:
        events: list[str] = []
        res = await claude_cli.run(
            "Reply with exactly: nloop-ok",
            cwd=".",
            max_turns=1,
            timeout_sec=120,
            on_event=lambda t, p: events.append(t),
        )
        print(
            f"claude: ok={res.ok} subtype={res.subtype!r} cost=${res.cost_usd:.4f} "
            f"turns={res.num_turns} session={res.session_id} events={events}"
        )
        print(f"claude text: {res.result_text[:100]!r}")
        assert res.ok, f"claude -p gagal: {res.subtype} / {res.stderr_tail[-500:]}"
        assert res.session_id, "session_id kosong"
        print("claude adapter: OK (jalan tanpa ANTHROPIC_API_KEY → subscription)")


if __name__ == "__main__":
    asyncio.run(main())
