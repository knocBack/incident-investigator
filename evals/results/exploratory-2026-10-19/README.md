# Historical exploratory re-grade

All ten existing live runs from live-demo{,-2,-3}, openrouter-live-{1,2,3}
and paid-runs were graded without new model calls. The original artifacts remain
outside the repository under the supervising task's ii-m3-agents data directory.
Only summary measurements and evaluator fault assignments are committed here.
Individual grade.json and human grade.md worksheets were written separately under
the ii-evals-v0 task data directory; originals were not modified.

Result: 1 correct, 4 inconclusive, 5 no_report. The four paid attempts contain
2 inconclusive and 2 no_report verdicts, with zero correct. A missing final report
is graded no_report even when an accepted intermediate hypothesis exists.

These were development-exposed runs across changing runtime/adapter versions,
models and limits, not a controlled model comparison. Missing model metadata in
older Codex runs remains unknown (shown as an empty model mapping); it must not
be interpreted as a model name. Missing usage stays unknown. Paid-run wall times
come from runs.json and include lab setup; other wall times use trace spans.
Full tokens/cost averages are unknown when any included run lacks usage; known
subtotals remain in each run's resource totals. No result is silently dropped.

Fault assignments are in expectations.json. Paid assignments use the preserved
batch's runs.json; earlier dev-style demos use bad_configuration as directed,
with no contrary injected fault identified in their saved result/trace.

The original failure was primarily agent/runtime behavior: reports without
accepted hypotheses/evidence and exhausted specialist budgets. The deterministic
grader now distinguishes these outcomes and includes regressions for verdicts,
provenance, future citations, decoys, repaired outputs and unknown accounting.
Human semantic rubric review remains pending. No acceptance threshold is set.
The ten primary specification cases and two reliability variants remain separate;
this lab re-grade does not claim coverage of that complete comparison protocol.

Verification: `uv run pytest -q`, `uv run ruff check .`,
`uv run ruff format --check .`, and both CLI commands with local scripted input.
