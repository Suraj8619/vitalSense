"""Run the fault-injection matrix and record what it measured.

Writes `docs/scenario_results.json`, which `dsp/validate.py` renders into
`docs/verification.md` and the traceability matrix. Exits non-zero if any requirement
failed, so it can gate CI (D-11).

Scenarios run at **real time** - a leads-off debounce measured in wall-clock seconds
cannot be measured faster (D-46) - so the full suite takes a few minutes. That is the
price of measuring the requirement rather than a model of it.

Run:  python sim/run_scenarios.py            # everything
      python sim/run_scenarios.py latency    # by substring
"""

from __future__ import annotations

import asyncio
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim"))

from scenarios import ALL_SCENARIOS, ScenarioResult  # noqa: E402

OUT = ROOT / "docs" / "scenario_results.json"


async def run(selected: list[str]) -> list[ScenarioResult]:
    chosen = [
        s for s in ALL_SCENARIOS
        if not selected or any(term.lower() in s.__name__.lower() for term in selected)
    ]
    if not chosen:
        print(f"no scenario matched {selected}", file=sys.stderr)
        return []

    results: list[ScenarioResult] = []
    for scenario in chosen:
        label = scenario.__name__.replace("scenario_", "")
        print(f"  {label:24s} ", end="", flush=True)
        started = time.monotonic()
        try:
            result = await scenario()
        except Exception as exc:  # a scenario that crashes is a failed scenario
            result = ScenarioResult(label, "?", "?", f"crashed: {exc}", False)
        took = time.monotonic() - started
        print(f"{'PASS' if result.passed else 'FAIL'}  {result.measured}  ({took:.0f}s)")
        results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
    selected = argv if argv is not None else sys.argv[1:]
    print(f"\nRunning {len(ALL_SCENARIOS) if not selected else '(filtered)'} scenarios at real time\n")
    started = time.monotonic()
    results = asyncio.run(run(selected))
    if not results:
        return 1

    passed = sum(r.passed for r in results)
    print(f"\n{passed}/{len(results)} scenarios passed in {time.monotonic() - started:.0f}s")

    # Only write the report when the whole matrix ran; a partial run would silently
    # narrow the verification evidence to whatever was convenient to re-run.
    if not selected:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "environment": f"Python {platform.python_version()} on {platform.system()} {platform.machine()}",
            "results": [
                {
                    "name": r.name, "requirement": r.requirement, "question": r.question,
                    "measured": r.measured, "passed": r.passed, "detail": r.detail,
                    "numbers": r.numbers,
                }
                for r in results
            ],
        }, indent=2) + "\n")
        print(f"Wrote {OUT.relative_to(ROOT)}")
    else:
        print("(filtered run - report not written)")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
