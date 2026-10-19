---
name: incident-evals
description: Maintain incident-investigator evaluations when reproducing a failed run or test, adding a regression case, or correcting fixtures and graders. Keep evaluation truth separate from runtime investigator context.
---

# Incident evaluation maintenance

Use this as a developer/evaluator workflow. Runtime investigation agents must never receive this skill, expected diagnoses, grader data, planning documents, or repository-reading tools.

Read the relevant requirements in the [PRD](../../../docs/mvp-plan.md) and the relevant case or protocol in the [evaluation specification](../../../docs/evaluation-plan.md). Do not copy case answers into this skill. The goal is reliable investigation; the required baseline comparison does not require multi-agent superiority.

## Work from the observed failure

1. Inspect the alert, run configuration, task/tool trace, returned evidence, output, and grading decision. Reproduce the failure with the smallest relevant setup. If reproduction is unavailable, record the evidence and uncertainty without inventing a result.
2. Classify the issue: agent behavior, runtime/tool defect, fixture defect, grader defect, or provider/environment failure. A justified inconclusive diagnosis is not automatically a failure. Check whether the available evidence actually supports the evaluator's expected precision.
3. Add or strengthen the smallest useful regression. Prefer a deterministic check for filtering, provenance, cutoff enforcement, state isolation/merging, retry/budget enforcement, or resume defects. Use an incident variant for an investigation-behavior gap. Reuse existing cases when they cover the failure; do not create a new incident for every ordinary bug.
4. Verify the changed check with the relevant failure and a valid outcome, then run the affected existing checks. Discover the project's actual commands; if the required runner is not implemented, record verification as pending rather than inventing a result. Do not claim that skill validation is a product test.
5. Record a short note alongside the relevant test/case or evaluation result: original failure evidence, classification, change and reason, commands run, outcome, and any remaining uncertainty. Add a reusable rule here only if it prevents a demonstrated recurring mistake.

## Preserve evaluation integrity

- Keep expected answers and grading logic outside tool-visible sources and runtime prompts. Check actual loaders/tool access; a private filename alone is insufficient.
- Preserve the ten primary cases and two reliability variants as an identifiable suite. Add development regressions separately and record exposure and fixture/prompt/grader versions. Do not call a tuned-on case unseen.
- Retain the original failed run. Correct a demonstrably flawed fixture or grader with a recorded reason and a new version; never silently relax expectations just to pass. Classify environment failures explicitly instead of deleting them or presenting them as diagnosis results.
- Reset per-run state and source-scoped fault counters. Isolate concurrent investigations and retain stable evidence/task references. On resume, preserve consumed budgets and saved fault state rather than starting a fresh allowance.
- Judge evidence support and important contradictions, not citation existence alone or an exact sequence of tool calls. Keep human semantic review alongside deterministic checks; an LLM judge is deferred.
- Before a model evaluation batch, record the model/settings, versions, finite budgets, run order, faults, and repeat count. Use the same conditions for both approaches. Retain all attempts and account for retries; missing usage is unknown, not zero.
- Initial diagnosis measurements are exploratory. Agree a threshold after reviewing them and record it before subsequent validation. All deterministic reliability checks remain mandatory. There is no preselected diagnosis score.

Keep this skill to this entrypoint initially. Add helpers or infrastructure only when repeated work justifies them; installing Harbor, containers, or additional agent frameworks is not part of this workflow.
