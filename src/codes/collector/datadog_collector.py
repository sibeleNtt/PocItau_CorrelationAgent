# datadog_collector
import os
import random
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests
from dotenv import load_dotenv

from ..logs.logger import logger
from ..services.aws import get_secret
from urllib.parse import quote


load_dotenv()

DD_SITE = os.getenv("DD_SITE", "datadoghq.com")

ENV = os.getenv("DD_ENV", "poc")
# Serviço padrão das consultas genéricas (get_recent_logs/get_recent_spans).
# "*" mantém a consulta válida quando não configurado.
SERVICE = os.getenv("DD_SERVICE", "*")
LOOKBACK_MINUTES = int(os.getenv("DD_LOOKBACK_MINUTES", "180"))
LOG_LIMIT = int(os.getenv("DD_LOG_LIMIT", "100"))
SPAN_LIMIT = int(os.getenv("DD_SPAN_LIMIT", "100"))
REQUEST_TIMEOUT = int(os.getenv("DD_REQUEST_TIMEOUT", "30"))
MAX_RATE_LIMIT_RETRIES = 5
CA_BUNDLE = "/opt/ca_bundle.crt"

CUSTOM_METRICS = [
    "latency_ms",
    "error"
]


def _build_env():
    env = os.environ.copy()

    env["DD_API_KEY"] = get_secret("datadog-dd-api-key")
    env["DD_APP_KEY"] = get_secret("datadog-dd-application-key")

    os.environ.update(env)


def build_headers() -> dict[str, str]:
    return {
        "DD-API-KEY": os.environ["DD_API_KEY"],
        "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def build_time_window() -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    return end - timedelta(minutes=LOOKBACK_MINUTES), end


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def get_data_count(response: dict[str, Any]) -> int:
    data = response.get("data", [])
    return len(data) if isinstance(data, list) else 0


def _headers_for_log(headers: Any) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        return {}
    sensitive_fragments = (
        "authorization",
        "auth",
        "api-key",
        "app-key",
        "application-key",
        "token",
        "bearer",
        "jwt",
        "session",
        "secret",
        "cookie",
        "password",
        "credential",
    )
    return {
        str(name): str(value)
        for name, value in headers.items()
        if not any(fragment in str(name).lower() for fragment in sensitive_fragments)
    }


def _request_with_retry(
    request: Callable[..., requests.Response],
    url: str,
    **kwargs: Any,
) -> requests.Response:
    """Repete respostas 429 e registra erros de transporte ou HTTP."""
    try:
        response = request(url, **kwargs)
        for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 1):
            if response.status_code != 429:
                break
            logger.warning(
                f"Erro HTTP 429 na requisição Datadog "
                f"{request.__name__.upper()} {url}; "
                f"response_headers={_headers_for_log(response.headers)}"
            )
            expo = 2 ** attempt
            sleep_time_in_seconds = (expo / 2) + random.uniform(0, expo / 2)
            logger.info(
                f"Retry API Datadog sleep_time_in_seconds: "
                f"{sleep_time_in_seconds} tentativa: {attempt}"
            )
            time.sleep(sleep_time_in_seconds)
            response = request(url, **kwargs)
        response.raise_for_status()
    except requests.RequestException as exc:
        """ Trata e loga erros de transporte e HTTP, registrando cabeçalhos de resposta sensíveis. """
        error_response = getattr(exc, "response", None)
        response_headers = (
            _headers_for_log(error_response.headers)
            if error_response is not None
            else {}
        )
        logger.exception(
            f"Erro na requisição Datadog {request.__name__.upper()} {url}; "
            f"response_headers={response_headers}"
        )
        raise
    return response


def search_logs(
    start: datetime,
    end: datetime,
    query: str,
) -> dict[str, Any]:
    response = _request_with_retry(
        requests.post,
        f"https://api.{DD_SITE}/api/v2/logs/events/search",
        headers=build_headers(),
        json={
            "filter": {
                "from": iso_utc(start),
                "to": iso_utc(end),
                "query": query,
            },
            "sort": "-timestamp",
            "page": {"limit": LOG_LIMIT},
        },
        timeout=REQUEST_TIMEOUT,
        verify=CA_BUNDLE,
    )
    response.raise_for_status()
    return response.json()


def get_recent_logs(
    start: datetime,
    end: datetime,
) -> tuple[dict[str, Any], str]:
    queries = [f"service:{SERVICE} env:{ENV}", f"service:{SERVICE}", "*"]
    last_result: dict[str, Any] = {"data": []}
    for query in queries:
        last_result = search_logs(start, end, query)
        if get_data_count(last_result) > 0:
            return last_result, query
    return last_result, queries[-1]


