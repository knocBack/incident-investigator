# Graph variants

`--graph current|explicit` is available on `investigate`, `demo`, and `eval-suite`.
The default remains `current`. Results, trace events, grades, batch metadata and
leaderboard rows record `graph`; leaderboard aggregation separates variants even
when all model selections match. Older artifacts without the field mean `current`.

The current graph centralizes workflow routing, receipt validation and call admission
in `controller`. Its advantages are a single owner of authoritative state, global
budgets and dedupe, parallel `Send` workers, and a checkpoint per external call.
Its topology hides the stage sequence inside `advance()`.

The explicit graph puts stage routing in conditional edges. Planning, gathering,
reasoning, critic review, orchestrator decisions and reporting are compiled subgraphs.
Each contains a model/commit loop; gathering also fans out individual tools with
`Send`. Expand subgraphs (`get_graph(xray=True)`) to see Observability and Dependency.
They share the gathering commit/admission boundary so simultaneous tasks cannot race
on global call budgets, fault counters, evidence provenance or cross-task dedupe.
These role workers run in parallel; stage transitions wait for the entire batch.

Both variants reuse the same task construction, prompts, schemas, call execution,
validation/repair/commit, ID allocation, provenance checks, admission limits,
dedupe, review accounting and report export. The explicit graph never calls
`controller()` or `advance()`. Its extra stage-entry/settlement checkpoints mean
revision numbers, checkpoint counts and wall time need not match the current graph.
`Control.stage` remains context for shared validation, not the workflow dispatcher.
Control has a single writer per superstep; parallel workers publish immutable
receipts through the identity-checking `inbox` reducer. Both use the same
`AsyncSqliteSaver` and `durability="sync"` at invocation; child checkpoints inherit
the saver. Global exhaustion exports the same partial fallback without another model
call. Failed control roles route to Reporting; failed Reporting retains partial artifacts.

## Inspect the compiled topology

This snippet builds both graphs and prints Mermaid without invoking a model or tool:

```python
import asyncio
from pathlib import Path

from incident_investigator.contracts import Limits
from incident_investigator.models import ScriptedModel
from incident_investigator.runtime import Investigation
from incident_investigator.sources.loader import load_snapshot

for variant in ("current", "explicit"):
    runner = Investigation(
        load_snapshot("dev-00"),
        ScriptedModel(),
        Path("."),
        Limits(),
        asyncio.Semaphore(4),
        graph=variant,
    )
    graph = runner.graph().compile()
    print(graph.get_graph(xray=True).draw_mermaid())
```

## Current

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	controller(controller)
	orchestrator(orchestrator)
	observability(observability)
	dependency(dependency)
	reasoning(reasoning)
	critic(critic)
	reporting(reporting)
	tool(tool)
	__end__([<p>__end__</p>]):::last
	__start__ --> controller;
	controller -.-> __end__;
	controller -.-> critic;
	controller -.-> dependency;
	controller -.-> observability;
	controller -.-> orchestrator;
	controller -.-> reasoning;
	controller -.-> reporting;
	controller -.-> tool;
	critic --> controller;
	dependency --> controller;
	observability --> controller;
	orchestrator --> controller;
	reasoning --> controller;
	reporting --> controller;
	tool --> controller;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

