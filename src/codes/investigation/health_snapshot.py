import hashlib
from datetime import datetime, timezone
from typing import Any
from collector import datadog_collector as dd
from collector.datadog_query_builder import DatadogQueryBuilder


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def snapshot_id(system: str, environment: str, sampled_at: datetime) -> str:
    logical = f"{system}|{environment}|{iso_utc(sampled_at)}"
    return "snapshot-" + hashlib.sha256(logical.encode()).hexdigest()[:32]


def _metric_points_by_query(
    response: dict[str, Any],
    queries: list[dict[str, str]],
) -> dict[int, list[float]]:
    data = response.get("data")
    attributes = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(attributes, dict):
        raise RuntimeError("Resposta v2 sem data.attributes.")

    series = attributes.get("series") or []
    values_by_series = attributes.get("values") or []
    if not isinstance(series, list) or not isinstance(values_by_series, list):
        raise RuntimeError("Resposta v2 de métricas com séries inválidas.")

    points_by_query: dict[int, list[float]] = {
        index: [] for index in range(len(queries))
    }
    for series_index, item in enumerate(series):
        if not isinstance(item, dict):
            raise RuntimeError("Resposta v2 contém metadados de série inválidos.")
        query_index = item.get("query_index")
        if type(query_index) is not int or query_index not in points_by_query:
            raise RuntimeError(
                "Resposta v2 contém query_index fora do lote enviado."
            )
        if series_index >= len(values_by_series):
            raise RuntimeError(
                "Resposta v2 não contém valores para uma série retornada."
            )
        series_values = values_by_series[series_index]
        if not isinstance(series_values, list):
            raise RuntimeError("Resposta v2 contém valores de série inválidos.")
        points_by_query[query_index].extend(
            float(value)
            for value in series_values
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        )
    return points_by_query


