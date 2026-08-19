"""The requirements, as data.

`docs/requirements.md` used to be prose that someone had to remember to update. That is
the same failure mode `docs/verification.md` was built to avoid (D-11): a hand-maintained
table drifts from the code silently, and the drift is invisible precisely because the
table still looks authoritative.

So the requirements live here, each naming the evidence that decides its status, and
`dsp/generate_requirements.py` renders the document - pulling measured values from
`dsp/validate.py`'s results and from the fault-injection matrix in `sim/run_scenarios.py`.
A requirement whose evidence has never been produced says so; one whose evidence says it
failed cannot be rendered as passing.

This is the smallest honest version of a model-based systems engineering artefact: one
source of truth for intent, generated views for humans, and traceability that is computed
rather than asserted.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UserNeed:
    id: str
    text: str


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    #: User needs this requirement exists to serve. Traceability upward.
    traces_to: tuple[str, ...]
    #: What implements it. Traceability downward, to code.
    design: str
    #: How it is demonstrated.
    verification: str
    #: Where the answer comes from:
    #:   ("scenario", <requirement id>)  - the fault-injection matrix
    #:   ("validation", <key>)           - dsp/validate.py's MIT-BIH results
    #:   ("selftest", <command>)         - a module self-test, run by CI
    #:   ("none", <reason>)              - not yet verifiable, with the reason
    evidence: tuple[str, str]


USER_NEEDS: tuple[UserNeed, ...] = (
    UserNeed("UN-1", "A clinician can see a patient's heart rhythm and rate live, without touching the device"),
    UserNeed("UN-2", "The system draws attention when a vital sign leaves its safe range"),
    UserNeed("UN-3", "A displayed number is either trustworthy or visibly marked as untrustworthy — never confidently wrong"),
    UserNeed("UN-4", "A brief network or electrode problem does not silently lose patient data"),
)


REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        "SR-01", "Heart rate accurate to within ±5 bpm of reference",
        ("UN-1", "UN-3"),
        "Pan-Tompkins detection + median-of-instantaneous-rate (`dsp/vitals.py`, D-09)",
        "Automated: `dsp/validate.py` against MIT-BIH cardiologist annotations",
        ("validation", "max_hr_error_bpm"),
    ),
    Requirement(
        "SR-02", "End-to-end latency, electrode to dashboard, under 500 ms",
        ("UN-1",),
        "WebSocket streaming with no batching; trailing-window streaming DSP",
        "Fault-injection scenario: sample emitted by the device to vitals frame at a dashboard",
        ("scenario", "SR-02"),
    ),
    Requirement(
        "SR-03", "Leads-off condition alarms within 2 s",
        ("UN-2", "UN-3"),
        "AD8232 LO± pins plus a rail cross-check → frame flag → alarm engine (D-28, D-18)",
        "Fault-injection scenario: detach the electrode, time the alarm",
        ("scenario", "SR-03"),
    ),
    Requirement(
        "SR-04", "QRS detection sensitivity > 99 % on clean records",
        ("UN-1", "UN-3"),
        "5–15 Hz detection band, adaptive dual threshold, search-back, T-wave discrimination (D-07)",
        "Automated: `dsp/validate.py`, ±150 ms match window per ANSI/AAMI EC57",
        ("validation", "clean_sensitivity"),
    ),
    Requirement(
        "SR-05", "No data loss across a network outage of up to 30 s",
        ("UN-4",),
        "Device-side ring buffer with backfill on reconnect (`firmware/src/vs_ringbuf.c`, D-27)",
        "Fault-injection scenario: disconnect for 30 s, verify frame count and `seq` continuity",
        ("scenario", "SR-05"),
    ),
    Requirement(
        "SR-06", "SpO₂ within ±3 % of reference",
        ("UN-1",),
        "Ratio-of-ratios on raw red/IR, server-side (`dsp/ppg.py`, protocol v2, D-32)",
        "Fault-injection scenario with a known synthetic saturation; the R→% calibration curve remains an assumption (D-34)",
        ("scenario", "SR-06"),
    ),
    Requirement(
        "SR-07", "A signal too poor to trust is flagged, not reported as a number",
        ("UN-3",),
        "QRS energy concentration gate with its threshold derived from SR-01 (`dsp/quality.py`, D-35)",
        "Fault-injection scenario: swamp the signal, verify the rate is withheld rather than wrong",
        ("scenario", "SR-07"),
    ),
    Requirement(
        "SR-08", "Alarm thresholds are defined once and applied identically everywhere",
        ("UN-2",),
        "`RateLimits`/`DEFAULT_LIMITS` in `dsp/vitals.py`, generated into `coefficients.js` (D-12)",
        "Boundary self-tests at each threshold, plus the generated-file drift check in CI",
        ("selftest", "python dsp/vitals.py"),
    ),
)


def by_id(requirement_id: str) -> Requirement | None:
    return next((r for r in REQUIREMENTS if r.id == requirement_id), None)


def needs_served_by(requirement_id: str) -> tuple[UserNeed, ...]:
    req = by_id(requirement_id)
    if req is None:
        return ()
    return tuple(n for n in USER_NEEDS if n.id in req.traces_to)


def requirements_for_need(need_id: str) -> tuple[Requirement, ...]:
    """Downward traceability: which requirements exist to serve this need.

    Computed rather than written down, so a need cannot silently end up with no
    requirement behind it - the gap shows up in the generated matrix as an empty row.
    """
    return tuple(r for r in REQUIREMENTS if need_id in r.traces_to)
