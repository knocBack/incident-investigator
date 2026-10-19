"""Run the same scripted runtime scenarios against both graph implementations."""

from functools import partial

import pytest

from incident_investigator import cli
from incident_investigator.runtime import investigate


@pytest.fixture(params=["current", "explicit"])
def runtime_graph(request, monkeypatch):
    run = partial(investigate, graph=request.param)
    monkeypatch.setattr(request.module, "investigate", run, raising=False)

    # Demo and CLI tests also exercise the selected graph. Their default argument
    # must not override this fixture's graph selection.
    async def cli_run(*args, **kwargs):
        kwargs["graph"] = request.param
        return await investigate(*args, **kwargs)

    monkeypatch.setattr(cli, "investigate", cli_run)
    return request.param
