"""Additional deterministic fault switches in the three-component laboratory.

These emit raw synthetic application observations, never evaluator expectations.
The lab models resource state; it does not operate external processes or databases.
"""

NEW_FAULTS = {
    "caller_deadline",
    "response_handling",
    "lock_order",
    "memory_growth",
    "retry_amplification",
    "endpoint_address",
    "pool_leak",
}


def activate(name, emit):
    settings = {
        "caller_deadline": ("checkout", "inventory_deadline_ms", 500, 50),
        "response_handling": ("checkout", "response_field", "available", "stock_count"),
        "lock_order": ("inventory", "reservation_path", "single item", "multi item"),
        "memory_growth": ("inventory", "cache_max_entries", 1000, None),
        "retry_amplification": ("checkout", "max_attempts", 1, 5),
        "endpoint_address": ("checkout", "inventory_endpoint", "inventory:8080", "inventroy:8080"),
        "pool_leak": ("inventory", "connection_cleanup", "finally", "success path only"),
    }
    service, key, before, after = settings[name]
    changes = [{"key": key, "before": before, "after": after}]
    if name == "retry_amplification":
        changes.append({"key": "backoff_ms", "before": 100, "after": 0})
    emit(
        "changes",
        dict(
            service=service,
            change_kind="deployment",
            version="v2",
            description=f"{service} v2 release configuration",
            changes=changes,
            attributes={},
        ),
    )