def search_spans(
    start: datetime,
    end: datetime,
    query: str,
) -> dict[str, Any]:
    response = _request_with_retry(
        requests.get,
        f"https://api.{DD_SITE}/api/v2/spans/events",
        headers=build_headers(),
        params={
            "filter[from]": iso_utc(start),
            "filter[to]": iso_utc(end),
            "filter[query]": query,
            "page[limit]": SPAN_LIMIT,
            "sort": "-timestamp",
        },
        timeout=REQUEST_TIMEOUT,
        verify=CA_BUNDLE,
    )

    response.raise_for_status()
    return response.json()


def get_recent_spans(
    start: datetime,
    end: datetime,
) -> tuple[dict[str, Any], str]:
    queries = [f"service:{SERVICE} env:{ENV}", f"service:{SERVICE}", "*"]
    last_result: dict[str, Any] = {"data": []}
    for query in queries:
        last_result = search_spans(start, end, query)
        if get_data_count(last_result) > 0:
            return last_result, query
    return last_result, queries[-1]


def query_metrics(
    start: datetime,
    end: datetime,
    queries: list[dict[str, str]],
) -> dict[str, Any]:
    if not queries:
        return {"data": {"attributes": {"series": [], "values": []}}}

    response = _request_with_retry(
        requests.post,
        f"https://api.{DD_SITE}/api/v2/query/timeseries",
        headers=build_headers(),
        json={
            "data": {
                "type": "timeseries_request",
                "attributes": {
                    "from": int(start.timestamp() * 1000),
                    "to": int(end.timestamp() * 1000),
                    "queries": [
                        {
                            "data_source": "metrics",
                            "name": item["query_name"],
                            "query": item["query"],
                        }
                        for item in queries
                    ],
                },
            },
        },
        timeout=REQUEST_TIMEOUT,
        verify=CA_BUNDLE,
    )
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Resposta inválida da consulta de métricas v2.")
    return data


def normalize_logs(logs: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = []
    for item in logs.get("data", []):
        attributes = item.get("attributes", {}) or {}
        custom = attributes.get("attributes", {}) or {}
        normalized.append({
            "id": item.get("id"),
            "timestamp": attributes.get("timestamp"),
            "service": attributes.get("service") or custom.get("service"),
            "env": custom.get("env") or attributes.get("env"),
            "status": attributes.get("status") or custom.get("status"),
            "message": attributes.get("message"),
            "http_status": custom.get("http_status"),
            "latency_ms": custom.get("latency_ms"),
            "error_rate": custom.get("error_rate"),
            "endpoint": custom.get("endpoint"),
            "event": custom.get("event"),
            "change_id": custom.get("change_id"),
            "host": attributes.get("host") or custom.get("host"),
            "topic": custom.get("topic"),
            "queue_name": custom.get("queue_name"),
            "aws_account_id": (
                custom.get("aws.account.id")
                or custom.get("aws_account")
                or custom.get("aws_account_id")
            ),
            "trace_id": custom.get("dd.trace_id"),
            "span_id": custom.get("dd.span_id"),
        })
    return normalized


def normalize_spans(spans: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = []
    for item in spans.get("data", []):
        attributes = item.get("attributes", {}) or {}
        custom = attributes.get("attributes", {}) or {}
        normalized.append({
            "id": item.get("id"),
            "timestamp": attributes.get("timestamp"),
            "service": attributes.get("service") or custom.get("service"),
            "env": attributes.get("env") or custom.get("env"),
            "operation_name": (
                attributes.get("operation_name")
                or custom.get("operation_name")
                or custom.get("resource_name")
            ),
            "resource_name": (
                attributes.get("resource_name") or custom.get("resource_name")
            ),
            "duration": attributes.get("duration") or custom.get("duration"),
            "status": attributes.get("status") or custom.get("status"),
            "error": attributes.get("error") or custom.get("error"),
            "trace_id": attributes.get("trace_id") or custom.get("trace_id"),
            "span_id": attributes.get("span_id") or custom.get("span_id"),
            "tags": attributes.get("tags", []) or [],
            "host": attributes.get("host") or custom.get("host"),
            "topic": custom.get("topic"),
            "queue_name": custom.get("queue_name"),
            "aws_account_id": (
                custom.get("aws.account.id")
                or custom.get("aws_account")
                or custom.get("aws_account_id")
            ),
        })
    return normalized

def get_service_dependencies(
    service: str,
    environment: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    end = end or datetime.now(timezone.utc)
    start = start or end - timedelta(hours=1)
    response = _request_with_retry(
        requests.get,
        f"https://api.{DD_SITE}/api/v1/service_dependencies/"
        f"{quote(service, safe='')}",
        headers=build_headers(),
        params={
            "env": environment,
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
        },
        timeout=REQUEST_TIMEOUT,
        verify=CA_BUNDLE,
    )
    response.raise_for_status()
    dependencies = response.json()
    if not isinstance(dependencies, dict):
        raise RuntimeError(
            f"Resposta inválida ao consultar dependências do serviço {service!r}."
        )
    return dependencies