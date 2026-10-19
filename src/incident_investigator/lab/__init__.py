"""Local Checkout → Inventory → Database telemetry laboratory."""

from .app import TelemetrySink, create_app

__all__ = ["TelemetrySink", "create_app"]
