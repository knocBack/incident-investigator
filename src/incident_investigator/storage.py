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

from incident_investigator.graph_config import recorded_graph, recorded_system
from incident_investigator.workspace import home, total, write_json


def object_key(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("invalid artifact key")
    return str(path)


def identity(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def recorded_cost(result):
    cost = total(list(result.get("usage", {}).values()), "cost")
    if cost is not None:
        return cost
    costs = []
    for call_id, call in result.get("state", {}).get("calls", {}).items():
        if call.get("kind") != "model":
            continue
        receipt = result.get("receipts", {}).get(call_id, {})
        # Older local budget refusals predate explicit before_send receipts.
        refused = receipt.get("before_send") is True or receipt.get("error") in {
            "ModelCallError: suite input token limit refused call",
            "ModelCallError: suite budget reservation refused call",
        }
        costs.append({"cost": 0.0 if refused else (receipt.get("usage") or {}).get("cost")})
    return total(costs, "cost")


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
                "case_set": "variants" if name.startswith("reliability-") else "primary",
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
        from incident_investigator.lab.cases import SECOND_CASES, VARIANTS
        from incident_investigator.workspace import read_json

        folder = directory if directory is not None else EXPECTED / "faults"
        definitions = {p.stem: read_json(p) for p in folder.glob("*.json")}
        if directory is None:
            definitions.update({p.stem: read_json(p) for p in (EXPECTED / "cases").glob("*.json")})
        super().__init__(
            definitions, (*FAULTS, *SECOND_CASES, *VARIANTS) if injectable is None else injectable
        )


class RunRepository(ABC):
    @abstractmethod
    def refresh(self) -> None: ...

    @abstractmethod
    def runs(self, filters: dict | None = None, sort: str = "created") -> list[dict]:
        """List unique investigations, or retained evaluation attempts when filtering by suite."""
        ...

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
        self._suite_runs = {}

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

        def graded_at(grade):
            stamp = grade.get("graded_at", 0)
            return stamp if isinstance(stamp, int | float) else 0

        for key in keys:
            if key.startswith("suites/") and key.endswith("/grade.json"):
                g = store.json(key)
                if g.get("run_id") and graded_at(g) >= graded_at(overlays.get(g["run_id"], {})):
                    overlays[g["run_id"]] = g
        suite_prefixes = sorted(suites, key=len, reverse=True)

        def suite_for(prefix):
            return next((suites[p] for p in suite_prefixes if prefix.startswith(p + "/")), None)

        # Prefer suite ancestry, then lexical order, for identical canonical sources.
        unique = {}
        for prefix in sorted(prefixes, key=lambda p: (suite_for(p) is None, p)):
            unique.setdefault(store.location(prefix), prefix)
        rows = []
        for location, prefix in unique.items():
            result = store.json(prefix + "/result.json") or store.json(prefix + "/run.json")
            attempt = store.json(prefix + "/attempt-error.json")
            grade = store.json(prefix + "/grade.json")
            overlay = overlays.get(result.get("run_id"))
            if overlay and graded_at(overlay) >= graded_at(grade):
                grade = overlay
            pointer = store.json(prefix + "/expectation.json")
            phoenix = store.json(prefix + "/phoenix.json")
            events = store.lines(prefix + "/trace.jsonl")
            times = [e["at"] for e in events if isinstance(e.get("at"), int | float)]
            usage = list(result.get("usage", {}).values())
            models = (
                result.get("models")
                or grade.get("models")
                or ({"requested": attempt["model"]} if attempt.get("model") else {})
            )
            suite = suite_for(prefix)
            attempt_context = (
                store.json(
                    suite["prefix"] + "/attempts/" + result.get("run_id", "unknown") + ".json"
                )
                if suite
                else {}
            )
            fault = (
                grade.get("suite_attempt", {}).get("fault")
                or pointer.get("family")
                or attempt_context.get("fault")
                or grade.get("family", "unknown")
            )
            system = recorded_system(
                result.get("system", grade.get("system", attempt_context.get("system"))),
                result.get("graph", grade.get("graph")),
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
                    "fault": fault,
                    "system": system,
                    "case_set": "variants"
                    if grade.get("suite_kind") == "reliability" or fault.startswith("reliability-")
                    else "primary",
                    "suite_set": suite["batch"].get("suite_set", "legacy")
                    if suite
                    else "standalone",
                    "models": models,
                    "phoenix_url": phoenix.get("url"),
                    "model": ", ".join(sorted(set(models.values()))),
                    "graph": recorded_graph(result.get("graph", grade.get("graph"))),
                    "status": result.get(
                        "status",
                        "failed" if prefix + "/attempt-error.json" in keys else "unfinished",
                    ),
                    "verdict": grade.get("verdict", "ungraded"),
                    "grade": grade,
                    "cost": recorded_cost(result),
                    "known_cost": total(usage, "known_cost")
                    if any("known_cost" in u for u in usage)
                    else recorded_cost(result),
                    "known_input_tokens": total(usage, "known_input_tokens"),
                    "known_output_tokens": total(usage, "known_output_tokens"),
                    "model_calls": sum(
                        c.get("kind") == "model"
                        for c in result.get("state", {}).get("calls", {}).values()
                    ),
                    "tool_calls": sum(
                        c.get("kind") == "tool"
                        for c in result.get("state", {}).get("calls", {}).values()
                    ),
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
        # Suite membership belongs to retained evaluation records, independently of
        # where the investigation lives or which grade is newest globally.
        members = {s["id"]: {} for s in suites.values()}
        by_location = {row["location"]: row for row in rows}
        by_run_id = {row["run_id"]: row for row in rows}
        for row in rows:
            if row["suite"]:
                members[row["suite"]][row["location"]] = {
                    **row,
                    "grade": {},
                    "verdict": "ungraded",
                }
        for key in keys:
            if not key.endswith("/grade.json"):
                continue
            prefix = key.rsplit("/", 1)[0]
            suite = suite_for(prefix)
            if suite is None:
                continue
            grade = store.json(key)
            location = store.location(prefix)
            source = by_location.get(location) or by_run_id.get(grade.get("run_id"))
            if source is None and not grade.get("run_id"):
                continue
            models = grade.get("models", (source or {}).get("models", {}))
            resources = grade.get("resources", {})
            totals = resources.get("totals", {})
            attempt = {
                "id": None,
                "run_id": grade.get("run_id", "unknown"),
                "case": "not indexed",
                "created": 0,
                "created_at": "unknown",
                "cost": None,
                "input_tokens": None,
                "output_tokens": None,
                "wall_seconds": None,
                "calls": (totals.get("model_calls") or 0) + (totals.get("tool_calls") or 0),
                "events": 0,
                "status": "unknown",
                **(source or {}),
                "grade": grade,
                "fault": grade.get("suite_attempt", {}).get("fault")
                or (source or {}).get("fault")
                or grade.get("family", "unknown"),
                "verdict": grade.get("verdict", "ungraded"),
                "models": models,
                "model": ", ".join(sorted(set(models.values()))),
                "graph": recorded_graph(grade.get("graph", (source or {}).get("graph"))),
                "system": recorded_system(
                    grade.get("system", (source or {}).get("system")),
                    grade.get("graph", (source or {}).get("graph")),
                ),
                "case_set": "variants"
                if grade.get("suite_kind") == "reliability"
                or grade.get("family", "").startswith("reliability-")
                else "primary",
                "suite_set": suite["batch"].get("suite_set", "legacy"),
                "suite": suite["id"],
                "suite_name": suite["name"],
            }
            for field in (
                "cost",
                "input_tokens",
                "output_tokens",
                "known_cost",
                "known_input_tokens",
                "known_output_tokens",
                "model_calls",
                "tool_calls",
            ):
                if field in totals and (
                    field != "cost" or totals[field] is not None or attempt[field] is None
                ):
                    attempt[field] = totals[field]
            if "wall_seconds" in resources:
                attempt["wall_seconds"] = resources["wall_seconds"]
            if grade.get("process", {}).get("execution_status"):
                attempt["status"] = grade["process"]["execution_status"]
            members[suite["id"]][location] = attempt
        self._runs, self._suites = rows, list(suites.values())
        self._suite_runs = {ident: list(attempts.values()) for ident, attempts in members.items()}
        store.save_json(
            "index.json", {"runs": rows, "suites": self._suites, "suite_runs": self._suite_runs}
        )

    def runs(self, filters=None, sort="created"):
        filters = filters or {}
        source = self._suite_runs.get(filters["suite"], []) if filters.get("suite") else self._runs
        rows = [r for r in source if all(not v or r.get(k) == v for k, v in filters.items())]
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
