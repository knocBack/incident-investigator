"""Read-only checks before provider-backed evaluation suites."""

import os
import shutil
from pathlib import Path

from incident_investigator.grading.budget import amount
from incident_investigator.models.openrouter import BASE_URL

MIN_FREE_BYTES = 3 * 1024**3


def disk_check(path):
    path = Path(path)
    while not path.exists():
        path = path.parent
    free = shutil.disk_usage(path).free
    if free < MIN_FREE_BYTES:
        raise ValueError(f"Preflight refused: free disk {free / 1024**3:.2f} GiB is below 3 GiB")
    return free


async def key_info(client):
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise ValueError("Preflight: provider API key is missing")
    response = await client.get(f"{BASE_URL}/key", headers={"Authorization": f"Bearer {key}"})
    if response.is_error:
        # Do not leak request headers or private metadata from the key endpoint.
        raise ValueError(f"Preflight: key check failed (HTTP {response.status_code})")
    try:
        data = response.json()["data"]
        if not isinstance(data, dict):
            raise ValueError("invalid key data")
        amount(data["usage"])
        if data.get("limit_remaining") is not None:
            amount(data["limit_remaining"])
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError("Preflight: invalid key usage response") from exc
    return {"usage": data["usage"], "limit_remaining": data.get("limit_remaining")}


async def suite_preflight(client, models, catalog, cap, path, *, paid=True):
    free = disk_check(path)
    missing = set(models) - catalog.keys()
    if missing:
        raise ValueError("Preflight: unknown model(s): " + ", ".join(sorted(missing)))
    data = await key_info(client)
    remaining = data["limit_remaining"]
    warnings = []
    if paid and remaining is not None and amount(remaining) < amount(cap):
        warnings.append(f"Key credit remaining USD {remaining} cannot cover suite cap USD {cap}")
    if paid and remaining is None:
        warnings.append("Key limit is unrestricted; account credit availability is unknown")
    return {
        "key_reachable": True,
        "usage": data["usage"],
        "limit_remaining": remaining,
        "models_present": list(models),
        "free_disk_bytes": free,
        "warnings": warnings,
        "message": f"Key reachable; credit remaining USD {remaining if remaining is not None else 'unknown'}; models present; free disk {free / 1024**3:.2f} GiB"
        + (". Warning: " + "; ".join(warnings) if warnings else ""),
    }
