"""
Tests for app/processing/scoring.py.

All tests use MagicMock sessions and patch evaluate_mandatory_gates so that
gate logic is tested separately in test_gates.py.

Execute call order inside score_company (after gates are mocked):
  call 0 → ScoringConfig (single row, .first())
  db.get  → Company
  call 1 → EvidenceItem (.all())
  call 2 → Signal (.all())
  call 3 → LeadCandidate (.first()) — check for existing active candidate
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.db.models import (
    Company,
    EvidenceItem,
    LeadCandidate,
    LeadScore,
    Signal,
    ScoringConfig,
)
from app.processing.scoring import (
    NoActiveScoringConfigError,
    ScoringIntegrityError,
    _assign_tier,
    score_company,
)


# ─── Shared gate return values ────────────────────────────────────────────────

_GATE_PASSED = {
    "passed": True,
    "gate_name": None,
    "gate_reason": None,
    "route": "score",
    "should_score": True,
    "suppression": {"suppressed": False, "route": None, "reason": None,
                    "match_type": None, "match_value": None, "suppression_id": None},
}

_GATE_BLOCKED = {
    "passed": False,
    "gate_name": "no_evidence",
    "gate_reason": "no_evidence",
    "route": "archive",
    "should_score": False,
    "suppression": None,
}


# ─── Mock object builders ─────────────────────────────────────────────────────


def _make_company(
    *,
    naics_code: str | None = None,
    country: str = "US",
) -> MagicMock:
    c = MagicMock(spec=Company)
    c.id = uuid.uuid4()
    c.canonical_name = "Acme Federal Services LLC"
    c.state = "TX"
    c.country = country
    c.naics_code = naics_code
    c.industry = None
    c.business_type = None
    return c


def _make_scoring_config() -> MagicMock:
    cfg = MagicMock(spec=ScoringConfig)
    cfg.id = uuid.uuid4()
    cfg.config_hash = "a" * 64
    cfg.active = True
    cfg.version_label = "v1-test"
    cfg.config = {}
    return cfg


def _make_evidence(*, source_url: str = "https://usaspending.gov/award/1") -> MagicMock:
    e = MagicMock(spec=EvidenceItem)
    e.id = uuid.uuid4()
    e.source_url = source_url
    return e


def _make_signal(
    *,
    signal_type: str = "CONTRACT_AWARD",
    freshness_score: float = 0.85,
    award_amount: float | None = 1_500_000.0,
    evidence_id: uuid.UUID | None = None,
) -> MagicMock:
    s = MagicMock(spec=Signal)
    s.id = uuid.uuid4()
    s.signal_type = signal_type
    s.freshness_score = freshness_score
    s.award_amount = award_amount
    s.evidence_id = evidence_id or uuid.uuid4()
    return s


def _make_scoring_session(
    *,
    company: MagicMock | None = None,
    execute_results: list | None = None,
) -> MagicMock:
    """
    Build a MagicMock session for scoring tests.

    execute_results is a positional list matched to execute() call order:
      [0] ScoringConfig — single object or None (use .first())
      [1] EvidenceItem list  (use .all())
      [2] Signal list        (use .all())
      [3] LeadCandidate — single object or None (use .first())

    db.get(Company, pk) always returns `company`.
    db.add() records objects in db._added for inspection.
    """
    results = execute_results or []
    call_count = [0]
    added: list = []

    def _execute(stmt):
        r = MagicMock()
        n = call_count[0]
        call_count[0] += 1
        item = results[n] if n < len(results) else None
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


# ─── Standard clean-company session ──────────────────────────────────────────


def _clean_session(
    company: MagicMock,
    cfg: MagicMock,
    evidence_items: list,
    signals: list,
    existing_lead: MagicMock | None = None,
) -> MagicMock:
    return _make_scoring_session(
        company=company,
        execute_results=[cfg, evidence_items, signals, existing_lead],
    )


# ─── Test 1: gates called first, gated company not scored ────────────────────


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_gates_called_first_and_early_exit_when_blocked(mock_gates):
    """score_company calls evaluate_mandatory_gates first; if blocked, no scoring occurs."""
    mock_gates.return_value = _GATE_BLOCKED

    company_id = uuid.uuid4()
    db = _make_scoring_session(execute_results=[])

    result = score_company(company_id, db)

    mock_gates.assert_called_once_with(company_id, db)
    assert result["scored"] is False
    # No execute calls should happen after early exit.
    assert db.execute.call_count == 0


# ─── Test 2: gated company returns scored=False and total_score=None ─────────


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_gated_company_result_shape(mock_gates):
    """Gated company returns the expected result shape with no score values."""
    mock_gates.return_value = _GATE_BLOCKED

    company_id = uuid.uuid4()
    db = _make_scoring_session(execute_results=[])

    result = score_company(company_id, db)

    assert result["scored"] is False
    assert result["total_score"] is None
    assert result["tier"] is None
    assert result["component_breakdown"] == {}
    assert result["evidence_ids"] == []
    assert result["gate_result"] is _GATE_BLOCKED


# ─── Test 3: clean company produces a numeric score ───────────────────────────


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_clean_company_produces_numeric_score(mock_gates):
    """A company with evidence, fresh signal, and AR-heavy NAICS gets a numeric score."""
    mock_gates.return_value = _GATE_PASSED

    company = _make_company(naics_code="541330")
    cfg = _make_scoring_config()
    ev1 = _make_evidence()
    ev2 = _make_evidence()
    signal = _make_signal(freshness_score=0.85, award_amount=1_500_000, evidence_id=ev1.id)

    db = _clean_session(company, cfg, [ev1, ev2], [signal])
    result = score_company(company.id, db)

    assert result["scored"] is True
    assert isinstance(result["total_score"], int)
    assert result["total_score"] > 0
    assert result["tier"] in ("hot", "warm", "cold", "archive")


# ─── Test 4: every component with points has evidence_ids ────────────────────


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_every_scoring_component_with_points_cites_evidence(mock_gates):
    """No component may award points > 0 without listing at least one evidence_id."""
    mock_gates.return_value = _GATE_PASSED

    company = _make_company(naics_code="541330")
    cfg = _make_scoring_config()
    ev1 = _make_evidence()
    ev2 = _make_evidence()
    signal = _make_signal(freshness_score=0.85, award_amount=1_500_000, evidence_id=ev1.id)

    db = _clean_session(company, cfg, [ev1, ev2], [signal])
    result = score_company(company.id, db)

    breakdown = result["component_breakdown"]
    for name, comp in breakdown.items():
        if comp["points"] > 0:
            assert comp["evidence_ids"], (
                f"Component '{name}' awarded {comp['points']} points "
                "but evidence_ids is empty — violates founding rule 1"
            )


# ─── Test 5: component with points but no evidence_ids raises integrity error ─


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_integrity_error_when_points_but_no_evidence(mock_gates):
    """ScoringIntegrityError is raised when a component would award points with no evidence."""
    mock_gates.return_value = _GATE_PASSED

    # Company with AR-heavy NAICS triggers porter_fit NAICS bonus (+15 + +5 for US).
    # Evidence items = [] means all_evidence_ids = [].
    # Signal has no award_amount → no citation from that path.
    # Result: pf_points > 0 but pf_evidence = [] → ScoringIntegrityError.
    company = _make_company(naics_code="541330", country="US")
    cfg = _make_scoring_config()
    signal_no_award = _make_signal(signal_type="OTHER", award_amount=None, freshness_score=0.8)

    db = _make_scoring_session(
        company=company,
        execute_results=[cfg, [], [signal_no_award], None],
    )

    with pytest.raises(ScoringIntegrityError):
        score_company(company.id, db)


# ─── Test 6: same input scored 3 times gives identical score and tier ─────────


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_scoring_is_deterministic(mock_gates):
    """Same company + evidence + config → identical total_score and tier every run."""
    mock_gates.return_value = _GATE_PASSED

    company = _make_company(naics_code="541330")
    cfg = _make_scoring_config()
    ev1 = _make_evidence()
    ev2 = _make_evidence()
    signal = _make_signal(freshness_score=0.85, award_amount=1_500_000, evidence_id=ev1.id)

    results = []
    for _ in range(3):
        db = _clean_session(company, cfg, [ev1, ev2], [signal])
        results.append(score_company(company.id, db))

    scores = [r["total_score"] for r in results]
    tiers = [r["tier"] for r in results]

    assert scores[0] == scores[1] == scores[2], f"Non-deterministic scores: {scores}"
    assert tiers[0] == tiers[1] == tiers[2], f"Non-deterministic tiers: {tiers}"


# ─── Tests 7–10: tier threshold boundaries ────────────────────────────────────


def test_hot_threshold():
    """Score >= 70 maps to 'hot'."""
    assert _assign_tier(70) == "hot"
    assert _assign_tier(100) == "hot"
    assert _assign_tier(73) == "hot"


def test_warm_threshold():
    """Score >= 55 and < 70 maps to 'warm'."""
    assert _assign_tier(55) == "warm"
    assert _assign_tier(69) == "warm"
    assert _assign_tier(60) == "warm"


def test_cold_threshold():
    """Score >= 35 and < 55 maps to 'cold'."""
    assert _assign_tier(35) == "cold"
    assert _assign_tier(54) == "cold"
    assert _assign_tier(40) == "cold"


def test_archive_threshold():
    """Score < 35 maps to 'archive'."""
    assert _assign_tier(0) == "archive"
    assert _assign_tier(34) == "archive"
    assert _assign_tier(1) == "archive"


# ─── Test 11: Phase 1 contactability is always 0 ─────────────────────────────


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_phase1_contactability_is_always_zero(mock_gates):
    """Contactability component must be 0 in Phase 1 (enrichment not built)."""
    mock_gates.return_value = _GATE_PASSED

    company = _make_company(naics_code="541330")
    cfg = _make_scoring_config()
    ev = _make_evidence()
    signal = _make_signal(freshness_score=0.85, award_amount=1_000_000, evidence_id=ev.id)

    db = _clean_session(company, cfg, [ev], [signal])
    result = score_company(company.id, db)

    assert result["component_breakdown"]["contactability"]["points"] == 0


# ─── Test 12: A/R Financing Fit capped at 10 in Phase 1 ──────────────────────


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_ar_fit_capped_at_10_in_phase1(mock_gates):
    """A/R Financing Fit is capped at 10 even when raw points would exceed it."""
    mock_gates.return_value = _GATE_PASSED

    # AR-heavy NAICS (+7) + CONTRACT_AWARD signal (+3) = 10, which hits the cap exactly.
    # If the cap didn't exist, the component could exceed 10.
    company = _make_company(naics_code="541330")
    cfg = _make_scoring_config()
    ev1 = _make_evidence()
    ev2 = _make_evidence()
    signal = _make_signal(
        signal_type="CONTRACT_AWARD",
        freshness_score=0.85,
        award_amount=2_000_000,
        evidence_id=ev1.id,
    )

    db = _clean_session(company, cfg, [ev1, ev2], [signal])
    result = score_company(company.id, db)

    ar_points = result["component_breakdown"]["ar_fit"]["points"]
    assert ar_points <= 10, f"A/R Fit exceeded Phase 1 cap: {ar_points}"
    assert result["component_breakdown"]["ar_fit"]["phase1_cap"] == 10


# ─── Test 13: lead_scores row inserted only for successfully scored company ───


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_lead_scores_row_inserted_for_scored_company(mock_gates):
    """One LeadScore row is added to the session for a successfully scored company."""
    mock_gates.return_value = _GATE_PASSED

    company = _make_company(naics_code="541330")
    cfg = _make_scoring_config()
    ev = _make_evidence()
    signal = _make_signal(freshness_score=0.85, award_amount=1_000_000, evidence_id=ev.id)

    db = _clean_session(company, cfg, [ev], [signal])
    score_company(company.id, db)

    lead_scores = [o for o in db._added if isinstance(o, LeadScore)]
    assert len(lead_scores) == 1
    assert lead_scores[0].total_score is not None
    assert lead_scores[0].gate_result == "passed"
    assert lead_scores[0].scoring_config_id == cfg.id


# ─── Test 14: no lead_scores row for gated company ────────────────────────────


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_no_lead_scores_row_for_gated_company(mock_gates):
    """No LeadScore is created when the company fails gates."""
    mock_gates.return_value = _GATE_BLOCKED

    db = _make_scoring_session(execute_results=[])
    score_company(uuid.uuid4(), db)

    lead_scores = [o for o in db._added if isinstance(o, LeadScore)]
    assert len(lead_scores) == 0


# ─── Test 15: missing active config raises a clear error ──────────────────────


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_missing_active_config_raises_clear_error(mock_gates):
    """NoActiveScoringConfigError is raised when no active scoring_config exists."""
    mock_gates.return_value = _GATE_PASSED

    company = _make_company()
    # execute_results[0] = None means ScoringConfig.first() returns None.
    db = _make_scoring_session(
        company=company,
        execute_results=[None],
    )

    with pytest.raises(NoActiveScoringConfigError):
        score_company(company.id, db)


# ─── Bonus: lead_candidate row also created ───────────────────────────────────


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_lead_candidate_row_created_for_scored_company(mock_gates):
    """A LeadCandidate is added to the session alongside the LeadScore."""
    mock_gates.return_value = _GATE_PASSED

    company = _make_company(naics_code="541330")
    cfg = _make_scoring_config()
    ev = _make_evidence()
    signal = _make_signal(freshness_score=0.85, award_amount=1_000_000, evidence_id=ev.id)

    db = _clean_session(company, cfg, [ev], [signal])
    result = score_company(company.id, db)

    candidates = [o for o in db._added if isinstance(o, LeadCandidate)]
    assert len(candidates) == 1
    assert candidates[0].gate_result == "passed"
    assert candidates[0].current_score == result["total_score"]
    assert candidates[0].tier == result["tier"]


# ─── Tests 17–18: NAICS code effect on scoring ────────────────────────────────


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_ar_heavy_naics_gives_full_naics_porter_fit_points(mock_gates):
    """company.naics_code in an AR-heavy prefix gives porter_fit the +15 NAICS bonus."""
    mock_gates.return_value = _GATE_PASSED

    # NAICS 541330 starts with "54" → AR-heavy.  Award $1M (in $250K–$10M range).
    # Expected porter_fit = 15 (NAICS) + 5 (US country) + 5 (award range) = 25.
    company = _make_company(naics_code="541330", country="US")
    cfg = _make_scoring_config()
    ev = _make_evidence()
    signal = _make_signal(freshness_score=0.85, award_amount=1_000_000, evidence_id=ev.id)

    db = _clean_session(company, cfg, [ev], [signal])
    result = score_company(company.id, db)

    pf = result["component_breakdown"]["porter_fit"]
    assert pf["points"] == 25, (
        f"AR-heavy NAICS should give porter_fit=25, got {pf['points']}"
    )


@patch("app.processing.scoring.evaluate_mandatory_gates")
def test_null_naics_code_gives_zero_naics_porter_fit_bonus(mock_gates):
    """company.naics_code = None → no NAICS bonus; porter_fit is limited to country + award points."""
    mock_gates.return_value = _GATE_PASSED

    # No NAICS.  Award $1M (in range).  Country US.
    # Expected porter_fit = 0 (no NAICS) + 5 (US) + 5 (award range) = 10.
    company = _make_company(naics_code=None, country="US")
    cfg = _make_scoring_config()
    ev = _make_evidence()
    signal = _make_signal(freshness_score=0.85, award_amount=1_000_000, evidence_id=ev.id)

    db = _clean_session(company, cfg, [ev], [signal])
    result = score_company(company.id, db)

    pf = result["component_breakdown"]["porter_fit"]
    assert pf["points"] == 10, (
        f"Null NAICS should give porter_fit=10 (country+award only), got {pf['points']}"
    )
