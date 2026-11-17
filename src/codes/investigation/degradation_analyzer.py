import os
from typing import Any


LATENCY_DELTA_PERCENT = float(os.getenv("DEGRADATION_LATENCY_PERCENT", "50"))
ERROR_RATE_DELTA_ABSOLUTE = float(os.getenv("DEGRADATION_ERROR_RATE_DELTA", "0.03"))


def _delta_percent(before: Any, current: Any) -> float | None:
    if not isinstance(before, (int, float)) or not isinstance(current, (int, float)):
        return None
    if before == 0:
        return None
    return round((current - before) / abs(before) * 100, 2)


def compare_snapshot(
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    changes = {}
    affected = []
    score = 0.0
    observable_sources = []
    for name, base_metric in baseline.get("metrics", {}).items():
        current_metric = current.get("metrics", {}).get(name, {})
        before = base_metric.get("avg")
        after = current_metric.get("avg")
        delta = _delta_percent(before, after)
        changes[name] = {"baseline": before, "current": after, "delta_percent": delta}
        if delta is None:
            continue
        observable_sources.append(name)
        is_latency = "latency" in name and delta >= LATENCY_DELTA_PERCENT
        error_delta = (
            after - before
            if "error_rate" in name
            and isinstance(before, (int, float))
            and isinstance(after, (int, float))
            else 0
        )
        is_error = "error_rate" in name and error_delta >= ERROR_RATE_DELTA_ABSOLUTE
        if is_latency or is_error:
            affected.append(name)
            score = max(score, abs(delta))

    for source in ("logs", "apm"):
        before = baseline.get(source, {}).get("error_ratio")
        after = current.get(source, {}).get("error_ratio")
        delta = None
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            observable_sources.append(f"{source}.error_ratio")
            delta = round(after - before, 6)
            if delta >= ERROR_RATE_DELTA_ABSOLUTE:
                affected.append(f"{source}.error_ratio")
                score = max(score, delta * 100)
        changes[f"{source}.error_ratio"] = {
            "baseline": before,
            "current": after,
            "delta_absolute": delta,
        }
    return {
        "data_available": bool(observable_sources),
        "observable_sources": sorted(set(observable_sources)),
        "degraded": bool(affected),
        "severity_score": round(score, 2),
        "affected_metrics": sorted(set(affected)),
        "changes": changes,
    }


def analyze_trajectory(samples: list[dict[str, Any]]) -> dict[str, Any]:
    observable = [
        sample for sample in samples
        if sample.get("comparison", {}).get("data_available")
    ]
    degraded = [sample for sample in samples if sample.get("comparison", {}).get("degraded")]
    worst = max(
        degraded,
        key=lambda item: item.get("comparison", {}).get("severity_score", 0),
        default=None,
    )
    last_three = samples[-3:]
    persistent = bool(last_three) and sum(
        bool(item.get("comparison", {}).get("degraded")) for item in last_three
    ) >= max(1, len(last_three) - 1)
    recovered = bool(degraded) and bool(samples) and not samples[-1].get(
        "comparison", {}
    ).get("degraded")
    affected = sorted({
        metric
        for sample in degraded
        for metric in sample.get("comparison", {}).get("affected_metrics", [])
    })
    return {
        "conclusion": (
            "degraded" if degraded else "healthy" if observable else "inconclusive"
        ),
        "observable_steps": [item.get("step") for item in observable],
        "degradation_detected": bool(degraded),
        "degradation_start": degraded[0].get("sampled_at") if degraded else None,
        "first_degraded_step": degraded[0].get("step") if degraded else None,
        "degraded_steps": [item.get("step") for item in degraded],
        "worst_step": worst.get("step") if worst else None,
        "worst_severity_score": (
            worst.get("comparison", {}).get("severity_score") if worst else 0
        ),
        "affected_metrics": affected,
        "persistent": persistent,
        "recovered": recovered,
    }
