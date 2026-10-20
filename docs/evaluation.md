# Evaluation

Evaluation asks two separate questions. Does the runtime behave correctly? Does the report identify a supported cause? Scripted tests answer the first. Model-backed trials and human review are needed for the second.

## What is evaluated

| Item | What it is |
| --- | --- |
| Primary set | Ten incidents: the five lab families plus `case-02`, `case-04`, `case-06`, `case-08` and `case-10` |
| Variants set | R1 (`reliability-01`, a transient retrieval failure to recover from) and R2 (`reliability-02`, where the exact cause cannot be established) |
| Multi-agent system | The six-role staged investigation: orchestrator, two specialists, reasoning, critic and reporting |
| Single-agent baseline | One investigator conversation with the same tools, frozen snapshot, model, limits profile and caps |

`--system both` runs each case on both systems with the same snapshot and limits. `--run-budget-usd`, `--call-seconds` and `--wall-seconds` apply equally to both. Every attempt gets fresh lab and model state. The accepted [evaluation plan](evaluation-plan.md) defines the cases, variants and parity rules.

A zero-cost paired run:

```sh
uv run incident-investigator eval-suite --model scripted --models scripted \
  --script tests/scripted-paired.json --system both --faults case-02 --budget-usd 0
```

Both scripted reports are inconclusive by design. A scripted replay checks execution, not diagnosis, so its zero correct verdicts are not a benchmark. The [console guide](ui.md#run-evaluation) shows the same launch from the browser.

## Automatic verdicts

The grader is `grader.v3`. Each grade records its version.

| Verdict | Meaning in the grader |
| --- | --- |
| `correct` | The top hypothesis names the culprit service and mechanism, cites retrieved key evidence, cites an observation from every required evidence group, and every supporting reference is valid |
| `partial` | The report names the culprit or cites key evidence, but fails at least one correct check |
| `wrong` | The report meets neither partial check. For R2, also an unjustified specific claim |
| `inconclusive` | A report exists but has no hypotheses or concludes inconclusive |
| `no_report` | No nonempty report summary exists |

Grader v3 rules that matter when reading results:

- **Receipt-only support is not enough.** A tool receipt is a valid reference, because it shows what retrieval returned. It never establishes a cause. Key evidence and required groups must be retrieved observations.
- **Valid means in scope.** A supporting reference must belong to the same dataset and fall before the cutoff.
- **R2 is graded on uncertainty.** Its expected diagnosis is inconclusive. It is `correct` only when uncertainty is justified (see [reliability variants](#reliability-variants)); otherwise `wrong`.

These are lexical and provenance checks, not semantic proof. A report can pass them and still make an unsupported causal claim. The Grade tab gives a one-line reason for each verdict.

## Grounding

[Grounding](glossary.md) is scored per attempt over its ranked hypotheses. A hypothesis counts only when every supporting reference is valid and at least one is a retrieved observation. A hypothesis backed only by receipts scores zero.

Suites show it as **score (m/n)**. The score is the mean over measured attempts. `m` counts attempts that had a score; `n` counts all retained attempts. An attempt with no hypotheses has no score, so `0.67 (3/5)` means three of five attempts were measured. **unknown (0/1)** means nothing was measured. Grounding checks provenance, not whether the evidence supports the claim.

## Failures, errors and stops

Execution failure, model-call error and budget-censored are defined in the [glossary](glossary.md). This table says how each outcome is recorded.

| Outcome | How it is recorded |
| --- | --- |
| Execution failure | Run status `failed`, `resource_exhausted` or `interrupted` |
| Model-call error | Counted per failed request, including retries. The run can still complete, sometimes with a partial report |
| `output_truncated` | The model hit its output-token limit (the adapter caps output at 4,096 tokens) before a valid answer. The affected role fails |
| Budget-censored | Stop reason `suite budget reservation refused call`. The attempt fails and keeps its report so far |
| Cancelled in flight | An attempt interrupted by the console's Cancel. Its partial report is kept |
| Skipped | A planned attempt that never started, for example "suite cancelled before admission". It is recorded but not counted as observed |

A truncated single investigator ends with no report. In the multi system, a truncated specialist can be recorded inside a run that still completes. Leaderboard files count interrupted runs as execution failures; the console lists user-cancelled ones separately as Cancelled in flight.

Failures and missing reports stay in observed denominators. Skipped attempts do not.

## Repeats and reliability metrics

For each incident and system, let `n` be the observed attempts, `c` the correct attempts and `k` the requested sample size clamped to `n`.

| Metric | Calculation | Interpretation |
| --- | --- | --- |
| pass@1 | `c / n` | Observed per-trial success rate |
| pass@k | `1 - C(n-c, k) / C(n, k)` | Chance that at least one of k sampled attempts succeeds |
| pass^k | `C(c, k) / C(n, k)` | Chance that all k sampled attempts succeed |

The pass@k estimator comes from the Codex paper [1]. pass^k and its estimator come from tau-bench [2]. Anthropic's agent-eval guide [3] explains when each matters: pass@k when one success is enough, pass^k when users expect consistent behavior.

`C(a,b)` counts combinations and is zero when `b > a`. The estimators sample without replacement; pass^k is not `(c/n)^k`. With one attempt, all three metrics are equal. `--k` sets the cap (default four). Suite rows report means across incidents.

### Reliability variants

R1 and R2 are reported separately from primary accuracy.

- **R1 recovery:** the injected retrieval failure must actually happen, and a bounded retry of the same request must then succeed. Reported as exercised / recovered / not exercised, plus recovered and diagnostically correct. A correct diagnosis without an exercised fault is not recovery.
- **R2 justified uncertainty:** the run completes with an inconclusive diagnosis, names the suspect service, cites observations from the required groups, explains the gap, and makes no forbidden specific claim.

## Other measurements

| Measurement | Meaning and limit |
| --- | --- |
| Culprit / mechanism / key-evidence checks | Whether the top hypothesis passes each deterministic check |
| Citation observation fraction | Fraction of cited references that are valid observations; receipts are listed separately |
| Unknown citations | References absent from both retrieved observations and receipts |
| Calls | Admitted model and tool attempts, including retries |
| Trace events | Local event records, including validation and transitions; not a call count |
| Wall seconds | Elapsed attempt time; summed role durations are a separate measure |
| Input/output tokens | Provider usage across calls; missing parts stay unknown |
| Cost | Recorded USD when complete; known subtotals stay separate |

## Human review

The console Rubric tab records `pass`, `partial` or `fail` with reasons for diagnosis, support, contradictions, uncertainty and actions. Check that citations support the mechanism, alternatives are addressed, and recommendations carry a verification step. Human judgments are workspace sidecars. They never reach investigator inputs or overwrite automatic verdicts.

## Cost and budget interpretation

Unknown cost is not zero. A total is unknown when any part is missing. Scripted mode makes no provider requests, and the console shows "scripted replay, no charge".

The suite guard reserves conservatively before each call and settles known charges after it. Unknown responses and post-send timeouts can keep their reserve. A batch can stop before the nominal cap, and a refusal can end an attempt midway. The cap is an admission guard, not a promise that every planned attempt runs.

## Multi-agent vs single-agent results

One exploratory paired run, launched once from the console on 2026-10-20. All ten attempts are reported; none were removed.

| Setting | Value |
| --- | --- |
| Model | `deepseek/deepseek-v3.2` for every role in both systems |
| Cases | Primary set: `case-02`, `case-04`, `case-06`, `case-08`, `case-10` |
| Systems | `both` (multi and single), staged workflow |
| Repeats, concurrency | 1 and 1 |
| Limits | `demo-live` profile, 180 s per call, 1,800 s per run, 64,000 max input tokens |
| Caps | Suite cap USD 0.45, per-run cap USD 0.08 |
| Runtime | Commit `56161c9` of the paired-evaluation change; grades from `grader.v3` |

**Spend: USD 0.441042 of the 0.45 cap.** The console total, the suite budget ledger (0.44104176) and the provider key-usage delta agree to the sixth decimal.

### By system

| System | Runs | Execution failures | Diagnostic verdict rate | Grounding | Mean model/tool calls | Mean tokens in/out | Mean USD | Mean wall s |
| --- | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: |
| multi | 5 | 0 | 0.4 | 0.666667 (5/5) | 19/19 | 300785/9910.4 | 0.0631203 | 271.891 |
| single | 5 | 2 | 0.2 | 0.666667 (3/5) | 8.6/10.2 | 142717/5125.6 | 0.0250881 | 145.627 |

### By incident

With one attempt per cell, pass@1, pass@k and pass^k all equal 1 for `correct` and 0 otherwise.

| Incident | System | Verdict | Execution failures | Grounding | Model/tool calls | Tokens in/out | USD | Wall s |
| --- | --- | --- | ---: | ---: | --- | --- | ---: | ---: |
| case-02 | multi | correct | 0 | 0.333333 (1/1) | 18/18 | 311343/7188 | 0.0651021 | 172.201 |
| case-02 | single | partial | 0 | 0.666667 (1/1) | 13/12 | 278496/4100 | 0.0467606 | 134.182 |
| case-04 | multi | correct | 0 | 1 (1/1) | 22/24 | 378310/10655 | 0.0768921 | 328.915 |
| case-04 | single | no_report | 1 | unknown (0/1) | 4/3 | 41160/8493 | 0.0112083 | 205.863 |
| case-06 | multi | partial | 0 | 0.333333 (1/1) | 16/15 | 231025/15107 | 0.0521624 | 404.119 |
| case-06 | single | correct | 0 | 1 (1/1) | 8/13 | 153119/3804 | 0.0252571 | 108.939 |
| case-08 | multi | partial | 0 | 1 (1/1) | 18/21 | 300572/8156 | 0.0629107 | 251.74 |
| case-08 | single | partial | 0 | 0.333333 (1/1) | 11/15 | 165673/4272 | 0.0263943 | 144.891 |
| case-10 | multi | partial | 0 | 0.666667 (1/1) | 21/17 | 282675/8446 | 0.0585341 | 202.481 |
| case-10 | single | no_report | 1 | unknown (0/1) | 7/8 | 75135/4959 | 0.0158202 | 134.259 |

### What happened to each attempt

| Class | Count | Attempts |
| --- | ---: | --- |
| Completed pairs | 3 | case-02, case-06, case-08 |
| Output-truncated failure | 1 | case-04 single: `output_truncated` after 4 model calls (41160/8493 tokens, USD 0.0112) |
| Budget-censored | 1 | case-10 single: `suite budget reservation refused call` after 7 model calls (USD 0.0158) |

Eight of ten attempts completed. On the three completed pairs, multi was correct once (case-02), single was correct once (case-06), and neither was correct on case-08.

### Hand spot-checks

1. **case-06 single, `correct`, grounding 1.0.** The report names database deadlocks from reversed lock ordering after an inventory deployment. It cites the raw deadlock log lines, the inventory lock-sequence warnings and the checkout 503s. All required checks pass. The grade is consistent with the report.
2. **case-06 multi, `partial`, grounding 0.33.** The summary names the right culprit and mechanism. Its support is the deployment change record plus seven metric and log-count receipts, with no raw deadlock log lines. `key_evidence_cited` and `required_evidence_groups` fail. This is the receipt-only rule at work, and the grade is consistent.

The other eight reports had no semantic review.

### What this does and does not show

**n = 1 per cell. This run does not establish which system is better.** The headline 0.4 versus 0.2 includes the two single attempts that did not complete. On completed pairs the systems tied one correct each. Single used fewer calls, tokens and dollars per attempt, partly because two of its attempts stopped early. A real comparison needs repeats on all ten incidents and the R1/R2 variants.

Run artifacts are retained locally by the maintainer, not in this repository: reports, grades, `budget.json`, `leaderboard.md` and Phoenix traces for suite `d3b5b956741141dbab20ca09550c2638`.

## Earlier results

The [historical exploratory re-grade](../evals/results/exploratory-2026-10-19/README.md) retains ten early runs: **1 correct, 4 inconclusive and 5 no_report**. They used changing models, runtime versions and limits. They document development failures, not current accuracy.

## Limits of the evidence

Synthetic faults are authored and development-exposed. The grader uses keywords and record matchers. Small samples and changing configurations do not establish production generalization, calibrated confidence or multi-agent superiority. Publish conditions and every attempt with any comparison. The [console guide](ui.md) shows how to inspect reports, grades and evidence.

## References

1. Chen et al., 2021. [Evaluating Large Language Models Trained on Code](https://arxiv.org/abs/2107.03374). Section 2.1, equation 1, defines the unbiased pass@k estimator `1 - C(n-c, k) / C(n, k)`.
2. Yao et al., 2024. [tau-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains](https://arxiv.org/abs/2406.12045). Proposes pass^k for reliability over multiple trials, estimated by `C(c, k) / C(n, k)`.
3. Anthropic, 2026. [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents). Explains pass@k for tools where one success matters and pass^k where consistency is essential, and notes they are identical at k = 1.
