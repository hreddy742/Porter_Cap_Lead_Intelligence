"""
Tests for app/processing/signals.py — detect_signals_for_evidence().

All tests use mocked sessions (MagicMock). No real database required.

Signal strength thresholds:
  >= 1_000_000 → "strong"
  >= 250_000   → "medium"
  < 250_000    → "weak"
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.db.models import EvidenceItem, Signal
from app.processing.signals import classify_signal_strength, detect_signals_for_evidence


# ─── Shared helpers ───────────────────────────────────────────────────────────


def _make_evidence(
    *,
    claim_supported: str = "CONTRACT_AWARD",
    freshness_score: Decimal = Decimal("0.75"),
    action_date: str | None = None,
    award_amount: str | None = "500000.00",
) -> MagicMock:
    ev = MagicMock(spec=EvidenceItem)
    ev.id = uuid.uuid4()
    ev.company_id = uuid.uuid4()
    ev.source_id = uuid.uuid4()
    ev.claim_supported = claim_supported
    ev.freshness_score = freshness_score
    ev.source_url = "https://www.usaspending.gov/award/CONT_AWD_12345/"
    ev.extracted_fields = {
        "action_date": action_date if action_date is not None else date.today().isoformat(),
        "award_amount": award_amount,
        "company_name": "Acme Federal Services",
    }
    return ev


def _make_session(
    *,
    evidence: MagicMock | None = None,
    existing_signals: list | None = None,
) -> MagicMock:
    added: list = []

    def _add(obj):
        added.append(obj)
        try:
            if obj.id is None:
                obj.id = uuid.uuid4()
        except Exception:
            pass

    def _get(cls, pk):
        if cls is EvidenceItem:
            return evidence
        return None

    def _execute(stmt):
        result = MagicMock()
        result.scalars.return_value.all.return_value = existing_signals or []
        return result

    s = MagicMock()
    s.get.side_effect = _get
    s.add.side_effect = _add
    s.execute.side_effect = _execute
    s._added = added
    return s


# ─── Test 1: CONTRACT_AWARD with company_id creates one signal ────────────────


def test_contract_award_creates_one_signal():
    """CONTRACT_AWARD evidence with company_id set creates exactly one Signal."""
    ev = _make_evidence()
    db = _make_session(evidence=ev)

    result = detect_signals_for_evidence(ev.id, db)

    assert len(result) == 1, "must return exactly one Signal"
    signals = [o for o in db._added if isinstance(o, Signal)]
    assert len(signals) == 1, "must add exactly one Signal to the session"


# ─── Test 2: signal has non-null evidence_id ─────────────────────────────────


def test_signal_has_non_null_evidence_id():
    """The created Signal must have evidence_id equal to the source evidence row's id."""
    ev = _make_evidence()
    db = _make_session(evidence=ev)

    result = detect_signals_for_evidence(ev.id, db)

    assert len(result) == 1
    signal = result[0]
    assert signal.evidence_id is not None, "evidence_id must not be NULL"
    assert signal.evidence_id == ev.id, "evidence_id must match the source evidence row"


# ─── Test 3: company_id NULL returns [] and creates no signal ─────────────────


def test_company_id_null_returns_empty_list():
    """Evidence where company_id is NULL returns [] — resolution must run first."""
    ev = _make_evidence()
    ev.company_id = None
    db = _make_session(evidence=ev)

    result = detect_signals_for_evidence(ev.id, db)

    assert result == [], "must return [] when company_id is NULL"
    signals = [o for o in db._added if isinstance(o, Signal)]
    assert len(signals) == 0, "must not add any Signal when company_id is NULL"


# ─── Test 4: nonexistent evidence returns [] ─────────────────────────────────


def test_nonexistent_evidence_returns_empty_list():
    """A nonexistent evidence_id returns [] without crashing."""
    db = _make_session(evidence=None)

    result = detect_signals_for_evidence(uuid.uuid4(), db)

    assert result == [], "must return [] for nonexistent evidence"
    signals = [o for o in db._added if isinstance(o, Signal)]
    assert len(signals) == 0


# ─── Test 5: wrong claim_supported returns [] ────────────────────────────────