def _http_status(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _text(value: Any, limit: int = 500) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _log_evidence(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        logs,
        key=lambda item: (
            str(item.get("status") or "").lower() == "error"
            or _http_status(item.get("http_status")) >= 500
        ),
        reverse=True,
    )
    return [
        {
            "timestamp": item.get("timestamp"),
            "service": item.get("service"),
            "status": item.get("status"),
            "http_status": item.get("http_status"),
            "message": _text(item.get("message")),
            "endpoint": item.get("endpoint"),
            "event": item.get("event"),
            "change_id": item.get("change_id"),
        }
        for item in ordered[:10]
    ]


def _apm_evidence(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        spans,
        key=lambda item: (
            item.get("error") in (True, 1, "1", "true")
            or str(item.get("status") or "").lower() == "error",
            float(item.get("duration") or 0),
        ),
        reverse=True,
    )
    return [
        {
            "timestamp": item.get("timestamp"),
            "service": item.get("service"),
            "operation_name": item.get("operation_name"),
            "resource_name": item.get("resource_name"),
            "duration": item.get("duration"),
            "status": item.get("status"),
            "error": item.get("error"),
            "tags": [_text(tag, 200) for tag in (item.get("tags") or [])[:20]],
        }
        for item in ordered[:10]
    ]


def health_snapshot_from_telemetry_samples(
    system: str,
    environment: str,
    samples: list[dict[str, Any]],
    metric_names: list[str] | None = None,
) -> dict[str, Any]:
    """Reconstrói a saúde usando os recibos das amostras enviadas ao Datadog."""
    if not samples:
        raise ValueError("Nenhuma amostra de telemetria foi fornecida.")
    metric_samples = [
        item for item in samples
        if not any(str(error).startswith("metrics:") for error in item.get("erros", []))
    ]
    if not metric_samples:
        raise ValueError("Nenhuma amostra teve envio de métricas confirmado.")
    latencies = [float(item["latency_ms"]) for item in metric_samples]
    error_rates = [float(item["error_rate"]) for item in metric_samples]
    failed = sum(bool(item.get("request_failed")) for item in metric_samples)
    modeled_error_ratio = round(sum(error_rates) / len(error_rates), 6)
    timestamps = [str(item["observed_at"]) for item in metric_samples]

    def metric(values: list[float]) -> dict[str, Any]:
        return {
            "available": True,
            "count": len(values),
            "avg": round(sum(values) / len(values), 6),
            "min": round(min(values), 6),
            "max": round(max(values), 6),
            "latest": round(values[-1], 6),
        }

    names = metric_names or [
        f"{system}.latency_ms",
        f"{system}.error_rate",
        f"{system}.request",
        f"{system}.error",
        f"{system}.change_marker",
    ]
    metrics = {}
    for name in names:
        if name.endswith(".latency_ms"):
            metrics[name] = metric(latencies)
        elif name.endswith(".error_rate"):
            metrics[name] = metric(error_rates)
        elif name.endswith(".request"):
            metrics[name] = metric([1.0] * len(metric_samples))
        elif name.endswith(".error"):
            metrics[name] = metric([
                1.0 if item.get("request_failed") else 0.0
                for item in metric_samples
            ])
        elif name.endswith(".change_marker"):
            metrics[name] = metric([
                1.0 if item.get("change_event") else 0.0
                for item in metric_samples
            ])

    return {
        "system": system,
        "environment": environment,
        "sampled_at": timestamps[-1],
        "window": {"from": timestamps[0], "to": timestamps[-1]},
        "snapshot_source": "datadog_ingestion_receipts",
        "metrics": metrics,
        "logs": {
            "total": len(metric_samples),
            "errors": failed,
            "error_ratio": modeled_error_ratio,
            "collection_error": None,
        },
        "apm": {
            "spans": len(metric_samples),
            "errors": failed,
            "error_ratio": modeled_error_ratio,
            "collection_error": None,
        },
    }


def _target_matches(
    item: dict[str, Any],
    target: dict[str, Any],
) -> bool:
    target_name = str(target.get("name") or "").casefold()
    entity_type = str(
        target.get("subtype") or target.get("type") or ""
    ).casefold()
    fields = {
        "ec2_instance": "host",
        "kafka_topic": "topic",
        "sqs_queue": "queue_name",
        "aws_account": "aws_account_id",
    }
    field = fields.get(entity_type, "service")
    values = [str(item.get(field) or "").casefold()]
    values.extend(str(tag).casefold() for tag in item.get("tags", []) or [])
    return any(
        value == target_name or value.endswith(f":{target_name}")
        for value in values
    )


def _pagination_warning(response: dict[str, Any], source: str) -> str | None:
    page = (response.get("meta") or {}).get("page") or {}
    if page.get("after"):
        return (
            f"A consulta de {source} atingiu o limite da página; "
            "há resultados adicionais não coletados."
        )
    return None


def collect_health_snapshots(
    targets: list[dict[str, Any]],
    environment: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, dict[str, Any]]:
    """Coleta telemetria para todos os alvos, agrupando logs e spans por janela."""
    if not targets:
        return {}
    query_builder = DatadogQueryBuilder(targets, environment)
    try:
        log_response = dd.search_logs(
            window_start, window_end, query_builder.build_logs_query()
        )
        logs = dd.normalize_logs(
            log_response
        )
        logs_error = _pagination_warning(log_response, "logs")
    except Exception as exc:
        logs = []
        logs_error = str(exc)
    try:
        span_response = dd.search_spans(
            window_start, window_end, query_builder.build_spans_query()
        )
        spans = dd.normalize_spans(
            span_response
        )
        spans_error = _pagination_warning(span_response, "APM")
    except Exception as exc:
        spans = []
        spans_error = str(exc)

    metrics_by_target = {str(target["key"]): {} for target in targets}
    metric_queries = query_builder.build_metric_queries()
    if metric_queries:
        try:
            response = dd.query_metrics(
                window_start, window_end, metric_queries
            )
            points_by_query = _metric_points_by_query(
                response, metric_queries
            )
        except Exception as exc:
            for metric_query in metric_queries:
                metrics_by_target[metric_query["target_key"]][
                    metric_query["metric_name"]
                ] = {"available": False, "error": str(exc)}
        else:
            for query_index, metric_query in enumerate(metric_queries):
                values = points_by_query[query_index]
                target_key = metric_query["target_key"]
                name = metric_query["metric_name"]
                metrics_by_target[target_key][name] = {
                    "available": bool(values),
                    "count": len(values),
                    "avg": (
                        round(sum(values) / len(values), 6) if values else None
                    ),
                    "min": round(min(values), 6) if values else None,
                    "max": round(max(values), 6) if values else None,
                    "latest": round(values[-1], 6) if values else None,
                }

    snapshots = {}
    for target in targets:
        key = str(target["key"])
        system_logs = [item for item in logs if _target_matches(item, target)]
        system_spans = [item for item in spans if _target_matches(item, target)]
        log_errors = sum(
            str(item.get("status") or "").lower() == "error"
            or _http_status(item.get("http_status")) >= 500
            for item in system_logs
        )
        span_errors = sum(
            item.get("error") in (True, 1, "1", "true")
            or str(item.get("status") or "").lower() == "error"
            for item in system_spans
        )
        snapshots[key] = {
            "system": target["name"],
            "environment": environment,
            "sampled_at": iso_utc(window_end),
            "window": {
                "from": iso_utc(window_start),
                "to": iso_utc(window_end),
            },
            "metrics": metrics_by_target[key],
            "logs": {
                "total": len(system_logs),
                "errors": log_errors,
                "error_ratio": (
                    round(log_errors / len(system_logs), 6)
                    if system_logs else None
                ),
                "collection_error": logs_error,
            },
            "apm": {
                "spans": len(system_spans),
                "errors": span_errors,
                "error_ratio": (
                    round(span_errors / len(system_spans), 6)
                    if system_spans else None
                ),
                "collection_error": spans_error,
            },
            "evidence": {
                "logs": _log_evidence(system_logs),
                "apm": _apm_evidence(system_spans),
            },
        }
    return snapshots


def collect_health_snapshot(
    system: str,
    environment: str,
    window_start: datetime,
    window_end: datetime,
    telemetry_query: str | None = None,
    metric_filter: str | None = None,
    metric_names: list[str] | None = None,
) -> dict[str, Any]:
    """Compatibilidade para coleta individual de uma entidade."""
    target = {
        "key": system,
        "name": system,
        "telemetry_query": telemetry_query,
        "metric_filter": metric_filter,
        "metric_names": metric_names or [],
    }
    return collect_health_snapshots(
        [target], environment, window_start, window_end
    )[system]
