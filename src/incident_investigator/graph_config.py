"""Public graph names, defaults and historical artifact labels."""

from argparse import ArgumentParser
from typing import Literal

GraphVariant = Literal["staged", "hub"]
DEFAULT_GRAPH: GraphVariant = "staged"
GRAPH_HELP = (
    "staged (default): stage nodes and conditional edges; "
    "hub: deprecated; removed if final evaluation shows no difference"
)


def add_graph_argument(parser: ArgumentParser) -> None:
    """Give every investigation CLI the same default and deprecation notice."""
    parser.add_argument(
        "--graph", choices=["staged", "hub"], default=DEFAULT_GRAPH, help=GRAPH_HELP
    )


def recorded_graph(value: str | None) -> str:
    """Keep historical run provenance when grading artifacts after the rename."""
    if value is None:
        return "hub"  # Runs predating graph selection used the hub implementation.
    return {"current": "hub", "explicit": "staged"}.get(value, value)
