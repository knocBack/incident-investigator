# Key decisions

High-level choices only. Detailed records are in [docs/decisions.md](../decisions.md).

| Decision | Why |
| --- | --- |
| Investigate a **frozen snapshot**, never the live system | Runs are safe, repeatable and fair to compare |
| **Six roles with a critic**, not one agent | Splitting the work and challenging the answer catches wrong causes. M5 measures whether it beats a single agent |
| **Every claim cites retrieved evidence** | Reports can be checked, and invented evidence is rejected |
| **Agents propose, code decides** | Models are unreliable at bookkeeping. Code owns IDs, budgets, routing and stopping |
| **Repair small model mistakes instead of rejecting them** | Early paid runs failed on cosmetic errors. Repairing them took accuracy from 0/4 to 5/5 |
| **Agents can look up what data exists** (a catalog tool) | Models were guessing metric names. Real stacks expose discovery the same way |
| **Staged graph as the default** | Same accuracy as the hub design, clearer flow and traces. The hub graph is opt-in and deprecated |
| **pass@k and pass^k, with pass@4 as the bar** | One lucky run proves little. pass@k checks the system *can* solve a case, pass^k checks it does so *consistently* |
| **Quick eval by default** (5 faults, 1 run, in parallel) | Gives a check in minutes instead of hours. Full pass@4 runs only at milestones |
| **Paid OpenRouter models for testing, with a hard budget cap** | Results reflect models a team would really use, and spending can't run away |
| **Arize Phoenix for agent traces, plus a slim console** | Phoenix shows prompts, tool calls and timing. The console covers what it doesn't: frozen evidence, grades and the leaderboard |
| **Storage behind interfaces** (Repository pattern) | Local files today. An object store such as MinIO or S3 later, with no other code changes |
| **Ports and adapters for evidence tools** | The same agents can be pointed at Prometheus, OpenTelemetry or alerting data later |
