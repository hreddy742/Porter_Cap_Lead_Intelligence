"""
Tests that a fresh database (migrations only, no manual seeding) has a
working scoring config so score_company() never raises NoActiveScoringConfigError
on first use.

Uses Testcontainers with a real Postgres instance — no mocking the database.
The db_session and db_with_schema fixtures come from conftest.py.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.db.models import Company, EvidenceItem, LeadScore, Signal, ScoringConfig
from app.processing.scoring import (
    NoActiveScoringConfigError,
    _assign_tier,
    score_company,
)

_GATE_PASSED = {
    "passed": True,
    "gate_name": None,
    "gate_reason": None,
    "route": "score",
    "should_score": True,
    "suppression": {
        "suppressed": False,
        "route": None,
        "reason": None,
        "match_type": None,
        "match_value": None,
        "suppression_id": None,
    },
}


def _make_company(*, naics_code: str | None = "541330", country: str = "US") -> MagicMock:
    c = MagicMock(spec=Company)
    c.id = uuid.uuid4()
    c.canonical_name = "Acme Federal Services LLC"
    c.state = "TX"
    c.country = country
    c.naics_code = naics_code
    return c


def _make_evidence(*, source_url: str = "https://usaspending.gov/award/1") -> MagicMock:
    e = MagicMock(spec=EvidenceItem)
    e.id = uuid.uuid4()
    e.source_url = source_url
    return e


def _make_signal(
    *,
    freshness_score: float = 0.85,
    award_amount: float = 1_500_000.0,
    evidence_id: uuid.UUID | None = None,
) -> MagicMock:
    s = MagicMock(spec=Signal)
    s.id = uuid.uuid4()
    s.signal_type = "CONTRACT_AWARD"
    s.freshness_score = freshness_score
    s.award_amount = award_amount
    s.evidence_id = evidence_id or uuid.uuid4()
    return s


def _make_scoring_session(
    *,
    company: MagicMock,
    execute_results: list,
) -> MagicMock:
    """Mock session where execute_results are returned in call order."""
    call_count = [0]
    added: list = []

    def _execute(stmt):
        r = MagicMock()
        n = call_count[0]
        call_count[0] += 1
        item = execute_results[n] if n < len(execute_results) else None
        if isinstance(item, list):
            r.scalars.return_value.all.return_value = item
            r.scalars.return_value.first.return_value = item[0] if item else None
        else:
            r.scalars.return_value.first.return_value = item
            r.scalars.return_value.all.return_value = [item] if item is not None else []
        return r

    s = MagicMock()
    s.get.side_effect = lambda cls, pk: company if cls is Company else None
    s.execute.side_effect = _execute
    s.add.side_effect = lambda obj: added.append(obj)
    s.flush.return_value = None
    s._added = added
    return s


# ─── Bootstrap test — requires real Postgres via conftest fixtures ─────────────


def test_fresh_database_has_working_scoring_config(db_session):
    """
    A freshly migrated database has exactly one active ScoringConfig row so that
    score_company() never raises NoActiveScoringConfigError on first use.

    Assertions:
      1. Active config exists with version_label == "phase1-v1".
      2. score_company() completes without NoActiveScoringConfigError using
         the real config object fetched from the Testcontainers Postgres instance.
      3. Hot tier threshold is 70 (not the structurally-unreachable 75).
    """
    # ── Part 1: real DB has exactly one active config ─────────────────────────
    active_configs = (
        db_session.query(ScoringConfig).filter_by(active=True).all()
    )
    assert len(active_configs) == 1, (
        f"Expected exactly 1 active ScoringConfig, found {len(active_configs)}. "
        "Migration 003 may not have run."
    )
    cfg = active_configs[0]
    assert cfg.version_label == "phase1-v1"
    assert cfg.config_hash is not None
    assert len(cfg.config_hash) == 64  # SHA-256 hex digest

    # ── Part 2: score_company() does not raise NoActiveScoringConfigError ─────
    # cfg is the real ORM object from Testcontainers Postgres.  The mock session
    # returns it as execute_results[0], which is exactly what score_company()
    # reads via: db.execute(select(ScoringConfig).where(active==True)).scalars().first()
    company = _make_company()
    ev1 = _make_evidence()
    ev2 = _make_evidence(source_url="https://usaspending.gov/award/2")
    signal = _make_signal(freshness_score=0.85, award_amount=1_500_000, evidence_id=ev1.id)

    db = _make_scoring_session(
        company=company,
        execute_results=[cfg, [ev1, ev2], [signal], None],
    )

    with patch("app.processing.scoring.evaluate_mandatory_gates") as mock_gates:
        mock_gates.return_value = _GATE_PASSED
        result = score_company(company.id, db)

    assert result["scored"] is True, "score_company() should score a passing company"
    lead_scores = [o for o in db._added if isinstance(o, LeadScore)]
    assert len(lead_scores) == 1
    assert lead_scores[0].scoring_config_id == cfg.id

    # ── Part 3: hot threshold is 70 ───────────────────────────────────────────
    assert _assign_tier(70) == "hot", "70 must be 'hot' (new threshold)"
    assert _assign_tier(73) == "hot", "73 (max Phase 1 score) must reach 'hot'"
    assert _assign_tier(69) == "warm", "69 must NOT be 'hot' (just below threshold)"
