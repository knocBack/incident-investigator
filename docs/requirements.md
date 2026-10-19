# MVP requirements

The project is a multi-agent system for investigating simulated production incidents. Given an alert and operational evidence, it must investigate likely causes and recommend actions supported by evidence, rather than merely produce a diagnosis.

## Functional requirements

- Provide at least five collaborating roles, including coordination, observability, dependency analysis, reasoning, critique, and reporting. Roles exchange structured results; a fixed sequence of prompts is not sufficient.
- Provide structured tools for logs, metrics, deployment/configuration history, service dependencies, and prior incidents. Tools handle errors, timeouts, incomplete data, and misleading or conflicting observations.
- Support parallel independent work, shared investigation state, task dependencies, feedback-driven evidence collection, finite retries and budgets, clear termination, and recovery from agent or tool failures.
- Produce reports with ranked hypotheses, supporting and contradicting evidence, confidence, facts separated from assumptions, and recommended corrective and verification actions. Inconclusive is valid when evidence is insufficient.

## Evaluation and delivery

- Supply at least ten synthetic incidents, including misleading evidence, contradictions, and tool failures.
- Compare the multi-agent system with a single-agent baseline using the same tools and conditions.
- Measure root-cause accuracy, evidence grounding, tool calls, latency, and token use.
- Deliver runnable code and setup instructions, architecture and coordination documentation, synthetic data and tests, comparison results, and a technical note of at most 500 words.

## Constraints

- Any language, model provider, and framework may be used.
- Do not hardcode answers for individual incidents.
- A polished frontend is unnecessary.
- Document incomplete functionality and external dependencies.
- Preserve the coding-assistant transcript outside this public repository for the private submission.
