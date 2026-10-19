# Architecture

## The big picture

```mermaid
flowchart LR
    subgraph Sources["Evidence sources (swappable)"]
        L[Fault lab<br/>today]
        P[Prometheus /<br/>OpenTelemetry<br/>future]
        A[Alerting and<br/>deploy history<br/>future]
    end
    Sources --> S[Frozen snapshot<br/>of evidence]
    S --> T[Five evidence tools<br/>logs · metrics · changes ·<br/>dependencies · past incidents]
    T --> G[Six-agent investigation]
    G --> R[Report with cited evidence<br/>+ trace + checkpoint]
    G -.traces.-> X[Arize Phoenix]
    R --> E[Grader and eval console]
```

**Built to plug into real systems.** The agents never talk to a live system. They only call five narrow evidence tools over a frozen snapshot. Today the snapshot comes from a synthetic fault lab. To investigate a real product, you add adapters for its telemetry (OpenTelemetry, Prometheus, a log store, alerting tools such as Alertmanager, deploy history). The agents, prompts, graph and grading do not change.

## How the agents coordinate

```mermaid
flowchart LR
    START([Alert]) --> PLAN[Orchestrator<br/>plans the work]
    PLAN --> GATHER
    subgraph GATHER[Gather evidence, in parallel]
        OBS[Observability<br/>logs and metrics]
        DEP[Dependency<br/>services and deploys]
    end
    GATHER --> REASON[Reasoning<br/>ranks hypotheses]
    REASON --> CRITIC[Critic<br/>challenges the top one]
    CRITIC --> DECIDE{Orchestrator<br/>decides}
    DECIDE -->|need more evidence| GATHER
    DECIDE -->|re-think| REASON
    DECIDE -->|good enough| REPORT[Reporting<br/>writes the report]
    REPORT --> END([Report])
```

| Role | Job |
| --- | --- |
| Orchestrator | Splits the alert into tasks, then decides after each critic review: gather more, re-think, or report |
| Observability | Looks for the signal in logs and metrics |
| Dependency | Checks the services involved, recent deploys and config changes |
| Reasoning | Ranks possible causes, each backed by cited evidence |
| Critic | Raises objections to the top hypothesis |
| Reporting | Writes the final report, including any objections still open |

**Rules that keep it trustworthy**

- **Agents propose, code decides.** Models return structured JSON. The runtime validates it, assigns IDs, enforces budgets and moves the work forward.
- **Evidence must be real.** A hypothesis can only cite evidence that was actually retrieved in that run.
- **Bounded.** Each run has hard limits on calls, time and critic rounds. When it hits one, it still writes a partial report.
- **"Not sure" is allowed.** Gaps and unresolved objections are shown, not hidden.

**Graph.** Each stage is its own node in a LangGraph graph (the "staged" graph, now the default). Inside a stage, agent and tool calls run in parallel. A small commit step keeps one trusted writer for shared state. An older single-hub version is kept behind `--graph hub` and is deprecated.

## The eval loop

```mermaid
flowchart LR
    B[Normal traffic] --> F[Inject a fault] --> T[Failing traffic] --> Z[Freeze snapshot] --> I[Investigate] --> GR[Grade against<br/>hidden answer]
```

The expected answers (culprit, mechanism, key evidence) are never shown to the agents.
