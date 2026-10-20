# Evaluation

Evaluation asks two separate questions: does the runtime behave correctly, and does the report identify a supported cause? Scripted tests answer the first. Model-backed incident trials and human review are needed for the second.

## What is available now

The current head has five injectable lab families: bad configuration, deployment regression, database contention, resource exhaustion and traffic overload. Each demo freezes actual lab telemetry and saves a separate evaluator pointer. `eval` grades saved runs; `eval-suite` starts independent trials and writes grades, budget records and leaderboards.

The [historical exploratory re-grade](../evals/results/exploratory-2026-10-19/README.md) retains ten early runs: **1 correct, 4 inconclusive and 5 no_report**. Those runs used changing models, runtime versions and limits. They document development failures, not current accuracy or a fair model ranking. No new provider calls were made for this documentation update.

A scripted replay is intentionally inconclusive. Its zero correct diagnoses are expected and must not be presented as a model benchmark. See [quick start](quick-start.md) for the zero-cost suite.

## Upcoming multi-agent versus single-agent comparison

**Results pending.** The single-agent baseline, ten paired incidents and reliability variants are a separate change. The benchmark and comparison report follow later. This section reserves a place for matched results; there are no multi-versus-single numbers at this head.

The accepted [evaluation plan](evaluation-plan.md) defines ten primary incidents and two separately reported reliability variants. It requires the same model, tools, source snapshot, total allowances and settings across systems, with versioned attempts and no selective removal of failures. The five-family lab is a development suite, not a substitute for those paired cases. No numerical diagnostic acceptance threshold or superiority claim has been accepted.

## Automatic verdicts

| Verdict | Meaning in the grader |
| --- | --- |
| `correct` | The top hypothesis matches the culprit service and mechanism patterns, and cites retrieved key evidence |
| `partial` | A non-inconclusive report matches the culprit or cites key evidence, but does not meet all correct checks |
| `wrong` | A non-inconclusive report meets neither of those partial checks |
| `inconclusive` | A report exists but has no hypotheses or explicitly concludes inconclusive |
| `no_report` | No nonempty report summary exists |

These are deterministic lexical and provenance checks, not semantic proof. A report can be structurally valid and still make an unsupported causal claim. Tool receipts can justify retrieval gaps; they are not observations of an application cause.

## Repeats and reliability metrics

For each fault/model/recorded-workflow group, let `n` be the observed attempts, `c` the correct attempts and `k` the requested sample size clamped to `n`.

| Metric | Calculation | Interpretation |
| --- | --- | --- |
| pass@1 | `c / n` | Observed first-trial success rate |
| pass@k | `1 - C(n-c, k) / C(n, k)` | Estimated chance of at least one success in a sample of k observed attempts |
| pass^k | `C(c, k) / C(n, k)` | Estimated chance that every attempt in that sample succeeds |

`C(a,b)` counts combinations and is zero when `b > a`. These finite-sample estimators sample without replacement; pass^k is not computed as `(c/n)^k`. With one attempt, all three metrics are identical. Requested repeats and observed attempts can differ, so inspect batch settings and skipped entries. Missing reports and execution failures remain in observed denominators. Unstarted/skipped attempts are recorded separately and are not fabricated as completed runs.

## Other measurements

| Measurement | Meaning and limit |
| --- | --- |
| Culprit/mechanism/key-evidence checks | Whether the top hypothesis passes each deterministic diagnosis check |
| Hypothesis grounding fraction | Fraction of hypotheses with nonempty supporting references all pointing to valid retrieved observations |
| Citation observation fraction | Fraction of cited references that identify valid observations; receipts are tracked separately |
| Unknown citations | References absent from both retrieved observations and known receipts |
| Key evidence retrieved/cited | Whether the evaluator's matching observation was retrieved and whether the top hypothesis cites it |
| Calls | Admitted model and tool attempts, including retries |
| Trace events | Local event records, including validation and state transitions; not a call count |
| Wall seconds | Elapsed run/attempt time; summed parallel role durations are a separate measure |
| Input/output tokens | Actual available provider usage across calls; missing components remain unknown |
| Cost | Recorded USD cost when complete; known subtotals remain separate from unresolved charges |
| Repairs/rejections | Logged normalization and unusable-output events, separate from diagnosis quality |

## Human review

The console Rubric tab records `pass`, `partial` or `fail` with reasons for diagnosis, support, contradictions, uncertainty and actions. Review whether citations support the mechanism, whether alternatives are addressed, and whether recommendations are tied to findings with a verification step. An empty report does not earn a grounding success. Human judgments are workspace sidecars, never investigator inputs, and do not overwrite automatic verdicts.

## Cost and budget interpretation

Unknown cost is not zero. A run or suite total is unknown when an included component is missing. Raw result/grade JSON retains known subtotals and call-level usage where available. Scripted mode makes no provider requests, but its adapter does not report token/cost usage; the UI therefore shows unknown.

The suite budget guard reserves conservatively before dispatch and settles known charges. Unknown responses and post-send timeouts can retain reserves; key-usage observations may be delayed or include unrelated callers. A batch may stop before the nominal cap. The cap is an admission guard, not a guarantee that every requested trial will start.

## Limits of the evidence

Synthetic faults are authored and development-exposed. The automatic grader uses keywords and record matchers. Small samples, changing configurations and reruns do not establish production generalization, calibrated confidence or multi-agent superiority. Publish conditions and all attempts alongside future comparisons. The [console guide](ui.md) explains how to inspect the underlying reports, grades and evidence.
