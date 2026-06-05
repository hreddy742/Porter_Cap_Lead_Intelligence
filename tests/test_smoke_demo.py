"""
Unit tests for scripts/smoke_demo.py.

All tests here are pure — they require no database, no Docker, and no network.
They verify static properties of the smoke demo module:

  1. DEMO_PAYLOAD contains all required USASpending fields.
  2. DEMO_CSV_PATH is under exports/.
  3. DEMO_REVIEWER_ID is a fixed, non-user-supplied constant.
  4. Importing the module does not auto-execute main() (no DB connection on import).
  5. The module contains no external API or Salesforce calls.
"""
from __future__ import annotations

import inspect


# Import the module under test.
# If this import causes a database connection attempt, the test will fail here
# before any test function runs — which is itself a useful signal.
import scripts.smoke_demo as smoke_demo


# ── 1. Payload shape ──────────────────────────────────────────────────────────

class TestDemoPayload:
    """DEMO_PAYLOAD must contain every field the evidence extractor reads."""

    REQUIRED_FIELDS = [
        "Award ID",
        "Recipient Name",
        "Recipient UEI",
        "Start Date",
        "Award Amount",
        "NAICS Code",
        "NAICS Description",
        "Place of Performance State Code",
        "Award Type",
        "Awarding Agency",
    ]

    def test_all_required_usaspending_fields_present(self):
        for field in self.REQUIRED_FIELDS:
            assert field in smoke_demo.DEMO_PAYLOAD, (
                f"DEMO_PAYLOAD is missing required field: {field!r}"
            )

    def test_recipient_name_contains_demo_marker(self):
        """Company name must make demo records easy to identify in the DB."""
        assert "Demo" in smoke_demo.DEMO_PAYLOAD["Recipient Name"]

    def test_award_amount_is_positive_number(self):
        amount = smoke_demo.DEMO_PAYLOAD["Award Amount"]
        assert isinstance(amount, (int, float))
        assert amount > 0

    def test_start_date_parses_as_iso_date(self):
        """evidence extractor calls date.fromisoformat() — must not raise."""
        from datetime import date
        date.fromisoformat(smoke_demo.DEMO_PAYLOAD["Start Date"])

    def test_uei_constant_matches_payload(self):
        assert smoke_demo.DEMO_PAYLOAD["Recipient UEI"] == smoke_demo.DEMO_UEI

    def test_award_id_constant_matches_payload(self):
        assert smoke_demo.DEMO_PAYLOAD["Award ID"] == smoke_demo.DEMO_SOURCE_RECORD_ID

    def test_payload_is_a_dict(self):
        assert isinstance(smoke_demo.DEMO_PAYLOAD, dict)
        assert len(smoke_demo.DEMO_PAYLOAD) >= len(self.REQUIRED_FIELDS)


# ── 2. CSV output path ────────────────────────────────────────────────────────

class TestDemoCsvPath:
    def test_csv_path_starts_with_exports_slash(self):
        assert smoke_demo.DEMO_CSV_PATH.startswith("exports/"), (
            f"DEMO_CSV_PATH must be under exports/, got: {smoke_demo.DEMO_CSV_PATH!r}"
        )

    def test_csv_path_ends_with_dot_csv(self):
        assert smoke_demo.DEMO_CSV_PATH.endswith(".csv")

    def test_csv_path_contains_demo_marker(self):
        assert "demo" in smoke_demo.DEMO_CSV_PATH.lower()


# ── 3. Reviewer identity is a fixed constant ──────────────────────────────────

class TestDemoReviewerId:
    def test_reviewer_id_is_a_non_empty_string(self):
        reviewer = smoke_demo.DEMO_REVIEWER_ID
        assert isinstance(reviewer, str)
        assert reviewer.strip() != ""

    def test_reviewer_id_looks_like_an_email(self):
        """reviewer_id is stored in the DB; must be email-shaped for traceability."""
        assert "@" in smoke_demo.DEMO_REVIEWER_ID

    def test_reviewer_id_contains_portercapital_domain(self):
        """Must use the .local placeholder domain, not a real user address."""
        assert "portercapital" in smoke_demo.DEMO_REVIEWER_ID

    def test_reviewer_id_is_not_empty_after_strip(self):
        assert len(smoke_demo.DEMO_REVIEWER_ID.strip()) > 0


# ── 4. Import does not auto-execute main() ────────────────────────────────────

class TestModuleStructure:
    def test_import_succeeded_without_db(self):
        """If we reach this line the import did not attempt a DB connection."""
        assert smoke_demo is not None

    def test_main_is_callable(self):
        """main() must be a callable function, not called at import time."""
        assert callable(smoke_demo.main)

    def test_module_has_all_expected_constants(self):
        required_attrs = [
            "DEMO_SOURCE_NAME",
            "DEMO_SOURCE_RECORD_ID",
            "DEMO_REVIEWER_ID",
            "DEMO_CSV_PATH",
            "DEMO_UEI",
            "DEMO_PAYLOAD",
            "main",
        ]
        for attr in required_attrs:
            assert hasattr(smoke_demo, attr), (
                f"smoke_demo is missing expected attribute: {attr!r}"
            )

    def test_demo_source_record_id_is_a_literal_string(self):
        """Must be a deterministic constant, not computed at runtime."""
        assert isinstance(smoke_demo.DEMO_SOURCE_RECORD_ID, str)
        assert smoke_demo.DEMO_SOURCE_RECORD_ID.strip() != ""

    def test_demo_uei_is_a_string(self):
        assert isinstance(smoke_demo.DEMO_UEI, str)
        assert smoke_demo.DEMO_UEI.strip() != ""


# ── 5. No external API or Salesforce calls ────────────────────────────────────

class TestNoExternalCalls:
    """
    Verify the module source code contains no patterns that would trigger
    real network calls to USASpending or Salesforce.
    """

    @property
    def _source(self) -> str:
        return inspect.getsource(smoke_demo)

    def test_no_usaspending_api_endpoint_called(self):
        assert "api.usaspending.gov/api" not in self._source

    def test_no_salesforce_call(self):
        assert "salesforce.com" not in self._source.lower()

    def test_no_httpx_get_call(self):
        assert "httpx.get(" not in self._source
        assert "httpx.post(" not in self._source

    def test_no_requests_get_call(self):
        assert "requests.get(" not in self._source
        assert "requests.post(" not in self._source

    def test_usaspending_connector_not_imported(self):
        """smoke_demo must not import the real USASpendingConnector."""
        assert "USASpendingConnector" not in self._source
