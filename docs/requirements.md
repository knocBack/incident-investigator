# Product scope

Given an alert and operational evidence, investigate likely causes and recommend checks supported by that evidence. The MVP is local and uses synthetic incidents; it is not an autonomous remediation system.

## Implemented capabilities

- Six collaborating roles with structured exchange, concurrent evidence collection and critic-driven follow-up.
- Typed tools for logs, metrics, changes, dependencies and history, plus discovery and aggregation helpers.
- Frozen sources with cutoff enforcement, stable citations and explicit complete, partial and failed retrieval outcomes.
- Ranked hypotheses, supporting and contradicting evidence, uncertainty, facts, assumptions and recommended verification.
- Finite calls, retries, review rounds and deadlines; partial exports; compatible checkpoint resume and invocation fencing.
- A five-family fault lab, scripted tests, offline grader, repeated evaluation suites and a local evaluation console.

## Remaining evaluation work

The ten primary incidents, both reliability variants and the single-agent baseline exist. One exploratory comparison has run; a repeated, matched comparison is still pending. See [evaluation](evaluation.md#multi-agent-vs-single-agent-results).

## Constraints

Runtime agents must not receive expectations, grading data, planning documents or repository-reading tools. They must not hardcode incident answers. Preserve local artifacts and finite execution limits. Keep failed and inconclusive outcomes visible. Recommendations do not mean a fix was applied.

Real telemetry integrations, alert webhooks, public hosting, authentication, object storage and automated remediation are outside the current implementation.
