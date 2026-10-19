"""Runtime-selected, source-scoped retrieval fault decorator."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from incident_investigator.tools.contracts import Failure, ToolPage


@dataclass(frozen=True)
class FaultSelection:
    outcome: Literal["none", "timeout", "error", "partial"] = "none"
    keep_first: int | None = None
    rule_id: str | None = None


def apply_fault(
    query_callable: Callable[..., Awaitable[ToolPage]],
    selection: FaultSelection,
    *,
    timeout_seconds: float = 10.0,
) -> Callable[..., Awaitable[ToolPage]]:
    """Return a stateless wrapper; admission owns selection and counters."""

    async def wrapped(*args: Any, **kwargs: Any) -> ToolPage:
        if selection.outcome == "timeout":
            async with asyncio.timeout(timeout_seconds):
                await asyncio.Event().wait()
            raise AssertionError("unreachable")
        page = await query_callable(*args, **kwargs)
        if selection.outcome == "error":
            return page.model_copy(
                update={
                    "source_status": "failed",
                    "observations": [],
                    "available_match_count": None,
                    "next_cursor": None,
                    "error": Failure(
                        code="source_error", message="simulated source error", retryable=True
                    ),
                }
            )
        if selection.outcome == "partial":
            return page.model_copy(
                update={
                    "source_status": "partial",
                    "observations": page.observations[: selection.keep_first],
                    "available_match_count": None,
                    "next_cursor": None,
                    "error": Failure(
                        code="source_incomplete",
                        message="simulated incomplete retrieval",
                        retryable=False,
                    ),
                }
            )
        return page

    return wrapped
