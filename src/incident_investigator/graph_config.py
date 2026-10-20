"""Public graph names, defaults and historical artifact labels."""

from argparse import ArgumentParser
from typing import Literal

GraphVariant = Literal["staged"]
DEFAULT_GRAPH: GraphVariant = "staged"
GRAPH_HELP = "staged: stage nodes and conditional edges (the only supported workflow)"


def add_graph_argument(parser: ArgumentParser, *, allow_both: bool = False) -> None:
    """Expose the workflow and system choices consistently."""
    parser.add_argument("--graph", choices=["staged"], default=DEFAULT_GRAPH, help=GRAPH_HELP)
    parser.add_argument(
        "--system", choices=["multi", "single", *(["both"] if allow_both else [])], default="multi"
    )


def recorded_graph(value: str | None) -> str:
    """Keep historical run provenance when grading artifacts after the rename."""
    if value is None:
        return "hub"  # Runs predating graph selection used the hub implementation.
    return {"current": "hub", "explicit": "staged"}.get(value, value)


def recorded_system(value: str | None, graph: str | None = None) -> str:
    """Retain the system label of older baseline artifacts with a graph alias."""
    return value if value in {"multi", "single"} else "single" if graph == "single" else "multi"
