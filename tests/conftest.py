"""Shared network doubles for suite execution tests."""

import pytest


@pytest.fixture
def mock_suite_preflight(monkeypatch):
    from incident_investigator.grading import suite

    async def preflight(client, models, catalog, cap, path, *, paid=True):
        return {"usage": await suite.key_usage(client), "message": "Mocked provider preflight"}

    monkeypatch.setattr(suite, "suite_preflight", preflight)
