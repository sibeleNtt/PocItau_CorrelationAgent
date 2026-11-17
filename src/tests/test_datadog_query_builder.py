import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

codes_path = Path(__file__).resolve().parents[1] / "codes"
sys.path.insert(0, str(codes_path))
collector_package = ModuleType("collector")
collector_package.__path__ = [str(codes_path / "collector")]
datadog_stub = ModuleType("collector.datadog_collector")
collector_package.datadog_collector = datadog_stub
sys.modules.setdefault("collector", collector_package)
sys.modules.setdefault("collector.datadog_collector", datadog_stub)

codes_package = ModuleType("codes")
codes_package.__path__ = [str(codes_path)]
sys.modules.setdefault("codes", codes_package)
logs_package = ModuleType("codes.logs")
logs_package.__path__ = []
logger_module = ModuleType("codes.logs.logger")
logger_module.logger = Mock()
services_package = ModuleType("codes.services")
services_package.__path__ = []
aws_module = ModuleType("codes.services.aws")
aws_module.get_secret = Mock()
sys.modules.setdefault("codes.logs", logs_package)
sys.modules.setdefault("codes.logs.logger", logger_module)
sys.modules.setdefault("codes.services", services_package)
sys.modules.setdefault("codes.services.aws", aws_module)

from collector.datadog_query_builder import DatadogQueryBuilder
from codes.collector import datadog_collector
from investigation.health_snapshot import collect_health_snapshots


class DatadogQueryBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = [
            {
                "key": "billing",
                "name": "billing-api",
                "type": "servico",
                "telemetry_query": 'service:"billing-api" env:"prod"',
                "metric_filter": 'service:"billing-api",env:"prod"',
                "metric_names": ["billing_api.latency_ms"],
            },
            {
                "key": "payments",
                "name": "payments-api",
                "type": "servico",
                "telemetry_query": 'service:"payments-api" env:"prod"',
                "metric_filter": 'service:"payments-api",env:"prod"',
                "metric_names": ["payments_api.latency_ms"],
            },
        ]

    def test_builds_combined_logs_and_spans_queries(self) -> None:
        builder = DatadogQueryBuilder(self.targets, "prod")

        self.assertEqual(
            builder.build_logs_query(),
            '(service:"billing-api" env:"prod") OR '
            '(service:"payments-api" env:"prod")',
        )
        self.assertEqual(
            builder.build_spans_query(),
            builder.build_logs_query(),
        )

    def test_builds_metric_queries_by_target(self) -> None:
        builder = DatadogQueryBuilder(self.targets, "prod")

        queries = builder.build_metric_queries()

        self.assertEqual([query["target_key"] for query in queries], [
            "billing",
            "payments",
        ])
        self.assertEqual(
            [query["query"] for query in queries],
            [
                'avg:billing_api.latency_ms{service:"billing-api",env:"prod"}',
                'avg:payments_api.latency_ms{service:"payments-api",env:"prod"}',
            ],
        )
        self.assertEqual(
            [query["query_name"] for query in queries],
            ["metric_0", "metric_1"],
        )

    @patch("investigation.health_snapshot.dd.query_metrics", create=True)
    @patch("investigation.health_snapshot.dd.normalize_spans", create=True)
    @patch("investigation.health_snapshot.dd.search_spans", create=True)
    @patch("investigation.health_snapshot.dd.normalize_logs", create=True)
    @patch("investigation.health_snapshot.dd.search_logs", create=True)
    def test_collects_logs_and_spans_once_and_groups_by_service(
        self,
        search_logs,
        normalize_logs,
        search_spans,
        normalize_spans,
        query_metrics,
    ) -> None:
        search_logs.return_value = {
            "data": [],
            "meta": {"page": {"after": "next-page"}},
        }
        normalize_logs.return_value = [
            {"service": "billing-api", "status": "error", "http_status": 500},
            {"service": "payments-api", "status": "ok", "http_status": 200},
        ]
        search_spans.return_value = {"data": []}
        normalize_spans.return_value = [
            {"service": "billing-api", "error": True, "duration": 12},
            {"service": "payments-api", "error": False, "duration": 8},
        ]
        query_metrics.return_value = {
            "data": {
                "attributes": {
                    "series": [
                        {"query_index": 1, "group_tags": []},
                        {"query_index": 0, "group_tags": []},
                    ],
                    "values": [[7.0], [5.0]],
                },
            },
        }
        window_start = datetime(2026, 11, 12, 9, tzinfo=timezone.utc)
        window_end = datetime(2026, 11, 12, 10, tzinfo=timezone.utc)

        results = collect_health_snapshots(
            self.targets, "prod", window_start, window_end
        )

        self.assertEqual(search_logs.call_count, 1)
        self.assertEqual(search_spans.call_count, 1)
        self.assertEqual(query_metrics.call_count, 1)
        self.assertEqual(results["billing"]["logs"]["errors"], 1)
        self.assertEqual(results["payments"]["logs"]["total"], 1)
        self.assertEqual(results["billing"]["apm"]["errors"], 1)
        self.assertEqual(results["payments"]["metrics"]["payments_api.latency_ms"]["avg"], 7)
        self.assertEqual(results["billing"]["metrics"]["billing_api.latency_ms"]["avg"], 5)
        self.assertIn("resultados adicionais", results["billing"]["logs"]["collection_error"])
        self.assertIn("billing-api", search_logs.call_args.args[2])
        self.assertIn("payments-api", search_logs.call_args.args[2])

    @patch("investigation.health_snapshot.dd.query_metrics", create=True)
    @patch("investigation.health_snapshot.dd.normalize_spans", create=True)
    @patch("investigation.health_snapshot.dd.search_spans", create=True)
    @patch("investigation.health_snapshot.dd.normalize_logs", create=True)
    @patch("investigation.health_snapshot.dd.search_logs", create=True)
    def test_metric_batch_failure_is_reported_for_each_query(
        self,
        search_logs,
        normalize_logs,
        search_spans,
        normalize_spans,
        query_metrics,
    ) -> None:
        search_logs.return_value = {"data": []}
        normalize_logs.return_value = []
        search_spans.return_value = {"data": []}
        normalize_spans.return_value = []
        query_metrics.side_effect = RuntimeError("Datadog unavailable")
        window_start = datetime(2026, 11, 12, 9, tzinfo=timezone.utc)
        window_end = datetime(2026, 11, 12, 10, tzinfo=timezone.utc)

        results = collect_health_snapshots(
            self.targets, "prod", window_start, window_end
        )

        self.assertEqual(query_metrics.call_count, 1)
        self.assertEqual(
            results["billing"]["metrics"]["billing_api.latency_ms"],
            {"available": False, "error": "Datadog unavailable"},
        )
        self.assertEqual(
            results["payments"]["metrics"]["payments_api.latency_ms"],
            {"available": False, "error": "Datadog unavailable"},
        )

    def test_missing_v2_series_is_reported_as_unavailable(self) -> None:
        from investigation.health_snapshot import _metric_points_by_query

        points = _metric_points_by_query(
            {"data": {"attributes": {"series": [], "values": []}}},
            self.targets,
        )

        self.assertEqual(points, {0: [], 1: []})

    def test_rejects_v2_series_without_query_index(self) -> None:
        from investigation.health_snapshot import _metric_points_by_query

        with self.assertRaisesRegex(RuntimeError, "query_index"):
            _metric_points_by_query(
                {
                    "data": {
                        "attributes": {
                            "series": [{"group_tags": []}],
                            "values": [[1.0]],
                        },
                    },
                },
                self.targets,
            )

    @patch.object(datadog_collector, "_request_with_retry")
    @patch.object(datadog_collector, "build_headers", return_value={"test": "headers"})
    def test_metric_collector_posts_v2_batch_in_milliseconds(
        self,
        build_headers,
        request_with_retry,
    ) -> None:
        response = Mock()
        response.json.return_value = {"data": {"attributes": {}}}
        request_with_retry.return_value = response
        start = datetime(2026, 11, 12, 9, tzinfo=timezone.utc)
        end = datetime(2026, 11, 12, 10, tzinfo=timezone.utc)
        queries = [
            {
                "query_name": "metric_0",
                "target_key": "billing",
                "target_name": "billing-api",
                "metric_name": "billing_api.latency_ms",
                "query": 'avg:billing_api.latency_ms{service:billing-api}',
            },
        ]

        datadog_collector.query_metrics(start, end, queries)

        request_with_retry.assert_called_once()
        request, url = request_with_retry.call_args.args
        _, kwargs = request_with_retry.call_args
        self.assertIs(request, datadog_collector.requests.post)
        self.assertEqual(
            url,
            f"https://api.{datadog_collector.DD_SITE}/api/v2/query/timeseries",
        )
        self.assertEqual(
            kwargs["json"],
            {
                "data": {
                    "type": "timeseries_request",
                    "attributes": {
                        "from": int(start.timestamp() * 1000),
                        "to": int(end.timestamp() * 1000),
                        "queries": [
                            {
                                "data_source": "metrics",
                                "name": "metric_0",
                                "query": queries[0]["query"],
                            },
                        ],
                    },
                },
            },
        )
        build_headers.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
