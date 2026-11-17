import unittest
from datetime import datetime, timedelta, timezone

from codes.investigation.snapshot_summary import (
    sanitize_entity_results,
    summarize_snapshot,
)
from codes.investigation.windows import matching_reference_window


class MatchingReferenceWindowTests(unittest.TestCase):
    def test_uses_same_hour_on_day_before_gmud(self) -> None:
        implemented_at = datetime(2026, 11, 3, 22, tzinfo=timezone.utc)
        current_start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
        current_end = datetime(2026, 11, 4, 10, tzinfo=timezone.utc)

        reference_start, reference_end = matching_reference_window(
            implemented_at, current_start, current_end
        )

        self.assertEqual(
            reference_start,
            datetime(2026, 11, 2, 9, tzinfo=timezone.utc),
        )
        self.assertEqual(
            reference_end,
            datetime(2026, 11, 2, 10, tzinfo=timezone.utc),
        )

    def test_matches_each_current_hour_to_the_fixed_reference_date(self) -> None:
        implemented_at = datetime(2026, 11, 3, 22, tzinfo=timezone.utc)

        for hour in (8, 9, 10, 23):
            with self.subTest(hour=hour):
                current_start = datetime(2026, 11, 5, hour, tzinfo=timezone.utc)
                current_end = current_start + timedelta(hours=1)

                reference_start, reference_end = matching_reference_window(
                    implemented_at, current_start, current_end
                )

                self.assertEqual(reference_start.date().isoformat(), "2026-11-02")
                self.assertEqual(reference_start.hour, hour)
                self.assertEqual(reference_end - reference_start, timedelta(hours=1))

    def test_preserves_duration_for_windows_crossing_midnight(self) -> None:
        implemented_at = datetime(2026, 11, 3, 22, tzinfo=timezone.utc)
        current_start = datetime(2026, 11, 4, 23, 30, tzinfo=timezone.utc)
        current_end = datetime(2026, 11, 5, 0, 30, tzinfo=timezone.utc)

        reference_start, reference_end = matching_reference_window(
            implemented_at, current_start, current_end
        )

        self.assertEqual(
            reference_start,
            datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            reference_end,
            datetime(2026, 11, 3, 0, 30, tzinfo=timezone.utc),
        )

    def test_summary_excludes_raw_log_and_apm_evidence(self) -> None:
        snapshot = {
            "window": {"from": "a", "to": "b"},
            "metrics": {"latency": {"avg": 10, "latest": 12}},
            "logs": {
                "total": 2,
                "errors": 1,
                "error_ratio": 0.5,
                "collection_error": None,
            },
            "apm": {
                "spans": 3,
                "errors": 1,
                "error_ratio": 0.333333,
                "collection_error": None,
            },
            "evidence": {"logs": [{"message": "raw"}], "apm": [{"trace": "raw"}]},
        }

        summary = summarize_snapshot(snapshot)

        self.assertEqual(summary["metrics"], snapshot["metrics"])
        self.assertEqual(summary["logs"]["error_ratio"], 0.5)
        self.assertEqual(summary["apm"]["spans"], 3)
        self.assertNotIn("evidence", summary)

    def test_sanitizes_legacy_raw_baseline_and_keeps_comparison(self) -> None:
        entity_result = {
            "baseline": {
                "metrics": {"latency": {"avg": 10}},
                "logs": {"total": 4, "errors": 1, "error_ratio": 0.25},
                "apm": {"spans": 2, "errors": 0, "error_ratio": 0},
                "evidence": {"logs": [{"message": "raw"}]},
            },
            "comparison": {"changes": {"latency": {"baseline": 10}}},
        }

        sanitized = sanitize_entity_results([entity_result])[0]

        self.assertNotIn("baseline", sanitized)
        self.assertNotIn("evidence", sanitized["baseline_summary"])
        self.assertEqual(sanitized["comparison"], entity_result["comparison"])
