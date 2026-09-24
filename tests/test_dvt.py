"""
Unit tests for app/integrations/dvt.py's trailing 3-month averaging (added
2026-09-24, stakeholder rule: RDC staffing-norm tier classification must use
the AVERAGE of the last 3 completed calendar months, not a single month's
figure, so one anomalous month doesn't misclassify a plant's tier).

All network calls are mocked via fetch_monthly_volumes — no live DVT calls.
"""
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from app.integrations import dvt


@contextmanager
def _fixed_now(dt):
    """Patch dvt.datetime.utcnow() to return a fixed value (a real datetime,
    so downstream .replace()/arithmetic still works normally)."""
    with patch.object(dvt, "datetime") as mock_datetime:
        mock_datetime.utcnow.return_value = dt
        yield


class TestTrailingMonthStrs:
    def test_returns_last_3_completed_months_most_recent_first(self):
        with _fixed_now(datetime(2026, 9, 24)):
            assert dvt._trailing_month_strs(3) == ["2026-08", "2026-07", "2026-06"]

    def test_handles_year_boundary(self):
        with _fixed_now(datetime(2026, 2, 10)):
            assert dvt._trailing_month_strs(3) == ["2026-01", "2025-12", "2025-11"]

    def test_months_param_respected(self):
        with _fixed_now(datetime(2026, 9, 24)):
            assert dvt._trailing_month_strs(1) == ["2026-08"]


class TestGetAveragePlantVolume:
    def _mock_months(self, by_month):
        """by_month: {month_str: [ {plant_code, volume}, ... ]}"""
        def _fake(month):
            return {"plants": by_month.get(month, [])}
        return patch.object(dvt, "fetch_monthly_volumes", side_effect=_fake)

    def test_averages_across_3_months(self):
        by_month = {
            "2026-08": [{"plant_code": "PX1", "volume": 3000.0}],
            "2026-07": [{"plant_code": "PX1", "volume": 4000.0}],
            "2026-06": [{"plant_code": "PX1", "volume": 5000.0}],
        }
        with _fixed_now(datetime(2026, 9, 24)), self._mock_months(by_month):
            assert dvt.get_average_plant_volume("PX1") == 4000.0

    def test_missing_month_not_counted_as_zero(self):
        """A plant absent from one month (e.g. newly commissioned) must not
        have that month silently treated as 0, dragging the average down."""
        by_month = {
            "2026-08": [{"plant_code": "PX1", "volume": 4000.0}],
            "2026-07": [{"plant_code": "PX1", "volume": 6000.0}],
            "2026-06": [],  # plant not in DVT's export yet this far back
        }
        with _fixed_now(datetime(2026, 9, 24)), self._mock_months(by_month):
            assert dvt.get_average_plant_volume("PX1") == 5000.0  # avg of the 2 real months, not /3

    def test_none_when_plant_never_appears(self):
        with _fixed_now(datetime(2026, 9, 24)), self._mock_months({}):
            assert dvt.get_average_plant_volume("GHOST") is None

    def test_a_single_high_month_does_not_dominate_unfairly(self):
        """Sanity check that this is a real average, not last-month-only —
        directly guards against regressing back to single-month behavior."""
        by_month = {
            "2026-08": [{"plant_code": "PX1", "volume": 100.0}],   # last month: a slow month
            "2026-07": [{"plant_code": "PX1", "volume": 5000.0}],
            "2026-06": [{"plant_code": "PX1", "volume": 5000.0}],
        }
        with _fixed_now(datetime(2026, 9, 24)), self._mock_months(by_month):
            avg = dvt.get_average_plant_volume("PX1")
        assert avg == (100.0 + 5000.0 + 5000.0) / 3
        assert avg != 100.0  # must not just be last month's figure


class TestGetAverageClusterTotalVolume:
    def test_averages_monthly_cluster_totals(self):
        by_month = {
            "2026-08": [{"plant_code": "P1", "volume": 1000.0}, {"plant_code": "P2", "volume": 2000.0}],
            "2026-07": [{"plant_code": "P1", "volume": 1500.0}, {"plant_code": "P2", "volume": 1500.0}],
            "2026-06": [{"plant_code": "P1", "volume": 500.0}, {"plant_code": "P2", "volume": 500.0}],
        }
        def _fake(month):
            return {"plants": by_month.get(month, [])}
        with _fixed_now(datetime(2026, 9, 24)), patch.object(dvt, "fetch_monthly_volumes", side_effect=_fake):
            # monthly totals: 3000, 3000, 1000 -> avg 2333.33
            result = dvt.get_average_cluster_total_volume(["P1", "P2"])
        assert round(result, 2) == round((3000.0 + 3000.0 + 1000.0) / 3, 2)


class TestFetchAllPlantsWithAvgVolume:
    def test_volume_averaged_identity_from_newest_month(self):
        by_month = {
            "2026-08": [{"plant_code": "PX1", "volume": 3000.0, "region": "SOUTH", "erp_name": "New Name"}],
            "2026-07": [{"plant_code": "PX1", "volume": 4000.0, "region": "SOUTH", "erp_name": "Old Name"}],
            "2026-06": [{"plant_code": "PX1", "volume": 5000.0, "region": "SOUTH", "erp_name": "Old Name"}],
        }
        def _fake(month):
            return {"plants": by_month.get(month, [])}
        with _fixed_now(datetime(2026, 9, 24)), patch.object(dvt, "fetch_monthly_volumes", side_effect=_fake):
            result = dvt.fetch_all_plants_with_avg_volume()
        assert len(result) == 1
        row = result[0]
        assert row["plant_code"] == "PX1"
        assert row["volume"] == 4000.0  # avg(3000, 4000, 5000)
        assert row["erp_name"] == "New Name"  # newest month's identity wins

    def test_plant_only_in_older_month_still_included(self):
        by_month = {
            "2026-08": [],
            "2026-07": [{"plant_code": "PX2", "volume": 2000.0}],
            "2026-06": [],
        }
        def _fake(month):
            return {"plants": by_month.get(month, [])}
        with _fixed_now(datetime(2026, 9, 24)), patch.object(dvt, "fetch_monthly_volumes", side_effect=_fake):
            result = dvt.fetch_all_plants_with_avg_volume()
        codes = {p["plant_code"] for p in result}
        assert "PX2" in codes
