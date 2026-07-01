"""
SBIR/STTR grants connector — federal R&D grant recipients as B2B leads.

Queries the SBIR.gov public API for recent Small Business Innovation Research (SBIR)
and Small Business Technology Transfer (STTR) grants awarded to companies in Porter
Capital's target states.

A company that received an SBIR/STTR grant:
  - Is confirmed as a small B2B business
  - Is actively working on a government project (invoicing the government)
  - Is growing and may be cash-constrained
  - Is a strong A/R financing candidate (government receivables)

Confirmed by John Cox Miller, Porter Capital, July 2026.

API: https://api.www.sbir.gov/public/api/awards
No API key required. Free, public, federal government data.

Filters applied:
  - State: AL, GA, TN, FL, MS, TX, VA (Porter's geographic ICP)
  - Award year: 2022 or later (recent grants only)
  - Award amount: >= $50,000
  - Exclude universities, colleges, research institutions

University filter is critical: SBIR awards go to universities too.
Porter cannot factor academic institutions — filter them out.

Signal types produced:
  SBIR_GRANT — all phases produce this signal type.
  Signal strength depends on phase:
    Phase II, III → "strong" (larger grants, further into commercialization)
    Phase I       → "medium" (early-stage government relationship)

Env vars:
  SBIR_GRANTS_ENABLED    enable this connector via env (default false)
  SBIR_TEST_LIMIT        max records to process (0 = no limit, default 0)
  SBIR_API_TIMEOUT       HTTP timeout in seconds (default 30.0)
  SBIR_PAGE_SIZE         records per API page (default 100, max 400)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RawSourceEvent, SourceRegistry, SourceRun

logger = structlog.get_logger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

_API_BASE_URL = "https://api.www.sbir.gov/public/api/awards"
_SOURCE_URL = "https://www.sbir.gov/awards"

_DEFAULT_PAGE_SIZE = 100
_PAGE_DELAY = 0.5        # seconds between pages (polite rate limit)
_API_TIMEOUT = 30.0
_MAX_RETRIES = 3

# Porter's geographic ICP — same as SBA and USASpending connectors
_TARGET_STATES = frozenset({"AL", "GA", "TN", "FL", "MS", "TX", "VA"})

# Earliest award year to include (recent awards only)
_MIN_AWARD_YEAR = 2022

# Minimum grant size (very small grants are likely pre-revenue)
_MIN_AWARD_AMOUNT = Decimal("50000")

# Company name tokens that indicate an academic / research institution.
# Porter cannot factor universities or government labs.
_ACADEMIC_KEYWORDS = frozenset({
    "university",
    "college",
    "institute",
    "laboratory",
    "laboratories",
    "research foundation",
    "polytechnic",
    "community college",
    "technical college",
    "school of",
})


class ConnectorError(Exception):
    """Raised when a fatal connector error occurs."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _env_int(var: str, default: int) -> int:
    raw = os.getenv(var)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{var}={raw!r} is not a valid integer")


def _env_float(var: str, default: float) -> float:
    raw = os.getenv(var)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{var}={raw!r} is not a valid number")


# ─── Pydantic v2 record model ─────────────────────────────────────────────────


