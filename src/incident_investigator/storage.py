"""Repository ports and local adapters. Keys are opaque to console/API callers.

An object-store adapter only implements ArtifactStore; indexing/query and views stay
unchanged. Import mounts are read-only, with reviews stored under the local home.
"""

from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from incident_investigator.graph_config import recorded_graph
from incident_investigator.workspace import home, total, write_json


def object_key(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("invalid artifact key")
    return str(path)


def identity(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:20]


class ArtifactStore(ABC):
    @abstractmethod
    def read(self, key: str) -> bytes | None: ...

    @abstractmethod
    def write(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def list_keys(self, prefix: str = "") -> list[str]: ...

    @abstractmethod
    def import_tree(self, source: Path) -> str:
        """Import without modifying the source; return its artifact prefix."""

    @abstractmethod
    def location(self, key: str) -> str:
        """Human-readable provenance, never interpreted by API/view code."""

    def ingest_tree(self, source: Path, prefix: str) -> None:
        """Publish local CLI output through the blob port (object stores upload here)."""
        for path in source.rglob("*"):
            if path.is_file() and not path.is_symlink():
                key = prefix + "/" + path.relative_to(source).as_posix()
                if self.location(key) != str(path.resolve()):
                    self.write(key, path.read_bytes())

    def materialize(self, prefix: str, target: Path) -> None:
        """Give a local CLI an isolated copy, irrespective of blob backend."""
        for key in self.list_keys(prefix + "/"):
            relative = object_key(key[len(prefix) + 1 :])
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            data = self.read(key)
            if data is not None:
                path.write_bytes(data)

    def json(self, key: str) -> dict:
        try:
            result = json.loads(self.read(key) or b"{}")
            return result if isinstance(result, dict) else {}
        except ValueError:
            return {}

    def save_json(self, key: str, value) -> None:
        self.write(key, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())

    def lines(self, key: str) -> list[dict]:
        rows = []
        for line in (self.read(key) or b"").decode(errors="replace").splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
            except ValueError:
                continue  # A process may still be appending its final JSONL record.
        return rows


class FileArtifactStore(ArtifactStore):
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.mounts = self.json("imports.json").get("mounts", {})

    def _path(self, key: str) -> Path:
        key = object_key(key)
        parts = PurePosixPath(key).parts
        if parts[0] == "_imports":
            if len(parts) < 2 or parts[1] not in getattr(self, "mounts", {}):
                raise ValueError("unknown import mount")
            base = Path(self.mounts[parts[1]])
            candidate = base.joinpath(*parts[2:])
        else:
            base, candidate = self.root, self.root / key
        resolved = candidate.resolve()
        if not resolved.is_relative_to(base.resolve()):
            raise ValueError("artifact escapes storage root")
        return resolved

    def read(self, key):
        object_key(key)
        try:
            return self._path(key).read_bytes()
        except (OSError, ValueError):
            return None

    def write(self, key, data):
        key = object_key(key)
        if key.split("/")[0] == "_imports":
            raise ValueError("imported artifacts are read-only")
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace keeps readers from seeing partial reports/reviews/index files.
        from uuid import uuid4

        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def list_keys(self, prefix=""):
        if prefix:
            prefix = object_key(prefix) + ("/" if prefix.endswith("/") else "")
        keys = []
        for mount, root in [
            ("", self.root),
            *[(f"_imports/{k}/", Path(v)) for k, v in self.mounts.items()],
        ]:
            if root.is_dir():
                for path in root.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        name = mount + path.relative_to(root).as_posix()
                        if name.startswith(prefix):
                            keys.append(name)
        return sorted(set(keys))

    def import_tree(self, source):
        source = source.expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ValueError("import expects directories")
        if source == self.root or source.is_relative_to(self.root):
            return source.relative_to(self.root).as_posix().removeprefix(".")
        ident = identity(str(source))
        self.mounts[ident] = str(source)
        write_json(self.root / "imports.json", {"mounts": self.mounts})
        return f"_imports/{ident}"

    def location(self, key):
        return str(self._path(key))


class MemoryArtifactStore(ArtifactStore):
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.locations: dict[str, str] = {}

    def read(self, key):
        return self.objects.get(object_key(key))

    def write(self, key, data):
        key = object_key(key)
        if key.split("/")[0] == "_imports":
            raise ValueError("imported artifacts are read-only")
        self.objects[key] = bytes(data)

    def list_keys(self, prefix=""):
        if prefix:
            prefix = object_key(prefix) + ("/" if prefix.endswith("/") else "")
        return sorted(k for k in self.objects if k.startswith(prefix))

    def import_tree(self, source):
        source = source.expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ValueError("import expects directories")
        prefix = f"_imports/{identity(str(source))}"
        self.locations[prefix] = str(source)
        for path in source.rglob("*"):
            self.locations[f"{prefix}/{path.relative_to(source).as_posix()}"] = str(path)
            if path.is_file() and not path.is_symlink():
                key = f"{prefix}/{path.relative_to(source).as_posix()}"
                self.objects[key] = path.read_bytes()
                self.locations[key] = str(path)
        return prefix

    def location(self, key):
        key = object_key(key)
        return self.locations.get(key, "memory://" + key)


class EvalCatalog(ABC):
    @abstractmethod
    def faults(self) -> list[dict]: ...

    @abstractmethod
    def expected(self, fault: str) -> dict: ...


class MemoryEvalCatalog(EvalCatalog):
    def __init__(self, expected: dict[str, dict], injectable=()):
        self.definitions, self.injectable = expected, set(injectable)

    def faults(self):
        return [
            {
                "id": name,
                "injectable": name in self.injectable,
                "evaluable": name in self.definitions,
                "version": self.definitions.get(name, {}).get("version", "lab registry"),
            }
            for name in sorted(set(self.definitions) | self.injectable)
        ]

    def expected(self, fault):
        # Return a detached value, never expose mutable catalogue internals.
        return json.loads(json.dumps(self.definitions.get(fault, {})))


class FileEvalCatalog(MemoryEvalCatalog):
    def __init__(self, directory: Path | None = None, injectable=None):
        from incident_investigator.grading.grader import EXPECTED
        from incident_investigator.lab.app import FAULTS
        from incident_investigator.workspace import read_json

        folder = directory if directory is not None else EXPECTED / "faults"
        super().__init__(
            {p.stem: read_json(p) for p in folder.glob("*.json")},
            FAULTS if injectable is None else injectable,
        )


class RunRepository(ABC):
    @abstractmethod
    def refresh(self) -> None: ...

    @abstractmethod
    def runs(self, filters: dict | None = None, sort: str = "created") -> list[dict]: ...

    @abstractmethod
    def get(self, ident: str) -> dict | None: ...

    @abstractmethod
    def suites(self) -> list[dict]: ...

    @abstractmethod
    def import_roots(self, roots: list[Path]) -> None: ...


class IndexedRunRepository(RunRepository):
    """Storage-independent metadata/query layer rebuilt from portable artifacts."""

    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts
        self._runs, self._suites = [], []

    def refresh(self):
        store = self.artifacts
        keys = [
            k
            for k in store.list_keys()
            if "jobs" not in PurePosixPath(k).parts and "reviews" not in PurePosixPath(k).parts
        ]
        suites = {}
        for key in keys:
            if key.endswith(("/batch.json", "/leaderboard.json")):
                prefix = key.rsplit("/", 1)[0]
                suites[prefix] = {
                    "id": identity(store.location(prefix)),
                    "name": store.location(prefix).rstrip("/").split("/")[-1],
                    "prefix": prefix,
                    "batch": store.json(prefix + "/batch.json"),
                    "location": store.location(prefix),
                }
        prefixes = {
            k.rsplit("/", 1)[0]
            for k in keys
            if k.endswith(("/result.json", "/attempt-error.json", "/run.json"))
        }
        overlays = {}
        for key in keys:
            if key.startswith("suites/") and key.endswith("/grade.json"):
                g = store.json(key)
                if g.get("run_id"):
                    overlays[g["run_id"]] = g
        # Resolve overlapping import roots once, using canonical provenance.
        unique = {store.location(prefix): prefix for prefix in sorted(prefixes, reverse=True)}
        rows = []
        for location, prefix in unique.items():
            result = store.json(prefix + "/result.json") or store.json(prefix + "/run.json")
            attempt = store.json(prefix + "/attempt-error.json")
            grade = overlays.get(result.get("run_id"), store.json(prefix + "/grade.json"))
            pointer = store.json(prefix + "/expectation.json")
            events = store.lines(prefix + "/trace.jsonl")
            times = [e["at"] for e in events if isinstance(e.get("at"), int | float)]
            usage = list(result.get("usage", {}).values())
            models = (
                result.get("models")
                or grade.get("models")
                or ({"requested": attempt["model"]} if attempt.get("model") else {})
            )
            suite = next(
                (
                    suites[p]
                    for p in sorted(suites, key=len, reverse=True)
                    if prefix.startswith(p + "/")
                ),
                None,
            )
            created = min(times) if times else result.get("created", 0)
            rows.append(
                {
                    "id": identity(location),
                    "run_id": result.get("run_id", prefix.split("/")[-1]),
                    "launch_id": result.get("launch_id", attempt.get("launch_id")),
                    "prefix": prefix,
                    "location": location,
                    "case": result.get("case_id", "unknown"),
                    "fault": grade.get("family", pointer.get("family", "unknown")),
                    "models": models,
                    "model": ", ".join(sorted(set(models.values()))),
                    "graph": recorded_graph(result.get("graph", grade.get("graph"))),
                    "status": result.get(
                        "status",
                        "failed" if prefix + "/attempt-error.json" in keys else "unfinished",
                    ),
                    "verdict": grade.get("verdict", "ungraded"),
                    "grade": grade,
                    "cost": total(usage, "cost"),
                    "input_tokens": total(usage, "input_tokens"),
                    "output_tokens": total(usage, "output_tokens"),
                    "calls": len(result.get("state", {}).get("calls", {})),
                    "events": len(events),
                    "wall_seconds": grade.get("resources", {}).get(
                        "wall_seconds", max(times) - min(times) if times else None
                    ),
                    "created": created,
                    "created_at": datetime.fromtimestamp(created, UTC).isoformat()
                    if created
                    else "unknown",
                    "suite": suite["id"] if suite else "",
                    "suite_name": suite["name"] if suite else "",
                    "imported": prefix.startswith("_imports/"),
                }
            )
        self._runs, self._suites = rows, list(suites.values())
        store.save_json("index.json", {"runs": rows, "suites": self._suites})

    def runs(self, filters=None, sort="created"):
        rows = [
            r for r in self._runs if all(not v or r.get(k) == v for k, v in (filters or {}).items())
        ]
        if sort not in {"created", "cost", "wall_seconds", "verdict"}:
            sort = "created"
        return sorted(rows, key=lambda r: (r.get(sort) is not None, r.get(sort) or 0), reverse=True)

    def get(self, ident):
        return next((r for r in self._runs if r["id"] == ident), None)

    def suites(self):
        return list(self._suites)

    def import_roots(self, roots):
        for root in roots:
            self.artifacts.import_tree(root)
        self.refresh()


@dataclass
class Repositories:
    runs: RunRepository
    artifacts: ArtifactStore
    catalog: EvalCatalog
    execution_root: Path | None = None


def repositories(
    *, root: Path | None = None, backend: str | None = None, catalog: EvalCatalog | None = None
) -> Repositories:
    selected = backend or os.environ.get("II_STORAGE", "filesystem")
    if selected not in {"filesystem", "memory"}:
        raise ValueError("II_STORAGE must be filesystem or memory")
    store = FileArtifactStore(root or home()) if selected == "filesystem" else MemoryArtifactStore()
    return Repositories(
        IndexedRunRepository(store),
        store,
        catalog or FileEvalCatalog(),
        (root or home()).resolve() if selected == "filesystem" else None,
    )
