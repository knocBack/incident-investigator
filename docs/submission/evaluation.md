# Evaluation results so far

**Setup.** The lab injects one of five faults: bad configuration, deployment regression, database contention, resource exhaustion and traffic overload. It freezes the evidence, the agents investigate, and a grader checks the report against a hidden answer: the right culprit, the right mechanism, and the key evidence cited.

These are early, exploratory results, mostly with one run per fault. pass@4 and the single-agent baseline have **not been measured yet**. A complete benchmark will be shared after the in-progress test finishes.

## 1. First paid runs, before quality fixes

| Models | Correct | Cost |
| --- | --- | --- |
| deepseek-chat (most roles), gemini-2.5-flash-lite (observability), deepseek-r1 (reasoning) | **0 / 4** | about $0.01 per run |

Why they failed: the runtime rejected small formatting mistakes, agents guessed metric names that didn't exist, specialists repeated the same queries, and the 30-second call timeout was too short. Every run still ended safely with a partial report.

## 2. After the fixes: model screening

| Model (all six roles) | Result |
| --- | --- |
| **DeepSeek V3.2** | **5 / 5 correct**, one run per fault, all citing the key evidence. 287 to 482 s per run, about $0.047 per run |
| Claude Haiku | Not measured. The provider returned "rate limited" (HTTP 429). An earlier smoke run found the right cause |
| GPT-5-mini, Gemini 3.1 Flash-Lite | Not run. The old cost limiter blocked them (since fixed) |

## 3. Staged graph vs hub graph (DeepSeek V3.2, 5 faults, 2 runs at a time)

| | Hub (old) | Staged (new default) |
| --- | --- | --- |
| Correct, first attempt | 2 / 5 | 2 / 5 |
| Correct, after re-running runs blocked by the cost limiter | 4 / 5 | 4 / 5 |
| Model outputs rejected | 3 | 0 |
| Model calls / tool calls | 99 / 93 | 92 / 81 |
| Time for the 5-fault suite | about 10 min | about 12 min |
| Cost of the 5-fault suite | about $0.15 | about $0.15 |

Both graphs lost their first-attempt failures to an over-cautious cost limiter, not to wrong diagnoses. That limiter now charges actual billed cost.

**Total model spend so far: about $0.93.**

## What is left

- pass@4 on the five faults (about 20 runs).
- The 10-incident benchmark plus 2 reliability variants, for both the multi-agent and the single-agent systems.
- Human review of report quality alongside the automatic grade.