def test_non_contract_award_claim_returns_empty_list():
    """Evidence with claim_supported != CONTRACT_AWARD returns [] — no signal created."""
    ev = _make_evidence(claim_supported="GRANT_AWARD")
    db = _make_session(evidence=ev)

    result = detect_signals_for_evidence(ev.id, db)

    assert result == [], "must return [] for non-CONTRACT_AWARD claims"
    signals = [o for o in db._added if isinstance(o, Signal)]
    assert len(signals) == 0


# ─── Test 6: missing or bad action_date returns [] ───────────────────────────


@pytest.mark.parametrize("bad_date", [None, "", "not-a-date", "2024-13-99", 12345])
def test_bad_action_date_returns_empty_list(bad_date):
    """Missing or unparseable action_date quarantines the record without crashing."""
    ev = _make_evidence()
    ev.extracted_fields["action_date"] = bad_date
    db = _make_session(evidence=ev)

    result = detect_signals_for_evidence(ev.id, db)

    assert result == [], f"action_date={bad_date!r} must return []"
    signals = [o for o in db._added if isinstance(o, Signal)]
    assert len(signals) == 0


# ─── Test 7: signal freshness_score equals evidence.freshness_score ──────────


def test_signal_freshness_matches_evidence():
    """Signal.freshness_score must equal evidence_items.freshness_score."""
    ev = _make_evidence(freshness_score=Decimal("0.65"))
    db = _make_session(evidence=ev)

    result = detect_signals_for_evidence(ev.id, db)

    assert len(result) == 1
    assert result[0].freshness_score == Decimal("0.65")


# ─── Tests 8–10: signal_strength via classify_signal_strength helper ──────────


@pytest.mark.parametrize("amount, expected_strength", [
    ("1000000", "strong"),
    ("2500000", "strong"),
    ("999999.99", "medium"),
    ("500000", "medium"),
    ("250000", "medium"),
    ("249999.99", "weak"),
    ("100000", "weak"),
    ("0", "weak"),
])
def test_classify_signal_strength_thresholds(amount, expected_strength):
    """classify_signal_strength returns the correct tier at every threshold boundary."""
    assert classify_signal_strength(Decimal(amount)) == expected_strength


def test_strong_signal_from_large_award():
    """award_amount >= 1_000_000 produces signal_strength='strong' via detect."""
    ev = _make_evidence(award_amount="1500000.00")
    db = _make_session(evidence=ev)

    result = detect_signals_for_evidence(ev.id, db)

    assert len(result) == 1
    assert result[0].signal_strength == "strong"


def test_medium_signal_from_mid_award():
    """award_amount in [250_000, 1_000_000) produces signal_strength='medium' via detect."""
    ev = _make_evidence(award_amount="500000.00")
    db = _make_session(evidence=ev)

    result = detect_signals_for_evidence(ev.id, db)

    assert len(result) == 1
    assert result[0].signal_strength == "medium"


def test_weak_signal_from_small_award():
    """award_amount < 250_000 produces signal_strength='weak' via detect."""
    ev = _make_evidence(award_amount="100000.00")
    db = _make_session(evidence=ev)

    result = detect_signals_for_evidence(ev.id, db)

    assert len(result) == 1
    assert result[0].signal_strength == "weak"


# ─── Test 11: calling twice does not create duplicate signals ─────────────────


def test_idempotent_no_duplicate_signals():
    """Calling detect_signals_for_evidence twice for the same evidence_id creates only one Signal."""
    ev = _make_evidence()

    added: list = []
    call_count = [0]

    def _execute(stmt):
        result = MagicMock()
        if call_count[0] == 0:
            result.scalars.return_value.all.return_value = []
        else:
            result.scalars.return_value.all.return_value = [
                o for o in added if isinstance(o, Signal)
            ]
        call_count[0] += 1
        return result

    def _add(obj):
        added.append(obj)
        try:
            if obj.id is None:
                obj.id = uuid.uuid4()
        except Exception:
            pass

    db = MagicMock()
    db.get.return_value = ev
    db.add.side_effect = _add
    db.execute.side_effect = _execute

    result1 = detect_signals_for_evidence(ev.id, db)
    result2 = detect_signals_for_evidence(ev.id, db)

    signals_added = [o for o in added if isinstance(o, Signal)]
    assert len(signals_added) == 1, "must not create duplicate Signal rows"
    assert len(result1) == 1
    assert len(result2) == 1
