"""Fase 2 acceptance with a REAL claude: test fails → loop fixes it → succeeded.

Builds a workspace holding a buggy calc.py (add uses minus) plus its test, then turns
the loop loose with `python3 test_calc.py` as the verifier. The loop has to fix it itself.

Usage: python scripts/e2e_loop.py [--model sonnet]    # default sonnet (cheap)
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import config, loop  # noqa: E402
from engine.store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WS = ROOT / "workspaces" / "e2e-demo"

CALC = "def add(a, b):\n    return a - b  # BUG on purpose\n"
TEST = (
    "from calc import add\n"
    "assert add(2, 3) == 5, f'add(2,3) = {add(2, 3)}, should be 5'\n"
    "print('test passed')\n"
)


async def main() -> None:
    model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else "sonnet"

    shutil.rmtree(WS, ignore_errors=True)
    WS.mkdir(parents=True)
    (WS / "calc.py").write_text(CALC)
    (WS / "test_calc.py").write_text(TEST)

    cfg = config.load(str(ROOT / "config.yaml"))
    store = Store(str(ROOT / "workspaces" / "e2e-demo.db"))
    run_id = store.create_run(
        "Fix the bug in calc.py so that `python3 test_calc.py` passes. "
        "Do NOT modify test_calc.py.",
        "python3 test_calc.py",
        str(WS),
        model=model,
        max_iterations=3,
        max_cost_usd=2.0,
    )
    print(f"run={run_id} model={model} workdir={WS}")

    def show(ev: dict) -> None:
        if ev["type"] in ("verify", "result", "status"):
            print(f"[{ev['type']}] {ev['payload']}")

    status = await loop.run_loop(run_id, store, cfg, on_event=show)
    run = store.get_run(run_id)
    print(f"\nstatus={status} iterations={run['iterations_done']} "
          f"cost=${run['cost_total']:.4f}")
    assert status == "succeeded", "the loop failed to fix the test"
    print("Fase 2 acceptance: OK — observe→act→verify→recover really works")


if __name__ == "__main__":
    asyncio.run(main())
