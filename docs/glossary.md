# Glossary

| Term | Meaning |
| --- | --- |
| Agent role | A model given a particular responsibility, prompt and permitted actions |
| Orchestrator | The role that plans specialist work and decides how to address objections |
| Specialist | Observability or Dependency, the two roles allowed to retrieve evidence |
| Hypothesis | A possible cause with supporting evidence, contradictions and assumptions |
| Critic | The role that challenges one identified hypothesis revision |
| Observation | An immutable source record with a stable identity |
| Citation | A reference to a retrieved observation or tool receipt |
| Tool receipt | A saved query outcome; it can show a gap but cannot itself establish an application cause |
| Frozen snapshot | Evidence fixed at a cutoff, identified by a digest |
| Cutoff | Latest time at which evidence could be available to the investigation |
| Digest | A content hash used to identify and verify a snapshot or version |
| Tool registry | Typed functions available to a role, with validated arguments |
| Catalog | A tool that lists names, fields and data available in the snapshot |
| Graph | The executable workflow of nodes and conditional edges |
| Stage/subgraph | A workflow phase containing its own admission, call and settlement loop |
| Send | LangGraph fan-out of independent call workers |
| Control state | The runtime-owned record of tasks, evidence, hypotheses, budgets and status |
| Receipt | The immutable response, error, timing and usage from one call |
| Checkpoint | Durable saved graph progress used for compatible resume |
| Invocation | One process execution starting or resuming a run |
| Repair | Logged normalization of a harmless deviation before strict validation |
| Inconclusive | A report that says the evidence does not support a sufficiently specific cause |
| Partial report | Preserved findings and gaps when the investigation could not complete all intended work |
| Fault injection | Deliberately changing lab behavior to create an incident |
| Tool fault | A controlled timeout or partial response; separate from the application's cause |
| Trial/attempt | One observed investigation in an evaluation suite |
| Expected answer | Evaluator-only culprit, mechanism and evidence requirements |
| Grader | Code that checks a saved report against evaluator expectations |
| Human rubric | Five separately reasoned judgments about diagnosis, support, contradictions, uncertainty and actions |
| pass@1 | Correct attempts divided by observed attempts |
| pass@k | Estimated chance of at least one correct result in k sampled attempts |
| pass^k | Estimated chance all k sampled attempts are correct |
| Model parity | Holding the model and settings fixed across compared systems |
| Single-agent baseline | One investigator conversation with the same tools, snapshot, model and limits as the team |
| Grounding | Fraction of ranked hypotheses whose valid supporting references include a retrieved observation |
| Execution failure | A run that stopped without completing reporting; not a wrong diagnosis |
| Model-call error | A failed model request, counted even when the run later completes |
| Budget-censored | An attempt ended because the suite budget guard refused a further call |
| R1 / R2 | Reliability variants: recovery from a transient retrieval failure, and justified uncertainty |
| Budget reserve | Capacity held before dispatch, distinct from measured usage |
| Known subtotal | Available measurements summed without pretending missing measurements are zero |
| OpenRouter | The optional provider API used by the model adapter |
| Scripted replay | Explicit canned actions for deterministic execution tests |
| Trace | Local event records of a run, including calls, validation and state transitions, saved in `trace.jsonl` |
| Phoenix | An external local viewer for agent, model and tool spans; the Phoenix stack is optional |
| Artifact store | Interface for reading, writing and listing opaque artifact keys |
| Run repository | Interface for indexing and selecting saved runs and suites |
| Evaluator catalog | Separate interface for discovering faults and expected answers |
