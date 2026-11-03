# Implementation verification — 2026-11-02

Scope: benchmark runner, frozen fixtures, semantic review/scoring, and real module
entry-point wiring. No live provider calls were made during implementation.

Command: `.venv/bin/python -m unittest discover -s tests -v`

Result: **24 tests passed** (7 benchmark regressions plus 17 existing tests).
The final run completed in 0.744 seconds. This duration is offline test execution,
not a model or end-to-end research latency measurement.

Observed checks:

- Default preview reports a maximum of 39 calls and instantiates no client.
- Three repetitions require an explicit 117-call cap; inadequate caps fail before calls.
- Fake-provider integration invokes the actual `test_rig.main` and
  `select_evidence.main` entry points: 39 fake calls for one full pass, 117 for three.
- Planner output reaches both extraction and selection unchanged; all chunk facts
  reach selection; no evaluator keys, criteria or assertion IDs enter requests.
- Failed planning, extraction and selection retain incomplete status, with observed
  fake-call counts of 3, 21 and 39 respectively. They do not become empty successes.
- Original-record copying and private artifacts are checked. Semantic scores remain
  null until explicit review; structural success does not fill in human judgments.
- Controlled review fixtures verify over-selection, selection loss and invalid
  extraction scoring separately. These judgments are test doubles, not model scores.
- Evidence changes and wrong-chunk support mappings are rejected. Reassessments
  preserve previous reports. Zero denominators remain undefined, not perfect.
- Three independent passes produce per-question observed score ranges only after
  review. Failed or pending trials remain separately counted.

Outstanding: run the live benchmark, review semantic judgments, and inspect the
resulting scores. Generalization remains untested; this is a small synthetic
benchmark, not a held-out population, live web acquisition or final-answer test.