class SBIRGrantRecord(BaseModel):
    """Validated representation of one SBIR/STTR award from the public API."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    firm: str
    city: str | None = None
    state: str
    zip: str | None = None
    address1: str | None = None
    address2: str | None = None
    award_amount: Decimal
    award_year: int
    agency: str | None = None
    branch: str | None = None
    phase: str | None = None
    program: str | None = None    # "SBIR" or "STTR"
    award_title: str | None = None
    abstract: str | None = None
    uei: str | None = None
    duns: str | None = None

    @field_validator("firm", mode="before")
    @classmethod
    def validate_firm(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("firm must be a non-empty string")
        return v.strip()

    @field_validator("state", mode="before")
    @classmethod
    def validate_state(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("state must be a non-empty string")
        return v.strip().upper()

    @field_validator("award_amount", mode="before")
    @classmethod
    def validate_award_amount(cls, v: object) -> Decimal:
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError("award_amount is required")
        try:
            amount = Decimal(str(v).replace(",", "").strip())
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f"award_amount must be numeric, got {v!r}")
        if amount < 0:
            raise ValueError(f"award_amount must be non-negative, got {amount}")
        return amount

    @field_validator("award_year", mode="before")
    @classmethod
    def validate_award_year(cls, v: object) -> int:
        if v is None:
            raise ValueError("award_year is required")
        try:
            return int(str(v).strip())
        except (ValueError, TypeError):
            raise ValueError(f"award_year must be an integer, got {v!r}")

    @field_validator("city", "zip", "address1", "address2", "agency", "branch",
                     "phase", "program", "award_title", "abstract", "uei", "duns",
                     mode="before")
    @classmethod
    def clean_optional_string(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None


# ─── Pure helpers ─────────────────────────────────────────────────────────────


def is_academic(company_name: str) -> bool:
    """Return True if the name contains an academic/research institution keyword.

    Universities, colleges, and research foundations are valid SBIR recipients
    but Porter Capital cannot factor academic institutions — exclude them.
    """
    name_lower = company_name.lower()
    return any(kw in name_lower for kw in _ACADEMIC_KEYWORDS)


def signal_strength_for_phase(phase: str | None) -> str:
    """Map SBIR phase to Porter signal strength tier.

    Phase II and III are further along commercialization and have larger grants:
      Phase II: ~$750K — strong
      Phase III: varies — strong (commercialization confirmed)
      Phase I:  ~$150K — medium (early stage)
    """
    if not phase:
        return "medium"
    normalized = phase.upper().replace("PHASE", "").strip()
    if normalized in ("II", "III", "2", "3"):
        return "strong"
    return "medium"


def _parse_response(data: object) -> list[dict]:
    """Parse the SBIR API JSON response into a flat list of award dicts.

    Handles both a direct JSON array and Solr-style {"docs": [...]} wrappers.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "response" in data and isinstance(data["response"], dict):
            return data["response"].get("docs", [])
        if "docs" in data:
            return data.get("docs", [])
    return []


# ─── HTTP fetch with retry ────────────────────────────────────────────────────


def _fetch_page(
    state: str,
    start: int,
    page_size: int,
    timeout: float,
    log: structlog.BoundLogger,
) -> list[dict]:
    """Fetch one page of SBIR awards from the public API.

    Returns an empty list on a maintenance 429 after all retries — the connector
    marks the run as failed rather than silently skipping pages.

    Raises ConnectorError if the API is consistently unavailable.
    """
    params: dict = {
        "rows": page_size,
        "start": start,
        "state": state,
        "format": "json",
    }

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = httpx.get(_API_BASE_URL, params=params, timeout=timeout)

            if resp.status_code == 429:
                # API is rate-limited or in maintenance mode
                wait = 30 * attempt
                log.warning(
                    "sbir_api_rate_limited",
                    attempt=attempt,
                    wait_seconds=wait,
                    state=state,
                    start=start,
                )
                time.sleep(wait)
                continue

            if resp.status_code == 200:
                data = resp.json()
                return _parse_response(data)

            resp.raise_for_status()

        except httpx.HTTPStatusError as exc:
            last_exc = exc
            log.warning(
                "sbir_api_http_error",
                status=exc.response.status_code,
                attempt=attempt,
                state=state,
                start=start,
            )
            if attempt < _MAX_RETRIES:
                time.sleep(5 * attempt)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            log.warning(
                "sbir_api_network_error",
                error=str(exc),
                attempt=attempt,
                state=state,
                start=start,
            )
            if attempt < _MAX_RETRIES:
                time.sleep(5 * attempt)

    raise ConnectorError(
        f"SBIR API unavailable for state={state} start={start} "
        f"after {_MAX_RETRIES} attempts: {last_exc}"
    )


# ─── Connector ────────────────────────────────────────────────────────────────


