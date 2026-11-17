from collections.abc import Iterable
from typing import Any


class DatadogQueryBuilder:
    def __init__(
        self,
        targets: Iterable[dict[str, Any]],
        environment: str,
    ) -> None:
        self._targets = list(targets)
        self._environment = environment

    def build_logs_query(self) -> str:
        return self._build_telemetry_query()

    def build_spans_query(self) -> str:
        return self._build_telemetry_query()

    def build_metric_queries(self) -> list[dict[str, str]]:
        queries = []
        seen = set()
        for target in self._targets:
            target_key = str(target.get("key") or target.get("name") or "")
            metric_filter = str(
                target.get("metric_filter")
                or (
                    f"service:{target.get('name')},"
                    f"env:{self._environment}"
                )
            )
            for metric_name in target.get("metric_names") or []:
                query = f"avg:{metric_name}{{{metric_filter}}}"
                identity = (target_key, str(metric_name), query)
                if identity in seen:
                    continue
                seen.add(identity)
                queries.append({
                    "query_name": f"metric_{len(queries)}",
                    "target_key": target_key,
                    "target_name": str(target.get("name") or ""),
                    "metric_name": str(metric_name),
                    "query": query,
                })
        return queries

    def _build_telemetry_query(self) -> str:
        queries = []
        seen = set()
        for target in self._targets:
            query = str(
                target.get("telemetry_query")
                or (
                    f"service:{target.get('name')} "
                    f"env:{self._environment}"
                )
            ).strip()
            if query and query not in seen:
                seen.add(query)
                queries.append(f"({query})")
        return " OR ".join(queries) if queries else "*"
