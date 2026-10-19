# Eval console (in testing)

A local web console to launch evaluations and review results. Agent behaviour (prompts, tool calls, tokens, timing) opens in Arize Phoenix. The console covers what Phoenix doesn't: the frozen evidence, grades against the expected answer, and the leaderboard.

Status: implemented in [PR #12](https://github.com/knocBack/incident-investigator/pull/12) and in testing. The screens below are from the approved design, built on real saved runs. Features not in the MVP, such as human grading, are hidden or greyed out.

## All runs in one place
![Runs list](img/console-runs.png)

## One run: report, grade and frozen evidence
![Run detail](img/console-detail.png)

## Launch a selective evaluation
![Run evaluation](img/console-launch.png)

## Agent trace view (Phoenix)
![Phoenix trace](img/console-phoenix.png)

<!-- Add screenshots of the running console here once PR #12 lands:
![Live console](img/live-console.png)
![Live Phoenix trace](img/live-phoenix.png)
-->
