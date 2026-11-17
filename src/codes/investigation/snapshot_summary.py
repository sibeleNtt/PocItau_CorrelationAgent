from typing import Any


def summarize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "window": snapshot.get("window"),
        "metrics": snapshot.get("metrics", {}),
        "logs": {
            key: snapshot.get("logs", {}).get(key)
            for key in ("total", "errors", "error_ratio", "collection_error")
        },
        "apm": {
            key: snapshot.get("apm", {}).get(key)
            for key in ("spans", "errors", "error_ratio", "collection_error")
        },
    }


def sanitize_entity_results(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sanitized = []
    for item in results:
        result = dict(item)
        baseline = result.pop("baseline", None)
        if isinstance(baseline, dict):
            result["baseline_summary"] = summarize_snapshot(baseline)
        sanitized.append(result)
    return sanitized
