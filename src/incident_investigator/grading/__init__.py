"""Evaluator-only deterministic checks. Never imported by investigation runtime."""

from .grader import grade_run

__all__ = ["grade_run"]