def step(name, index, request_id, log, metric):
    """Model a failing request plus controls; return HTTP status and caller latency."""

    def m(service, metric_name, value, unit, **attrs):
        changed = (
            "checkout"
            if name
            in {"caller_deadline", "response_handling", "retry_amplification", "endpoint_address"}
            else "inventory"
        )
        attrs.setdefault("version", "v2" if service == changed else "v1")
        metric(service, metric_name, value, unit, "gauge", **attrs)

    # Explicit downstream controls distinguish local exhaustion from server limits.
    m("database", "cpu_percent", 22, "%")
    m("database", "connection_count", 24, "connections", connection_limit=100)
    m("database", "lock_wait_ms", 0, "ms")
    m("database", "query_latency", 2, "ms")
    m("checkout", "logical_request_rate", 10, "requests/s")
    if name == "caller_deadline":
        log(
            "inventory",
            "INFO",
            "reservation response completed",
            request_id,
            version="v1",
            duration_ms=100,
            status=200,
        )
        log(
            "checkout",
            "ERROR",
            "Inventory attempt expired at caller deadline",
            request_id,
            version="v2",
            duration_ms=50,
            inventory_deadline_ms=50,
        )
        log(
            "checkout",
            "INFO",
            "older caller completed comparable reservation",
            request_id + "-control",
            version="v1",
            duration_ms=103,
            inventory_deadline_ms=500,
        )
        m("inventory", "request_latency", 100, "ms", baseline_min_ms=80, baseline_max_ms=120)
        return 504, 53
    if name == "response_handling":
        log(
            "inventory",
            "INFO",
            "HTTP 200 response body {available: 99}",
            request_id,
            version="v1",
            status=200,
            response_field="available",
            available=99,
        )
        log(
            "checkout",
            "ERROR",
            "KeyError: stock_count while handling Inventory response",
            request_id,
            version="v2",
            expected_field="stock_count",
            dependency_status=200,
        )
        log(
            "checkout",
            "INFO",
            "v1 read available from same response contract",
            request_id + "-control",
            version="v1",
            status=200,
            response_field="available",
        )
        m("inventory", "request_latency", 3, "ms")
        return 500, 6
    if name == "lock_order":
        # Both transaction sequences and an explicit cycle are available, not inferred.
        for tx, order, version in (("tx-a", "A then B", "v2"), ("tx-b", "B then A", "v1")):
            log(
                "inventory",
                "WARN",
                "stock reservation transaction lock sequence",
                request_id,
                version=version,
                transaction_id=f"{request_id}-{tx}",
                lock_order=order,
            )
        log(
            "database",
            "ERROR",
            "deadlock detected: tx-a holds A waits B; tx-b holds B waits A; victim tx-b",
            request_id,
            transaction_id=f"{request_id}-tx-b",
            blocker=f"{request_id}-tx-a",
            sqlstate="40P01",
        )
        m("inventory", "transaction_rollbacks", 1, "transactions")
        m("inventory", "pool_in_use", 4, "connections", pool_limit=20)
        return 503, 30
    if name == "memory_growth":
        index = (index - 1) % 7 + 1
        # Missing 14:04-14:06 is explicitly a collection gap, never a zero.
        used = {1: 290, 2: 380, 3: 470, 4: 512, 5: 290, 6: 512, 7: 290}[index]
        entries = int(used * 100)
        log(
            "inventory",
            "INFO",
            "cache size sample",
            request_id,
            version="v2",
            cache_entries=entries,
            rss_mib=used,
            memory_limit_mib=512,
        )
        if index not in (2, 3, 4):
            m("inventory", "rss_mib", used, "MiB", memory_limit_mib=512)
        if index == 1:
            log(
                "inventory",
                "WARN",
                "memory sampler unavailable 14:04-14:06; no samples recorded",
                request_id,
                missing_start="2026-01-01T14:04:00Z",
                missing_end="2026-01-01T14:06:00Z",
            )
        if index in (4, 6):
            log(
                "inventory",
                "ERROR",
                "memory limit termination followed by process restart",
                request_id,
                version="v2",
                rss_mib=512,
                memory_limit_mib=512,
                cache_entries=entries,
            )
            if index != 4:
                m("inventory", "rss_mib", 70, "MiB", process_generation=index)
            return 503, 100
        m("inventory", "cache_entries", entries, "entries")
        return 200, 4
    if name == "retry_amplification":
        for attempt in range(1, 6):
            log(
                "checkout",
                "WARN",
                "Inventory attempt exceeded deadline; retry without backoff",
                request_id,
                version="v2",
                logical_request_id=request_id,
                attempt=attempt,
                elapsed_ms=(attempt - 1) * 100,
                deadline_ms=100,
                backoff_ms=0,
            )
        log(
            "inventory",
            "WARN",
            "worker queue saturated",
            request_id,
            active_requests=64,
            worker_limit=64,
            queued_requests=120,
        )
        m("checkout", "inventory_attempt_rate", 50, "attempts/s")
        m("inventory", "active_requests", 64, "requests", worker_limit=64)
        m("inventory", "queued_requests", 120, "requests")
        return 503, 503
    if name == "pool_leak":
        log(
            "inventory",
            "INFO",
            "reservation handler completed",
            f"finished-{index}",
            lease_id=f"lease-{index}",
            request_state="completed",
        )
        log(
            "inventory",
            "INFO",
            "request completed while lease remains owned in later pool snapshot",
            request_id,
            lease_owner=f"finished-{max(1, index - 1)}",
            lease_id=f"lease-{max(1, index - 1)}",
            request_state="completed",
            lease_state="owned",
            pool_in_use=20,
            pool_limit=20,
        )
        log(
            "inventory",
            "ERROR",
            "connection acquisition timed out",
            request_id,
            pool_in_use=20,
            pool_limit=20,
        )
        m("inventory", "pool_in_use", 20, "connections", pool_limit=20)
        m("database", "idle_connections", 20, "connections")
        return 503, 120
    if name == "endpoint_address":
        log(
            "checkout",
            "ERROR",
            "DNS lookup failed for inventroy:8080",
            request_id,
            version="v2",
            host="inventroy:8080",
        )
        log(
            "checkout",
            "INFO",
            "v1 request reached inventory:8080",
            request_id + "-control",
            version="v1",
            status=200,
            host="inventory:8080",
        )
        m("checkout", "error_percent", 60, "%", version="v2", traffic_share_percent=60)
        m("inventory", "received_request_rate", 4, "requests/s", baseline_rate=10)
        return 503, 3
    raise ValueError("unknown scenario")
