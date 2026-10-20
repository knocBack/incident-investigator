# Technical note

## Problem and approach

Incident diagnosis requires connecting symptoms to logs, metrics, dependencies and recent changes without confusing correlation with cause. This project tests whether specialized agents can collect that evidence and produce an explanation a human can verify.

Six roles share a bounded staged workflow. The Orchestrator plans, Observability and Dependency gather in parallel, Reasoning ranks explanations, Critic challenges them and Reporting writes the reviewed result. Follow-up can request more evidence, reanalyze or preserve an unresolved objection. Role separation gives each stage a focused responsibility but adds calls and waiting time. A matched single-agent comparison is still needed to judge that tradeoff.

## Trust boundary

Agents consume a frozen snapshot through typed tools. Cutoff filtering excludes later information, and stable evidence IDs make citations inspectable. Expected answers and human reviews remain outside runtime inputs. Models propose JSON actions; code owns identities, permissions, validation, budgets and state commits. Citation provenance establishes retrieval, while human review checks whether evidence supports a claim.

The catalog and aggregation tools expose real names and compact summaries. Identical normalized searches reuse receipts. Harmless output slips are repaired and logged; unsupported certainty can become inconclusive. Missing evidence is kept explicit.

## Reliability and observability

Every external call is separated from admission and acceptance by durable checkpoint boundaries. SQLite retains successful responses when a sibling is interrupted. Resume verifies source and runtime compatibility, reuses saved work and keeps the original deadline and consumed allowances. Unknown calls stay charged and retry only within remaining limits; exactly-once remote execution is not promised.

Run/task call limits, timeouts, bounded retries and review caps guarantee finite execution. Hard exhaustion retains partial exports. A kernel lock prevents concurrent modifiers of one run, and a local runner semaphore bounds aggregate active calls.

The console connects reports to frozen evidence, automatic grades, human rubric notes and suite comparisons. Optional local Phoenix export shows model/tool behavior. Local artifacts remain authoritative even without tracing. Unknown cost and tokens remain unknown rather than becoming reassuring zeros.

## Evaluation and remaining work

Scripted tests validate execution and recovery without paid calls. They are not diagnostic measurements. The retained early exploratory re-grade spans changing configurations and cannot establish current accuracy. The accepted evaluation protocol specifies paired incidents, reliability variants and matched system conditions. The baseline and expanded fixtures are a separate change; benchmark results and a comparison report follow later.

Future telemetry and storage adapters can reuse narrow interfaces. Production access control, alert integration and automated remediation are outside this local MVP. The immediate next evidence needed is the matched quality, cost and latency comparison, including failures and human semantic review.
