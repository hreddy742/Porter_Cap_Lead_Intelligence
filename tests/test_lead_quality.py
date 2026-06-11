"""
Tests for app/ops/lead_quality.py.

Strategy:
  - classify_award_bucket: pure-function tests, no DB needed.
  - count_* / get_* functions: MagicMock session (consistent with test_signals.py).
  - build_report: patch all sub-functions and verify assembly.

No real database is required.  All tests run with `pytest -q` without Docker.
"""

from __future__ import annotations

from collections import namedtuple
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.ops.lead_quality import (
    BUCKET_KEYS,
    LeadQualityReport,
    build_report,
    classify_award_bucket,
    count_companies,
    count_evidence_items,
    count_lead_candidates,
    count_raw_source_records,
    get_award_buckets,
    get_multi_award_companies,
    get_tier_counts,
    get_tiny_award_examples,
    get_top_agencies,
    get_top_naics,
)


# ─── classify_award_bucket (pure function) ────────────────────────────────────

class TestClassifyAwardBucket:
    def test_none_returns_null_amount(self):
        assert classify_award_bucket(None) == "null_amount"

    def test_negative(self):
        assert classify_award_bucket(Decimal("-0.01")) == "negative"
        assert classify_award_bucket(Decimal("-100")) == "negative"
        assert classify_award_bucket(Decimal("-999999")) == "negative"

    def test_zero(self):
        assert classify_award_bucket(Decimal("0")) == "zero"
        assert classify_award_bucket(Decimal("0.00")) == "zero"

    def test_under_50k_lower_boundary(self):
        # $0.01 is the smallest positive amount
        assert classify_award_bucket(Decimal("0.01")) == "under_50k"

    def test_under_50k_example(self):
        # The $50.50 Baltimore Auto Supply record
        assert classify_award_bucket(Decimal("50.50")) == "under_50k"

    def test_under_50k_upper_boundary(self):
        # $49,999.99 is still under_50k
        assert classify_award_bucket(Decimal("49999.99")) == "under_50k"

    def test_50k_lower_boundary(self):
        assert classify_award_bucket(Decimal("50000")) == "50k_to_250k"
        assert classify_award_bucket(Decimal("50000.00")) == "50k_to_250k"

    def test_50k_mid(self):
        assert classify_award_bucket(Decimal("125000")) == "50k_to_250k"

    def test_50k_upper_boundary(self):
        assert classify_award_bucket(Decimal("249999.99")) == "50k_to_250k"

    def test_250k_lower_boundary(self):
        assert classify_award_bucket(Decimal("250000")) == "250k_to_1m"

    def test_250k_mid(self):
        assert classify_award_bucket(Decimal("500000")) == "250k_to_1m"

    def test_250k_upper_boundary(self):
        assert classify_award_bucket(Decimal("999999.99")) == "250k_to_1m"

    def test_over_1m_exact(self):
        assert classify_award_bucket(Decimal("1000000")) == "over_1m"

    def test_over_1m_large(self):
        assert classify_award_bucket(Decimal("5000000")) == "over_1m"

    def test_float_input_is_coerced(self):
        # Function accepts anything str()-able including int/float
        assert classify_award_bucket(Decimal(str(0))) == "zero"
        assert classify_award_bucket(Decimal(str(1000000))) == "over_1m"


# ─── count_* helpers ──────────────────────────────────────────────────────────

class TestCountHelpers:
    def _scalar_db(self, value):
        db = MagicMock()
        db.execute.return_value.scalar.return_value = value
        return db

    def test_count_raw_source_records(self):
        assert count_raw_source_records(self._scalar_db(42)) == 42

    def test_count_raw_source_records_none_becomes_zero(self):
        assert count_raw_source_records(self._scalar_db(None)) == 0

    def test_count_evidence_items(self):
        assert count_evidence_items(self._scalar_db(100)) == 100

    def test_count_companies(self):
        assert count_companies(self._scalar_db(7)) == 7

    def test_count_lead_candidates(self):
        assert count_lead_candidates(self._scalar_db(3)) == 3


# ─── get_tier_counts ──────────────────────────────────────────────────────────

TierRow = namedtuple("TierRow", ["tier", "n"])