## Explicit

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	__end__([<p>__end__</p>]):::last
	__start__ --> orchestrator_plan\3abegin;
	critic\3asettle -.-> __end__;
	critic\3asettle -.-> orchestrator_decide\3abegin;
	critic\3asettle -.-> reporting\3abegin;
	gathering\3asettle -.-> __end__;
	gathering\3asettle -.-> reasoning\3abegin;
	orchestrator_decide\3asettle -.-> __end__;
	orchestrator_decide\3asettle -.-> gathering\3abegin;
	orchestrator_decide\3asettle -.-> reasoning\3abegin;
	orchestrator_decide\3asettle -.-> reporting\3abegin;
	orchestrator_plan\3asettle -.-> __end__;
	orchestrator_plan\3asettle -.-> gathering\3abegin;
	orchestrator_plan\3asettle -.-> reasoning\3abegin;
	orchestrator_plan\3asettle -.-> reporting\3abegin;
	reasoning\3asettle -.-> __end__;
	reasoning\3asettle -.-> critic\3abegin;
	reasoning\3asettle -.-> reporting\3abegin;
	reporting\3asettle --> __end__;
	subgraph orchestrator_plan
	orchestrator_plan\3abegin(begin)
	orchestrator_plan\3acommit(commit)
	orchestrator_plan\3asettle(settle)
	orchestrator_plan\3aorchestrator(orchestrator)
	orchestrator_plan\3abegin --> orchestrator_plan\3acommit;
	orchestrator_plan\3acommit -.-> orchestrator_plan\3aorchestrator;
	orchestrator_plan\3acommit -.-> orchestrator_plan\3asettle;
	orchestrator_plan\3aorchestrator --> orchestrator_plan\3acommit;
	end
	subgraph gathering
	gathering\3abegin(begin)
	gathering\3acommit(commit)
	gathering\3asettle(settle)
	gathering\3aobservability(observability)
	gathering\3adependency(dependency)
	gathering\3atool(tool)
	gathering\3abegin --> gathering\3acommit;
	gathering\3acommit -.-> gathering\3adependency;
	gathering\3acommit -.-> gathering\3aobservability;
	gathering\3acommit -.-> gathering\3asettle;
	gathering\3acommit -.-> gathering\3atool;
	gathering\3adependency --> gathering\3acommit;
	gathering\3aobservability --> gathering\3acommit;
	gathering\3atool --> gathering\3acommit;
	end
	subgraph reasoning
	reasoning\3abegin(begin)
	reasoning\3acommit(commit)
	reasoning\3asettle(settle)
	reasoning\3areasoning(reasoning)
	reasoning\3abegin --> reasoning\3acommit;
	reasoning\3acommit -.-> reasoning\3areasoning;
	reasoning\3acommit -.-> reasoning\3asettle;
	reasoning\3areasoning --> reasoning\3acommit;
	end
	subgraph critic
	critic\3abegin(begin)
	critic\3acommit(commit)
	critic\3asettle(settle)
	critic\3acritic(critic)
	critic\3abegin --> critic\3acommit;
	critic\3acommit -.-> critic\3acritic;
	critic\3acommit -.-> critic\3asettle;
	critic\3acritic --> critic\3acommit;
	end
	subgraph orchestrator_decide
	orchestrator_decide\3abegin(begin)
	orchestrator_decide\3acommit(commit)
	orchestrator_decide\3asettle(settle)
	orchestrator_decide\3aorchestrator(orchestrator)
	orchestrator_decide\3abegin --> orchestrator_decide\3acommit;
	orchestrator_decide\3acommit -.-> orchestrator_decide\3aorchestrator;
	orchestrator_decide\3acommit -.-> orchestrator_decide\3asettle;
	orchestrator_decide\3aorchestrator --> orchestrator_decide\3acommit;
	end
	subgraph reporting
	reporting\3abegin(begin)
	reporting\3acommit(commit)
	reporting\3asettle(settle)
	reporting\3areporting(reporting)
	reporting\3abegin --> reporting\3acommit;
	reporting\3acommit -.-> reporting\3areporting;
	reporting\3acommit -.-> reporting\3asettle;
	reporting\3areporting --> reporting\3acommit;
	end
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

## Fast local comparison

```sh
uv run pytest -q tests/test_m3_agents.py tests/test_quality.py tests/test_graph.py
uv run incident-investigator eval-suite --graph current --model scripted --models scripted --script tests/scripted-demo.json --quick --faults all --budget-usd 0 --out /tmp/ii-current
uv run incident-investigator eval-suite --graph explicit --model scripted --models scripted --script tests/scripted-demo.json --quick --faults all --budget-usd 0 --out /tmp/ii-explicit
```

`--quick` runs the five fault jobs with concurrency 5. Each job receives the selected
graph alongside its own model adapter; graph labels remain distinct in the leaderboard.

The runtime E2E and quality scenarios are parametrized across both variants, covering
parallel gathering, follow-up review, validation repairs, task/global budgets,
no-progress/round/wall limits, tool timeout gaps, dedupe and Reporting fallback.
A cancellation/resume test rebuilds each graph from its SQLite checkpoints and
checks that completed model/tool calls are not repeated. CLI and grading tests check
variant selection and separate leaderboard groups. The demo script intentionally
reports an inconclusive diagnosis: these local runs check execution parity and
artifacts, not live model accuracy. No paid calls are needed for this comparison.
