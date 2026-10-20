"""Estimate initial-call reservations locally; only public price metadata is fetched."""

import asyncio
from argparse import Namespace
from pathlib import Path
from tempfile import gettempdir
from time import monotonic

import httpx

from incident_investigator.grading.budget import amount, estimated_input_tokens, price_bound
from incident_investigator.models import OpenRouterModel, ScriptedModel
from incident_investigator.runtime import Control, Investigation, model_input


class LaunchPreflight:
    def __init__(self):
        self._catalog = {}
        self._loaded = float("-inf")
        self._lock = asyncio.Lock()
        self._snapshots = {}

    async def catalogue(self):
        async with self._lock:
            if monotonic() - self._loaded > 300:
                async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                    response = await client.get("https://openrouter.ai/api/v1/models")
                    response.raise_for_status()
                    self._catalog = {m["id"]: m for m in response.json()["data"]}
                    self._loaded = monotonic()
        return self._catalog

    async def check(self, request, repo):
        if request.provider != "openrouter" or request.command == "eval":
            return {
                "launch_allowed": True,
                "estimated_call_reservation": None,
                "message": "",
                "blocked_fields": [],
            }
        from incident_investigator import cli

        try:
            info = (await self.catalogue())[request.model_id]
            cases = request.faults or [
                f["id"]
                for f in repo.catalog.faults()
                if f["injectable"]
                and f["evaluable"]
                and (request.case_set == "all" or f.get("case_set", "primary") == request.case_set)
            ]
            limits = cli.profile(Namespace(model="openrouter", **request.model_dump()))
            estimates = []
            for case in cases:
                if case not in self._snapshots:
                    self._snapshots[case] = await cli.demo(
                        Namespace(fault=case, _freeze_only=True, _fixed_clock=True)
                    )
                snapshot = self._snapshots[case]
                for system in ["multi", "single"] if request.system == "both" else [request.system]:
                    runner = Investigation(
                        snapshot,
                        ScriptedModel(),
                        Path(gettempdir()) / "ii-console-preflight",
                        limits,
                        asyncio.Semaphore(1),
                        system=system,
                    )
                    control = Control(
                        run_id="preflight", case_id=snapshot.case_id, limits=limits, system=system
                    )
                    (runner.start_single if system == "single" else runner.plan)(control)
                    runner.schedule(control)
                    if not control.dispatch:
                        raise ValueError(
                            "Raise Run wall limit: it expired while preparing the first call."
                        )
                    call = control.calls[control.dispatch[0]]
                    messages, schema = model_input(call.role, call.request)
                    rendered = OpenRouterModel.build_request(
                        messages,
                        schema,
                        request.model_id,
                        structured="structured_outputs" in info.get("supported_parameters", []),
                    )
                    tokens = estimated_input_tokens(rendered)
                    if tokens > request.max_input_tokens:
                        raise ValueError(
                            f"Raise Max input tokens to at least {tokens} for the initial call."
                        )
                    estimates.append(
                        price_bound(
                            info, max_input_tokens=tokens, output_tokens=rendered["max_tokens"]
                        )
                    )
            bound = max(estimates)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "Cannot check the model's public prices. Check the model id and retry."
            )
            fields = (
                ["max_input_tokens"]
                if message.startswith("Raise Max input tokens")
                else ["wall_seconds"]
                if message.startswith("Raise Run wall limit")
                else ["model_id"]
            )
            return {
                "launch_allowed": False,
                "estimated_call_reservation": None,
                "message": message,
                "blocked_fields": fields,
            }
        blocked = []
        if amount(request.budget_usd) < bound:
            blocked.append("Suite cap USD")
        if request.run_budget_usd is not None and amount(request.run_budget_usd) < bound:
            blocked.append("Per-run cap USD")
        suggested = bound.quantize(amount("0.01"), rounding="ROUND_CEILING")
        run_guide = (bound * limits.max_model_calls).quantize(
            amount("0.01"), rounding="ROUND_CEILING"
        )
        return {
            "launch_allowed": not blocked,
            "estimated_call_reservation": str(bound),
            "minimum_cap": str(suggested),
            "max_model_calls": limits.max_model_calls,
            "rough_run_reservation": str(run_guide),
            "free_model": bound == 0,
            "blocked_fields": (["budget_usd"] if "Suite cap USD" in blocked else [])
            + (["run_budget_usd"] if "Per-run cap USD" in blocked else []),
            "reservation_guide": (
                "Free model: public prices are zero; no charge is reserved. Usage may still be unavailable after a provider failure."
                if bound == 0
                else f"Each initial call reserves about USD {bound:.4f}, with a minimum cap of USD {suggested:.2f} for one call. At this initial size, up to {limits.max_model_calls} model calls would reserve USD {run_guide:.2f}: an illustrative reservation ceiling, not expected spend or a required cap. Later prompts can be larger; a lower cap stops calls when reservations no longer fit."
            ),
            "message": f"Raise {' and '.join(blocked)} to at least USD {suggested:.2f} to fit an initial call. Budget for several calls per run."
            if blocked
            else "Initial calls fit the caps. Later calls may need larger reservations.",
        }
