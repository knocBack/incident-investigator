"""Local invocation ownership, shared runner capacity and immutable run metadata."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from weakref import WeakKeyDictionary

from incident_investigator.sources.loader import CaseSnapshot, load_snapshot

_CAPACITY: WeakKeyDictionary = WeakKeyDictionary()
RUNNER_CONCURRENCY = 4


def runner_capacity() -> asyncio.Semaphore:
    """One aggregate permit pool per local event-loop runner, including API callers."""
    loop = asyncio.get_running_loop()
    if loop not in _CAPACITY:
        _CAPACITY[loop] = asyncio.Semaphore(RUNNER_CONCURRENCY)
    return _CAPACITY[loop]


@contextmanager
def invocation_lock(directory: Path):
    # Kernel ownership survives neither exit nor SIGKILL; the stable inode is retained.
    with (directory / "invocation.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("investigation already has an active modifying invocation") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def versions(snapshot: CaseSnapshot) -> dict:
    root = Path(__file__).parent

    def digest(paths):
        checksum = hashlib.sha256()
        for path in sorted(paths):
            checksum.update(str(path.relative_to(root)).encode())
            checksum.update(path.read_bytes())
        return checksum.hexdigest()

    return {
        "dataset": snapshot.dataset_digest,
        "prompt": digest((root / "prompts").glob("*.md")),
        "tool_contract": digest((root / "tools").glob("*.py")),
        "runtime": digest(
            [
                *root.glob("*.py"),
                *(root / "models").glob("*.py"),
                *(root / "sources").glob("*.py"),
                *(root / "telemetry").glob("*.py"),
            ]
        ),
        "dependencies": {
            name: version(name)
            for name in (
                "langgraph",
                "langgraph-checkpoint",
                "langgraph-checkpoint-sqlite",
                "pydantic",
            )
        },
        "checkpoint_schema": "m4.v1",
    }


def save_source(snapshot: CaseSnapshot, directory: Path) -> None:
    """Persist only the registered frozen source, never evaluator files or source paths."""
    root = directory / "source"
    root.mkdir()
    (root / "alert.json").write_text(json.dumps(dict(snapshot.alert)))
    (root / "faults.json").write_text(json.dumps(dict(snapshot.fault_plan)))
    (root / "observations.jsonl").write_text(
        "".join(row.model_dump_json() + "\n" for row in snapshot.observations)
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "cases": {
                    snapshot.case_id: {
                        "alert": "alert.json",
                        "observations": "observations.jsonl",
                        "fault_plan": "faults.json",
                        "dataset_digest": snapshot.dataset_digest,
                    }
                }
            }
        )
    )


def resume_metadata(directory: Path) -> tuple[dict, CaseSnapshot]:
    """Validate read-only before SQLite saver setup or any adapter invocation."""
    metadata = json.loads((directory / "run.json").read_text())
    snapshot = load_snapshot(metadata["case_id"], directory / "source")
    current = versions(snapshot)
    if metadata.get("versions") != current:
        changed = sorted(
            key for key in current if metadata.get("versions", {}).get(key) != current[key]
        )
        raise ValueError("incompatible resume versions: " + ", ".join(changed))
    if not (directory / "state.sqlite").is_file():
        raise ValueError("no saved checkpoint to resume")
    return metadata, snapshot