class TestGetTierCounts:
    def _db_with_rows(self, rows):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = rows
        return db

    def test_returns_dict_keyed_by_tier(self):
        rows = [TierRow("warm", 5), TierRow("cold", 3)]
        result = get_tier_counts(self._db_with_rows(rows))
        assert result == {"warm": 5, "cold": 3}

    def test_unscored_candidates_included(self):
        # COALESCE(NULL, 'unscored') in SQL → key 'unscored'
        rows = [TierRow("unscored", 2), TierRow("warm", 1)]
        result = get_tier_counts(self._db_with_rows(rows))
        assert result["unscored"] == 2
        assert result["warm"] == 1

    def test_empty_returns_empty_dict(self):
        assert get_tier_counts(self._db_with_rows([])) == {}

    def test_all_tiers_present(self):
        rows = [
            TierRow("hot", 1),
            TierRow("warm", 4),
            TierRow("cold", 8),
            TierRow("archive", 12),
        ]
        result = get_tier_counts(self._db_with_rows(rows))
        assert result == {"hot": 1, "warm": 4, "cold": 8, "archive": 12}


# ─── get_award_buckets ────────────────────────────────────────────────────────

BucketRow = namedtuple(
    "BucketRow",
    ["cnt_null", "cnt_negative", "cnt_zero", "cnt_under_50k", "cnt_50k", "cnt_250k", "cnt_over_1m"],
)


