# Technical note

**Problem.** When production breaks, on-call engineers spend most of their time gathering and cross-checking evidence. This project tests whether a team of AI agents can do that investigation and produce a report a human can trust.

**Approach.** Six agents (Orchestrator, Observability, Dependency, Reasoning, Critic and Reporting) run as a staged LangGraph graph. Specialists gather evidence in parallel through five narrow tools over a frozen snapshot. Reasoning ranks causes, the Critic challenges the top one, and the Orchestrator decides whether to gather more, re-think or report. Models only propose. Code validates every output, owns budgets and IDs, and only accepts evidence that was actually retrieved. Each run saves a report, a trace and a checkpoint.

**Built to extend.** Agents see only tool adapters, never a live system. Today those adapters read a synthetic fault lab. Adding adapters for OpenTelemetry, Prometheus, log stores, alerting tools such as Alertmanager and deploy history would let the same agents investigate an existing product without changing the agents themselves.

**Results so far.** The first paid runs scored 0/4. After the fixes (repairing small model mistakes, a data catalog tool, query de-duplication, longer timeouts), DeepSeek V3.2 scored 5/5 at one run per fault, for about $0.05 per run. The new staged graph matched the old hub graph (4/5 each) with zero rejected outputs. Total spend is about $0.93. pass@4 and the single-agent baseline are not measured yet.

**Why testing is slow.** One investigation makes about 20 model calls at 15 to 25 seconds each, and most must wait for the previous step, so a run takes 5 to 8 minutes. A 5-fault quick suite takes 10 to 12 minutes at two runs in parallel, which is our provider rate-limit ceiling. The full benchmark is 10 incidents × 2 systems × 4 trials = 80 runs, about 4 hours and roughly $3.60. Early runs also exposed a cost limiter that blocked valid runs, and it had to be fixed before results were trustworthy.

**How to speed it up.**
- Run more investigations in parallel, using higher rate limits or several providers.
- Use smaller, faster models for the specialists and a stronger one for reasoning.
- Use prompt caching for the shared context.
- Cap critic rounds when confidence is already high.
- Keep the quick eval as the default and run the full pass@4 only at milestones.

**What's left.**
- Reliability work: resume after a crash, and recovery from tool failures.
- The 10-incident benchmark and the single-agent baseline.
- The final comparison.
- The eval console is in testing.

**Potential improvements.**
- *More robust evaluation:* more incidents and variants per failure family, pass^k to measure consistency, human review alongside an LLM judge, and turning every observed failure into a regression case.
- *Beyond the MVP:* real telemetry integrations, alert-triggered runs, cost and latency dashboards, multi-tenant access control, and object storage such as MinIO or S3 behind the existing storage interfaces.
