# Exploratory evaluation

No acceptance threshold. Missing measurements remain null. Citation grounding is a provenance proxy; human semantic review remains pending.

| Models | Runs | Verdicts | Grounding | Mean USD | Mean tokens in/out | Wall s | Failures |
| --- | ---: | --- | --- | --- | --- | --- | ---: |
| {} | 3 | {'correct': 1, 'partial': 0, 'wrong': 0, 'inconclusive': 1, 'no_report': 1} | 0.8333333333333334 | None | None/None | 97.72806557019551 | 1 |
| {"critic": "nvidia/nemotron-3-super-120b-a12b:free", "dependency": "nvidia/nemotron-3-super-120b-a12b:free", "observability": "nvidia/nemotron-3-super-120b-a12b:free", "orchestrator": "nvidia/nemotron-3-super-120b-a12b:free", "reasoning": "nvidia/nemotron-3-super-120b-a12b:free", "reporting": "nvidia/nemotron-3-super-120b-a12b:free"} | 2 | {'correct': 0, 'partial': 0, 'wrong': 0, 'inconclusive': 1, 'no_report': 1} | None | None | None/None | 107.14871847629547 | 1 |
| {"critic": "google/gemma-4-31b-it:free", "dependency": "google/gemma-4-31b-it:free", "observability": "google/gemma-4-31b-it:free", "orchestrator": "google/gemma-4-31b-it:free", "reasoning": "google/gemma-4-31b-it:free", "reporting": "google/gemma-4-31b-it:free"} | 1 | {'correct': 0, 'partial': 0, 'wrong': 0, 'inconclusive': 0, 'no_report': 1} | None | None | None/None | 2.718050003051758 | 1 |
| {"critic": "deepseek/deepseek-chat", "dependency": "deepseek/deepseek-chat", "observability": "google/gemini-2.5-flash-lite", "orchestrator": "deepseek/deepseek-chat", "reasoning": "deepseek/deepseek-r1-0528", "reporting": "deepseek/deepseek-chat"} | 4 | {'correct': 0, 'partial': 0, 'wrong': 0, 'inconclusive': 2, 'no_report': 2} | None | None | None/None | 99.4796831872518 | 2 |
