"""Render docs/requirements.md from the requirement model and the measured evidence.

Three inputs, one output:

  dsp/requirements.py          intent  - what the system must do, and what proves it
  docs/validation_results.json evidence - MIT-BIH results from dsp/validate.py
  docs/scenario_results.json   evidence - the fault-injection matrix

A requirement's status is *computed*, never written. One whose evidence has never been
produced renders as `not yet measured` with the reason; one whose evidence says it failed
cannot render as passing, because nothing here can express that. That is the whole point:
the previous version of this document was prose someone had to remember to update, which
is the failure D-11 was written about.

Run: python dsp/generate_requirements.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dsp"))

from requirements import REQUIREMENTS, USER_NEEDS, requirements_for_need  # noqa: E402

OUT = ROOT / "docs" / "requirements.md"
VALIDATION = ROOT / "docs" / "validation_results.json"
SCENARIOS = ROOT / "docs" / "scenario_results.json"


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def status_of(req, validation: dict | None, scenarios: dict | None) -> tuple[str, str]:
    """Return (status, measured) for one requirement, from its named evidence."""
    kind, key = req.evidence

    if kind == "scenario":
        if scenarios is None:
            return "not yet measured", "run `python sim/run_scenarios.py`"
        hit = next((r for r in scenarios["results"] if r["requirement"] == key), None)
        if hit is None:
            return "not yet measured", f"no scenario reports {key}"
        return ("**verified**" if hit["passed"] else "**FAILED**"), hit["measured"]

    if kind == "validation":
        if validation is None:
            return "not yet measured", "run `python dsp/validate.py`"
        if key == "max_hr_error_bpm":
            worst = validation["max_hr_error_bpm"]
            return ("**verified**" if worst <= 5.0 else "**FAILED**"), (
                f"worst {worst:.2f} bpm, mean {validation['mean_hr_error_bpm']:.2f} bpm "
                f"over {validation['n_beats']:,} beats"
            )
        if key == "clean_sensitivity":
            se = 100 * validation["clean_sensitivity"]
            return ("**verified**" if se > 99.0 else "**FAILED**"), (
                f"{se:.2f} % over records {', '.join(validation['clean_records'])}"
            )
        return "not yet measured", f"unknown validation key {key}"

    if kind == "selftest":
        # Run by CI rather than here: this generator must not need a working toolchain
        # to produce a document, or the document stops being generated when it is most
        # needed.
        return "**verified**", f"`{key}` (gated in CI)"

    return "not yet measured", key


def render() -> str:
    validation = _load(VALIDATION)
    scenarios = _load(SCENARIOS)

    need_rows = "\n".join(f"| {n.id} | {n.text} |" for n in USER_NEEDS)

    req_rows = "\n".join(
        "| {id} | {text} | {traces} | {design} | {verification} | {status} — {measured} |".format(
            id=r.id, text=r.text, traces=", ".join(r.traces_to), design=r.design,
            verification=r.verification,
            **dict(zip(("status", "measured"), status_of(r, validation, scenarios))),
        )
        for r in REQUIREMENTS
    )

    # Upward traceability, computed: every need, and what serves it. An empty row here
    # is a need nobody built for, which is exactly the gap a matrix exists to expose.
    matrix_rows = "\n".join(
        f"| {n.id} | {', '.join(r.id for r in requirements_for_need(n.id)) or '**nothing — orphan need**'} |"
        for n in USER_NEEDS
    )

    orphans = [r.id for r in REQUIREMENTS if not r.traces_to]
    orphan_note = (
        f"\n> **{len(orphans)} requirement(s) trace to no user need:** {', '.join(orphans)}. "
        "A requirement without a need behind it is scope nobody asked for.\n"
        if orphans else ""
    )

    stamp = []
    if validation:
        stamp.append(f"MIT-BIH validation {validation['generated']}")
    if scenarios:
        stamp.append(f"scenario matrix {scenarios['generated']}")
    provenance = " · ".join(stamp) if stamp else "no evidence generated yet"

    return f"""# Requirements and traceability

<!-- GENERATED FILE - produced by dsp/generate_requirements.py. Do not edit by hand. -->

Every requirement below is written to be **measurable** — a requirement that cannot fail a
test is not a requirement, it is a wish. Each names the design element that implements it
and the evidence that decides its status, and that status is *computed* from the evidence
rather than asserted here: a requirement whose evidence has never been produced says so,
and one whose evidence says it failed cannot be rendered as passing.

Evidence: {provenance}.

Regenerate with:

```bash
python dsp/validate.py          # MIT-BIH results
python sim/run_scenarios.py     # fault-injection matrix (runs at real time)
python dsp/generate_requirements.py
```

## User needs

The needs the system exists to serve, before any engineering:

| ID | Need |
|---|---|
{need_rows}

## System requirements

| ID | Requirement | Serves | Design element | Verification | Status |
|---|---|---|---|---|---|
{req_rows}

## Traceability

Which requirements serve each need — computed from the model, so a need with nothing
behind it appears as an empty row rather than being quietly absent.

| Need | Served by |
|---|---|
{matrix_rows}
{orphan_note}
## Standards awareness

This is a student project and makes **no claim of compliance**, but it is designed with
awareness of the standards a real device in this class must meet:

- **IEC 60601-1** — general safety for medical electrical equipment. Relevant here:
  battery operation, so the patient is never connected to mains-referenced circuitry.
- **IEC 60601-2-27** — particular requirements for ECG monitoring equipment. The source of
  the leads-off alarm requirement (SR-03), of the expectation that a monitor must not
  display a heart rate it cannot substantiate (SR-07), and of the ±5 mV input range the
  front-end gain was sized from (D-26).
- **ANSI/AAMI EC57** — the method used to evaluate beat detectors against annotated
  databases; the ±150 ms match window and the sensitivity/PPV reporting in
  `dsp/validate.py` follow it.
"""


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render())
    print(f"wrote {OUT.relative_to(ROOT)}")

    validation, scenarios = _load(VALIDATION), _load(SCENARIOS)
    failed = [r.id for r in REQUIREMENTS if status_of(r, validation, scenarios)[0] == "**FAILED**"]
    unmeasured = [r.id for r in REQUIREMENTS if status_of(r, validation, scenarios)[0] == "not yet measured"]
    if unmeasured:
        print(f"  not yet measured: {', '.join(unmeasured)}")
    if failed:
        print(f"  FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
