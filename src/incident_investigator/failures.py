"""Execution failure summaries, independent of diagnostic grading."""

from collections import Counter

FAILURE_CLASSES = (
    "rate_limited",
    "provider_error",
    "timeout",
    "output_truncated",
    "budget_censored",
    "other",
)


def failure_counts(failures):
    counts = Counter(failure["class"] for failure in failures)
    return {name: counts[name] for name in FAILURE_CLASSES}


def run_failures(control, inbox):
    failures = []
    for call_id, call in control["calls"].items():
        receipt = inbox.get(call_id, {})
        error = receipt.get("error")
        truncated = receipt.get("finish_reason") == "length"
        if error or truncated:
            failures.append(
                {
                    "class": "output_truncated"
                    if truncated
                    else receipt.get("failure_class", "other"),
                    "message": error
                    or f"Output truncated for role {call['role']} at {receipt.get('output_limit') or 'provider'} tokens",
                    "role": call["role"],
                    "kind": call["kind"],
                    "call_id": call_id,
                    "retry": call.get("retry", 0),
                    "retry_of": call.get("retry_of"),
                }
            )
    for task in control["tasks"].values():
        if not task.get("failure"):
            continue
        task_calls = [
            call_id
            for call_id, call in control["calls"].items()
            if call["task_id"] == task["task_id"]
        ]
        last_failure = next(
            (f for f in failures if task_calls and f["call_id"] == task_calls[-1]), None
        )
        terminal_class = task.get("failure_class") or "other"
        if last_failure and (
            last_failure["message"] == task["failure"]
            or terminal_class == last_failure["class"]
            and terminal_class in {"timeout", "output_truncated"}
        ):
            continue
        failures.append(
            {
                "class": terminal_class,
                "message": task["failure"],
                "role": task["role"],
                "kind": "task",
                "call_id": None,
            }
        )
    return failures
