"""Isolated deterministic and local-Codex model adapters."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class ModelResult:
    action: dict[str, Any]
    usage: ModelUsage | None
    latency_ms: float
    raw_id: str | None


class ModelAdapter(Protocol):
    async def complete(
        self, messages: Sequence[Mapping[str, Any]], schema: dict[str, Any]
    ) -> ModelResult: ...


class ScriptedModel:
    """A FIFO deterministic double. Each action must be a tagged model action object."""

    def __init__(self, actions: Sequence[dict[str, Any]] = ()) -> None:
        self.actions = deque(actions)
        self.requests: list[tuple[Sequence[Mapping[str, Any]], dict[str, Any]]] = []

    async def complete(
        self, messages: Sequence[Mapping[str, Any]], schema: dict[str, Any]
    ) -> ModelResult:
        self.requests.append((messages, schema))
        if not self.actions:
            raise RuntimeError("ScriptedModel has no queued action")
        action = self.actions.popleft()
        if not isinstance(action, dict) or action.get("kind") not in {"tools", "final"}:
            raise ValueError("scripted action must be a tagged action")
        return ModelResult(action=action, usage=ModelUsage(), latency_ms=0.0, raw_id="scripted")


class CodexExecModel:
    """A single no-retry Codex CLI call, isolated from the repository and its files."""

    def __init__(self, *, timeout_seconds: float = 30.0, executable: str = "codex") -> None:
        self.timeout_seconds = timeout_seconds
        self.executable = executable

    def build_command(self, sandbox: Path, schema_path: Path, output_path: Path) -> list[str]:
        return [
            self.executable,
            "exec",
            "-",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "-s",
            "read-only",
            "--skip-git-repo-check",
            "-C",
            str(sandbox),
            "-m",
            "gpt-6-luna",
            "-c",
            'model_reasoning_effort="low"',
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "--json",
        ]

    async def complete(
        self, messages: Sequence[Mapping[str, Any]], schema: dict[str, Any]
    ) -> ModelResult:
        if shutil.which(self.executable) is None:
            raise RuntimeError("Codex CLI is unavailable; install `codex` or use II_MODEL=scripted")
        prompt = json.dumps({"messages": list(messages)}, ensure_ascii=False)
        with tempfile.TemporaryDirectory(prefix="ii-codex-") as directory:
            sandbox = Path(directory)
            schema_path, output_path = sandbox / "schema.json", sandbox / "result.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            started = time.perf_counter()
            process = await asyncio.create_subprocess_exec(
                *self.build_command(sandbox, schema_path, output_path),
                cwd=sandbox,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(prompt.encode()), self.timeout_seconds
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                raise TimeoutError("Codex CLI model call timed out") from None
            latency_ms = (time.perf_counter() - started) * 1000
            if process.returncode:
                raise RuntimeError(f"Codex CLI failed: {stderr.decode(errors='replace')[:1000]}")
            try:
                action = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("Codex CLI returned no structured output") from exc
            usage, raw_id = _events(stdout.decode(errors="replace"))
            return ModelResult(action=action, usage=usage, latency_ms=latency_ms, raw_id=raw_id)


def _events(raw: str) -> tuple[ModelUsage | None, str | None]:
    input_tokens = output_tokens = None
    raw_id = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_id = event.get("id", raw_id) if isinstance(event, dict) else raw_id
        usage = event.get("usage") if isinstance(event, dict) else None
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens", input_tokens)
            output_tokens = usage.get("output_tokens", output_tokens)
    return (
        ModelUsage(input_tokens, output_tokens)
        if input_tokens is not None or output_tokens is not None
        else None,
        raw_id,
    )


def model_from_environment() -> ModelAdapter:
    mode = os.getenv("II_MODEL", "scripted").lower()
    if mode == "scripted":
        return ScriptedModel()
    if mode == "codex":
        return CodexExecModel()
    raise ValueError("II_MODEL must be scripted or codex")