class SBIRGrantsConnector:
    """
    Fetches SBIR/STTR award records from the SBIR.gov public API and stores
    raw events for B2B companies in Porter's target states.

    Paginates state-by-state. Applies year, amount, and university filters
    in Python after the API response (the API only supports state filtering).

    Usage:
        connector = SBIRGrantsConnector(session, source_run, source)
        connector.run()   # never raises; errors recorded in source_run

    Local testing (PowerShell env vars before running):
        $env:SBIR_GRANTS_ENABLED="true"
        $env:SBIR_TEST_LIMIT="100"
        → process only the first 100 records
    """

    def __init__(
        self,
        session: Session,
        source_run: SourceRun,
        source: SourceRegistry,
    ) -> None:
        self.session = session
        self.source_run = source_run
        self.source = source
        self.test_limit = _env_int("SBIR_TEST_LIMIT", 0)
        self._page_size = _env_int("SBIR_PAGE_SIZE", _DEFAULT_PAGE_SIZE)
        self._timeout = _env_float("SBIR_API_TIMEOUT", _API_TIMEOUT)

        self._log = logger.bind(
            connector="sbir_grants",
            source_run_id=str(source_run.id),
        )

    def run(self) -> None:
        """Fetch all SBIR/STTR awards. Mark source_run completed or failed. Never raises."""
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass
        try:
            self._fetch_all()
            self.source_run.status = "completed"
        except ConnectorError as exc:
            self.source_run.status = "failed"
            self.source_run.error_text = str(exc)
            self._log.error("sbir_run_failed", error=str(exc))
        except Exception as exc:
            self.source_run.status = "failed"
            self.source_run.error_text = str(exc)
            self._log.error("sbir_run_unexpected_error", error=str(exc))
        finally:
            self.source_run.finished_at = _utcnow()
            self.session.commit()

    # ── Private ───────────────────────────────────────────────────────────────

    def _fetch_all(self) -> None:
        """Loop over all target states and paginate through awards."""
        for state in sorted(_TARGET_STATES):
            self._fetch_state(state)
            if self.test_limit > 0 and self.source_run.records_fetched >= self.test_limit:
                self._log.info("sbir_test_limit_reached", limit=self.test_limit)
                break

    def _fetch_state(self, state: str) -> None:
        """Paginate through all awards for one state."""
        start = 0

        while True:
            if self.test_limit > 0 and self.source_run.records_fetched >= self.test_limit:
                break

            batch = _fetch_page(state, start, self._page_size, self._timeout, self._log)
            if not batch:
                break

            for raw_record in batch:
                self._process_record(raw_record)

            if len(batch) < self._page_size:
                break

            start += self._page_size
            time.sleep(_PAGE_DELAY)

    def _process_record(self, raw_record: dict) -> None:
        self.source_run.records_fetched += 1

        try:
            record = SBIRGrantRecord.model_validate(raw_record)
        except Exception as exc:
            self.source_run.quarantine_count += 1
            self._log.warning(
                "sbir_validation_failure",
                company=raw_record.get("firm"),
                error=str(exc),
            )
            return

        # State filter
        if record.state not in _TARGET_STATES:
            self.source_run.records_skipped += 1
            return

        # Year filter — only recent awards produce fresh signals
        if record.award_year < _MIN_AWARD_YEAR:
            self.source_run.records_skipped += 1
            return

        # Amount filter — very small grants are unlikely A/R candidates
        if record.award_amount < _MIN_AWARD_AMOUNT:
            self.source_run.records_skipped += 1
            return

        # University filter — Porter cannot factor academic institutions
        if is_academic(record.firm):
            self.source_run.records_skipped += 1
            self._log.info(
                "sbir_academic_skipped",
                company=record.firm,
                state=record.state,
            )
            return

        strength = signal_strength_for_phase(record.phase)
        phase_display = record.phase or "Unknown Phase"
        program_display = record.program or "SBIR"

        payload: dict = {
            "firm": record.firm,
            "address1": record.address1,
            "address2": record.address2,
            "city": record.city,
            "state": record.state,
            "zip": record.zip,
            "award_amount": str(record.award_amount),
            "award_year": record.award_year,
            "agency": record.agency,
            "branch": record.branch,
            "phase": record.phase,
            "program": record.program,
            "award_title": record.award_title,
            "abstract": (record.abstract or "")[:500] if record.abstract else None,
            "uei": record.uei,
            "duns": record.duns,
            "sbir_signal_type": "SBIR_GRANT",
            "sbir_signal_strength": strength,
            "description": (
                f"{program_display} {phase_display} grant of "
                f"${record.award_amount:,.0f} from "
                f"{record.agency or 'federal agency'}"
                + (f" ({record.branch})" if record.branch else "")
                + f" for: {(record.award_title or 'N/A')[:100]}"
            ),
        }

        content_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()

        existing = self.session.execute(
            select(RawSourceEvent).where(
                RawSourceEvent.source_id == self.source.id,
                RawSourceEvent.content_hash == content_hash,
            )
        ).scalar_one_or_none()

        if existing is not None:
            self.source_run.records_skipped += 1
            return

        source_record_id = (
            f"{record.firm}|{record.state}|"
            f"{record.award_year}|{str(record.award_amount)}"
        )

        event = RawSourceEvent(
            source_id=self.source.id,
            source_run_id=self.source_run.id,
            source_record_id=source_record_id[:500],
            company_name_raw=record.firm,
            payload=payload,
            content_hash=content_hash,
            source_url=_SOURCE_URL,
        )
        self.session.add(event)
        self.source_run.records_valid += 1

        self._log.info(
            "sbir_record_stored",
            company=record.firm,
            state=record.state,
            award_year=record.award_year,
            amount=str(record.award_amount),
            phase=record.phase,
            signal_strength=strength,
        )
