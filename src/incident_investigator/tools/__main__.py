"""Print one retrieval page from every tool for the local development fixture."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from incident_investigator.sources.loader import load_snapshot
from incident_investigator.tools import changes, dependencies, history, logs, metrics


async def main() -> None:
    snapshot = load_snapshot("dev-00", Path("fixtures/dev-00"))
    calls = {
        "logs": logs(snapshot, service="checkout", limit=1),
        "metrics": metrics(snapshot, service="checkout", limit=1),
        "changes": changes(snapshot, service="inventory", limit=1),
        "dependencies": dependencies(snapshot, service="checkout"),
        "history": history(snapshot, service="checkout", limit=1),
    }
    for name, page in calls.items():
        print(json.dumps({name: (await page).model_dump(mode="json")}, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