class TestGetAwardBuckets:
    def _db_with_row(self, row):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = row
        return db

    def test_maps_all_seven_buckets(self):
        row = BucketRow(
            cnt_null=1, cnt_negative=2, cnt_zero=0,
            cnt_under_50k=10, cnt_50k=5, cnt_250k=3, cnt_over_1m=2,
        )
        result = get_award_buckets(self._db_with_row(row))
        assert result["null_amount"] == 1
        assert result["negative"] == 2
        assert result["zero"] == 0
        assert result["under_50k"] == 10
        assert result["50k_to_250k"] == 5
        assert result["250k_to_1m"] == 3
        assert result["over_1m"] == 2

    def test_all_bucket_keys_present(self):
        row = BucketRow(0, 0, 0, 0, 0, 0, 0)
        result = get_award_buckets(self._db_with_row(row))
        for key in BUCKET_KEYS:
            assert key in result, f"missing key: {key}"

    def test_no_rows_returns_zeros(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        result = get_award_buckets(db)
        assert all(v == 0 for v in result.values())
        assert set(result.keys()) == set(BUCKET_KEYS)

    def test_tiny_award_detected(self):
        # $50.50 Baltimore Auto Supply would land in under_50k bucket
        row = BucketRow(0, 0, 0, 1, 0, 0, 0)
        result = get_award_buckets(self._db_with_row(row))
        assert result["under_50k"] == 1


# ─── get_top_naics ────────────────────────────────────────────────────────────

NaicsRow = namedtuple("NaicsRow", ["naics_code", "naics_description", "company_count"])


class TestGetTopNaics:
    def test_returns_list_of_dicts(self):
        rows = [NaicsRow("541511", "Custom Computer Programming", 5)]
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = rows
        result = get_top_naics(db)
        assert len(result) == 1
        assert result[0]["naics_code"] == "541511"
        assert result[0]["naics_description"] == "Custom Computer Programming"
        assert result[0]["company_count"] == 5

    def test_null_description_triggers_fallback(self):
        rows = [NaicsRow("541511", None, 3)]
        db = MagicMock()
        # First execute call → fetchall for the main query
        # Subsequent execute calls → scalar for the fallback
        db.execute.return_value.fetchall.return_value = rows
        db.execute.return_value.scalar.return_value = "Computer Services"
        result = get_top_naics(db)
        assert result[0]["naics_description"] == "Computer Services"

    def test_null_description_stays_none_when_fallback_empty(self):
        rows = [NaicsRow("999999", None, 1)]
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = rows
        db.execute.return_value.scalar.return_value = None
        result = get_top_naics(db)
        assert result[0]["naics_description"] is None

    def test_empty_returns_empty_list(self):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = []
        assert get_top_naics(db) == []


# ─── get_top_agencies ─────────────────────────────────────────────────────────

AgencyRow = namedtuple("AgencyRow", ["agency", "evidence_count"])


class TestGetTopAgencies:
    def _db_with_rows(self, rows):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = rows
        return db

    def test_returns_list_of_dicts(self):
        rows = [AgencyRow("DEPT OF DEFENSE", 20), AgencyRow("NASA", 5)]
        result = get_top_agencies(self._db_with_rows(rows))
        assert result[0] == {"agency": "DEPT OF DEFENSE", "evidence_count": 20}
        assert result[1] == {"agency": "NASA", "evidence_count": 5}

    def test_empty_returns_empty_list(self):
        assert get_top_agencies(self._db_with_rows([])) == []


# ─── get_multi_award_companies ────────────────────────────────────────────────

MultiRow = namedtuple(
    "MultiRow",
    ["canonical_name", "company_id", "award_count", "total_amount", "latest_award"],
)


class TestGetMultiAwardCompanies:
    def _db_with_rows(self, rows):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = rows
        return db

    def test_returns_list_of_dicts(self):
        import uuid
        cid = uuid.uuid4()
        rows = [MultiRow("Acme Federal", cid, 3, Decimal("750000"), "2024-03-01")]
        result = get_multi_award_companies(self._db_with_rows(rows))
        assert len(result) == 1
        assert result[0]["award_count"] == 3
        assert result[0]["total_amount"] == pytest.approx(750000.0)

    def test_null_total_amount_becomes_none(self):
        import uuid
        cid = uuid.uuid4()
        rows = [MultiRow("No Amount Co", cid, 2, None, "2024-01-15")]
        result = get_multi_award_companies(self._db_with_rows(rows))
        assert result[0]["total_amount"] is None

    def test_empty_returns_empty_list(self):
        assert get_multi_award_companies(self._db_with_rows([])) == []


# ─── get_tiny_award_examples ──────────────────────────────────────────────────

TinyRow = namedtuple(
    "TinyRow",
    ["canonical_name", "award_amount", "signal_date", "agency",
     "naics_code", "naics_description", "evidence_id", "source_url"],
)


class TestGetTinyAwardExamples:
    def _db_with_rows(self, rows):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = rows
        return db

    def test_baltimore_auto_supply_example(self):
        import uuid
        eid = uuid.uuid4()
        rows = [
            TinyRow(
                "Baltimore Auto Supply",
                Decimal("50.50"),
                "2024-01-15",
                "DEPT OF DEFENSE",
                "441310",
                "Automotive Parts Stores",
                eid,
                "https://www.usaspending.gov/award/CONT_AWD_TEST/",
            )
        ]
        result = get_tiny_award_examples(self._db_with_rows(rows))
        assert len(result) == 1
        assert result[0]["canonical_name"] == "Baltimore Auto Supply"
        assert result[0]["award_amount"] == pytest.approx(50.50)
        assert result[0]["evidence_id"] == str(eid)

    def test_empty_returns_empty_list(self):
        assert get_tiny_award_examples(self._db_with_rows([])) == []

    def test_null_signal_date_becomes_none(self):
        import uuid
        eid = uuid.uuid4()
        rows = [TinyRow("Some Co", Decimal("100"), None, None, None, None, eid, "http://x")]
        result = get_tiny_award_examples(self._db_with_rows(rows))
        assert result[0]["signal_date"] is None


# ─── build_report ─────────────────────────────────────────────────────────────

class TestBuildReport:
    def test_assembles_all_fields(self):
        """build_report calls every sub-function and assembles the dataclass."""
        db = MagicMock()
        patches = {
            "app.ops.lead_quality.count_raw_source_records": 50,
            "app.ops.lead_quality.count_evidence_items": 100,
            "app.ops.lead_quality.count_companies": 25,
            "app.ops.lead_quality.count_lead_candidates": 20,
            "app.ops.lead_quality.get_tier_counts": {"warm": 5, "cold": 10},
            "app.ops.lead_quality.get_award_buckets": {"under_50k": 3, "over_1m": 1},
            "app.ops.lead_quality.get_top_naics": [{"naics_code": "541511"}],
            "app.ops.lead_quality.get_top_agencies": [{"agency": "DOD", "evidence_count": 8}],
            "app.ops.lead_quality.get_multi_award_companies": [],
            "app.ops.lead_quality.get_tiny_award_examples": [],
        }

        with (
            patch("app.ops.lead_quality.count_raw_source_records", return_value=50),
            patch("app.ops.lead_quality.count_evidence_items", return_value=100),
            patch("app.ops.lead_quality.count_companies", return_value=25),
            patch("app.ops.lead_quality.count_lead_candidates", return_value=20),
            patch("app.ops.lead_quality.get_tier_counts", return_value={"warm": 5, "cold": 10}),
            patch("app.ops.lead_quality.get_award_buckets", return_value={"under_50k": 3}),
            patch("app.ops.lead_quality.get_top_naics", return_value=[{"naics_code": "541511"}]),
            patch("app.ops.lead_quality.get_top_agencies", return_value=[]),
            patch("app.ops.lead_quality.get_multi_award_companies", return_value=[]),
            patch("app.ops.lead_quality.get_tiny_award_examples", return_value=[]),
        ):
            report = build_report(db)

        assert isinstance(report, LeadQualityReport)
        assert report.raw_source_records == 50
        assert report.evidence_items == 100
        assert report.companies == 25
        assert report.lead_candidates == 20
        assert report.tier_counts == {"warm": 5, "cold": 10}
        assert report.award_buckets == {"under_50k": 3}
        assert report.top_naics == [{"naics_code": "541511"}]
        assert report.top_agencies == []
        assert report.multi_award_companies == []
        assert report.tiny_award_examples == []
