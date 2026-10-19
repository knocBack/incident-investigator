"""Async HTTP load generator for the local lab."""

from __future__ import annotations

import asyncio

import httpx


async def generate_load(
    target_url: str, rps: float, duration: float, fault: str | None = None
) -> dict[str, int]:
    """Send checkout requests at a steady pace, optionally injecting a fault halfway."""
    if rps <= 0 or duration <= 0:
        raise ValueError("rps and duration must be positive")
    sent = failures = 0
    async with httpx.AsyncClient(base_url=target_url, timeout=5.0) as client:
        start = asyncio.get_running_loop().time()
        injected = False
        while asyncio.get_running_loop().time() - start < duration:
            elapsed = asyncio.get_running_loop().time() - start
            if fault and not injected and elapsed >= duration / 2:
                await client.post("/admin/faults", json={"family": fault})
                injected = True
            response = await client.post("/checkout")
            sent += 1
            failures += response.is_error
            await asyncio.sleep(1 / rps)
    return {"sent": sent, "failures": failures}
