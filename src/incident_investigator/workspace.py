"""Local artifact discovery. Imported roots are references and are never written to."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def home() -> Path:
    return Path(os.environ.get("II_HOME", "var")).expanduser().resolve()


def batch_output(prefix: str = "batch") -> Path:
    return home() / "suites" / f"{prefix}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def read_lines(path: Path):
    """Ignore incomplete final records while a running process appends JSONL."""
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        yield value
                except ValueError:
                    continue
    except OSError:
        return


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def key(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:20]


def total(rows, field):
    values = [r.get(field) for r in rows]
    return sum(values) if values and all(isinstance(v, int | float) for v in values) else None
