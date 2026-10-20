# Technical note

## Problem and approach

Incident diagnosis requires connecting symptoms to logs, metrics, dependencies and recent changes without confusing correlation with cause [1]. This project tests whether specialized agents can collect that evidence and produce an explanation a human can verify.

Six roles share a bounded staged workflow, a lead plus specialists [2]. The Orchestrator plans, Observability and Dependency gather in parallel, Reasoning ranks explanations, Critic challenges them and Reporting writes the reviewed result. The hypothesize-and-test loop follows SRE troubleshooting [1] and adversarial review used by production AI investigation tools [3]. Follow-up can request more evidence, reanalyze or preserve an unresolved objection. Role separation gives each stage a focused responsibility but adds calls and waiting time. A matched single-agent comparison is still needed to judge that tradeoff.

## Trust boundary

Agents consume a frozen snapshot through typed tools. Cutoff filtering excludes later information, and stable evidence IDs make citations inspectable. Expected answers and human reviews remain outside runtime inputs. Models propose JSON actions; code owns identities, permissions, validation, budgets and state commits. Citation provenance establishes retrieval, while human review checks whether evidence supports a claim.

The catalog and aggregation tools expose real names and compact summaries. Identical normalized searches reuse receipts. Harmless output slips are repaired and logged; unsupported certainty can become inconclusive. Missing evidence is kept explicit.

## Reliability and observability

Every external call is separated from admission and acceptance by durable checkpoint boundaries. SQLite retains successful responses when a sibling is interrupted. Resume verifies source and runtime compatibility, reuses saved work and keeps the original deadline and consumed allowances. Unknown calls stay charged and retry only within remaining limits; exactly-once remote execution is not promised.

Run/task call limits, timeouts, bounded retries and review caps guarantee finite execution. Hard exhaustion retains partial exports. A kernel lock prevents concurrent modifiers of one run, and a local runner semaphore bounds aggregate active calls.

The console connects reports to frozen evidence, automatic grades, human rubric notes and suite comparisons. Every run writes a local trace; an optional Phoenix stack displays model and tool spans. Local artifacts remain authoritative without the Phoenix stack. Unknown cost and tokens remain unknown rather than becoming reassuring zeros.

## Evaluation and remaining work

Scripted tests validate execution and recovery without paid calls. They are not diagnostic measurements. The retained early exploratory re-grade spans changing configurations and cannot establish current accuracy. The accepted evaluation protocol specifies paired incidents, reliability variants and matched system conditions. The baseline and expanded fixtures are a separate change; benchmark results and a comparison report follow later.

Future telemetry and storage adapters can reuse narrow interfaces, in the ports-and-adapters style [4]. Production access control, alert integration and automated remediation are outside this local MVP. The immediate next evidence needed is the matched quality, cost and latency comparison, including failures and human semantic review.

## References

1. Google. [Site Reliability Engineering, Chapter 12: Effective Troubleshooting](https://sre.google/sre-book/effective-troubleshooting/)
2. Anthropic, 2025. [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
3. incident.io. [Building Investigations: what it takes to build an AI SRE](https://incident.io/blog/building-investigations-what-it-takes-to-build-an-ai-sre)
4. Alistair Cockburn. [Hexagonal Architecture](https://alistair.cockburn.us/hexagonal-architecture/)

[Decisions](decisions.md#references) lists what each source supports.
