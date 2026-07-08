"""
SBA 7(a) FOIA bulk loan connector — fetches loan records for B2B companies.

Downloads the SBA 7(a) FOIA bulk CSV from data.sba.gov.
Caches locally for 30 days to avoid repeated large downloads (~150 MB).
Reads the CSV in chunks of 1000 rows — never loads 373K rows into memory.

Filters applied (per John Cox Miller, Porter Capital, July 2026 — collect
everything, soft-flag rather than hard-block):
  - State: ALL 50 states (Porter is expanding nationally — geographic
    restriction removed)
  - Loan amount: >= $50K (the $5M SBA program maximum makes an upper cap
    redundant)
  - Activeness: included if LoanStatus is EXEMPT/COMMIT (currently active/
    pending) OR approval_date >= 2019-01-01 (7-year recency window)
  - NAICS: all sectors collected; B2C sub-sectors (621-624 healthcare) and
    non-B2B sectors (retail 44/45, restaurants/hotels 72) are soft-flagged
    (sector_excluded=True), not hard-blocked. Blank NAICS is included.
  - LoanStatus: hard-skip only CHGOFF (defaulted) and CANCLD (cancelled)

Signal types produced:
  SBA_LOAN_PIF     — paid-in-full loan (company grew, repaid, now scaling)
  SBA_LOAN_ACTIVE  — active loan, incl. EXEMPT (lien on receivables, needs
                     qualification call)
  SBA_LOAN_PENDING — COMMIT status (just approved, needs working capital now)

Confirmed by John Cox Miller (Porter Capital SVP), June 25 2026 and July 2026:
  - Include ALL B2B industries, not just government contractors
  - Noise keywords and B2C NAICS sub-sectors soft-flag, they don't hard-block
  - PIF alumni are strong prospects; active loans are workable

Cache logic:
  If cache file exists AND age < 30 days: use cached file.
  Else: download fresh, save to cache, log sba_cache_refreshed.

Env vars:
  SBA_LOANS_TEST_LIMIT    max rows to read (0 = no limit, default 0)
  SBA_CACHE_EXPIRY_DAYS   cache TTL in days (default 30)
  SBA_CACHE_PATH          local cache file path (default: data/sba_loans_cache.csv)
  SBA_DOWNLOAD_TIMEOUT    HTTP download timeout in seconds (default 120.0)
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RawSourceEvent, SourceRegistry, SourceRun

logger = structlog.get_logger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

# Confirmed dead as of 2026-07-08 (returns 404; CKAN resource_show for this
# ID also 404s, so the resource was actually removed, not just relinked).
# As of that date the "7-a-504-foia" dataset on data.sba.gov has ZERO CSV
# resources — only the data dictionary XLSX remains listed. No replacement
# URL exists yet to swap in. Re-check data.sba.gov/en/dataset/7-a-504-foia
# periodically; the portal has reorganized this URL before (see build notes
# in CLAUDE.md). Until SBA republishes, _ensure_cache() falls back to the
# existing stale local cache rather than failing the run.
_DOWNLOAD_URL = (
    "https://data.sba.gov/en/dataset/0ff8e8e9-b967-4f4e-987c-6ac78c575087/"
    "resource/d67d3ccb-2002-4134-a288-481b51cd3479/download/"
    "foia-7a-fy2020-present-asof-260331.csv"
)

_DEFAULT_CACHE_PATH = "data/sba_loans_cache.csv"
_DEFAULT_CACHE_EXPIRY_DAYS = 30
_CHUNK_SIZE = 1000

# Minimum loan amount — filters administrative noise. No maximum: the SBA
# 7(a) program's own $5M cap makes an upper filter redundant. Porter is
# expanding nationally, so the state restriction is removed entirely.
# Fixed per John Cox Miller, Porter Capital, July 2026.
_MIN_LOAN_AMOUNT = Decimal("50000")

# Recency window for PIF/blank-status loans (7 years). EXEMPT and COMMIT are
# always included regardless of age — they represent currently active/pending
# loans. Fixed per John Cox Miller, Porter Capital, July 2026.
_MIN_APPROVAL_DATE = date(2019, 1, 1)

# LoanStatus values that hard-skip — defaulted or cancelled loans only.
# EXEMPT means an active loan (not exempt from reporting) and must NOT be
# skipped — this was a critical bug. Fixed per John Cox Miller, Porter
# Capital, July 2026.
_SKIP_LOAN_STATUSES = frozenset({"CHGOFF", "CANCLD"})

# Statuses that are always "currently active/pending" regardless of age.
_ALWAYS_ACTIVE_STATUSES = frozenset({"EXEMPT", "COMMIT"})

# B2B NAICS prefixes to include — excludes pure B2C (44/45 Retail, 72 Food/Hotels).
# Confirmed by John Cox Miller, Porter Capital, June 25 2026.
# Porter is expanding beyond government contractors to all B2B industries.
_SBA_INCLUDED_NAICS_PREFIXES = frozenset({
    "21",              # Mining
    "22",              # Utilities
    "23",              # Construction
    "31", "32", "33",  # Manufacturing
    "42",              # Wholesale Trade
    "48", "49",        # Transportation/Warehousing
    "51",              # Information/Tech
    "52",              # Finance and Insurance
    "54",              # Professional/Technical Services
    "55",              # Management of Companies
    "56",              # Administrative/Staffing
    "61",              # Educational Services
    "62",              # Health Care (B2B medical)
    "71",              # Arts/Entertainment (B2B events)
    "81",              # Other Services (commercial)
    "92",              # Public Administration
    # EXCLUDED — pure B2C:
    # "44", "45"       # Retail Trade
    # "72"             # Restaurants/Hotels/Bars
})

# Healthcare sub-sectors that are B2C for SBA loan purposes.
# Individual practices bill patients/insurers — not B2B A/R accounts.
# NOTE: Do NOT add these to the main pipeline exclusion gate. USASpending 62x
# may include medical SUPPLY/DEVICE companies that are genuinely B2B.
_SBA_EXCLUDED_NAICS_PREFIXES = frozenset({
    "621",  # Ambulatory health care (physician, dental, chiropractic offices)
    "622",  # Hospitals
    "623",  # Nursing and residential care facilities
    "624",  # Social assistance (child daycare, social services)
})

# Company name keywords that flag consumer-facing businesses not suited for
# B2B A/R financing. Applied after NAICS filters as a secondary safeguard.
_SBA_NOISE_KEYWORDS = frozenset({
    "anesthesia",
    "dental",
    "dentist",
    "orthodontic",
    "pediatric",
    "chiropractic",
    "veterinary",
    "daycare",
    "day care",
    "child care",
    "preschool",
    "academy",
})

_SOURCE_URL = "https://data.sba.gov/en/dataset/0ff8e8e9-b967-4f4e-987c-6ac78c575087"


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


class SBALoanRecord(BaseModel):
    """Validated representation of one SBA 7(a) loan CSV row."""

    model_config = ConfigDict(populate_by_name=True)

    borr_name: str = Field(alias="borrname")
    borr_state: str = Field(alias="borrstate")
    borr_city: str | None = Field(None, alias="borrcity")
    borr_street: str | None = Field(None, alias="borrstreet")
    borr_zip: str | None = Field(None, alias="borrzip")
    naics_code: str | None = Field(None, alias="naicscode")
    naics_description: str | None = Field(None, alias="naicsdescription")
    gross_approval: Decimal = Field(alias="grossapproval")
    approval_date: date = Field(alias="approvaldate")
    loan_status: str | None = Field(None, alias="loanstatus")
    jobs_supported: int | None = Field(None, alias="jobssupported")

    @field_validator("borr_name", mode="before")
    @classmethod
    def validate_borr_name(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("BorrName must be a non-empty string")
        stripped = v.strip()
        if any(kw in stripped.lower() for kw in {"domestic awardees", "undisclosed"}):
            raise ValueError(f"placeholder BorrName: {stripped}")
        return stripped

    @field_validator("borr_state", mode="before")
    @classmethod
    def validate_borr_state(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("BorrState must be a non-empty string")
        return v.strip().upper()

    @field_validator("gross_approval", mode="before")
    @classmethod
    def validate_gross_approval(cls, v: object) -> Decimal:
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError("GrossApproval is required")
        try:
            amount = Decimal(str(v).replace(",", "").strip())
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f"GrossApproval must be numeric, got {v!r}")
        if amount <= 0:
            raise ValueError(f"GrossApproval must be positive, got {amount}")
        return amount

    @field_validator("approval_date", mode="before")
    @classmethod
    def parse_approval_date(cls, v: object) -> date:
        if isinstance(v, date) and not isinstance(v, datetime):
            return v
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, str) and v.strip():
            stripped = v.strip()
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(stripped, fmt).date()
                except ValueError:
                    continue
        raise ValueError(f"ApprovalDate must be a parseable date string, got {v!r}")

    @field_validator("naics_code", mode="before")
    @classmethod
    def clean_naics_code(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    @field_validator("loan_status", mode="before")
    @classmethod
    def clean_loan_status(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s.upper() if s else None

    @field_validator("jobs_supported", mode="before")
    @classmethod
    def parse_jobs_supported(cls, v: object) -> int | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            return int(str(v).strip().split(".")[0])
        except (ValueError, TypeError):
            return None

    @field_validator("borr_city", "borr_street", "borr_zip", "naics_description", mode="before")
    @classmethod
    def clean_optional_string(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None


def _is_included_naics(naics_code: str | None) -> bool:
    """Return True if the NAICS code belongs to a Porter B2B target sector."""
    if not naics_code:
        return False
    code = str(naics_code).strip()
    return any(code.startswith(prefix) for prefix in _SBA_INCLUDED_NAICS_PREFIXES)


def _is_excluded_naics(naics_code: str | None) -> bool:
    """Return True if the NAICS code is in a B2C-only sub-sector excluded from SBA leads."""
    if not naics_code:
        return False
    code = str(naics_code).strip()
    return any(code.startswith(prefix) for prefix in _SBA_EXCLUDED_NAICS_PREFIXES)


def _has_noise_keyword(company_name: str) -> bool:
    """Return True if the company name contains a B2C noise keyword."""
    name_lower = company_name.lower()
    return any(kw in name_lower for kw in _SBA_NOISE_KEYWORDS)


def _signal_type_for_status(loan_status: str | None) -> str:
    """Map LoanStatus to signal type.

    PIF          — paid-in-full (strongest signal, company grew and repaid)
    COMMIT       — just approved, not yet disbursed (needs capital now)
    EXEMPT/blank/other — active loan (lien on receivables)

    The new SBA CSV format uses 'P I F' (with spaces) instead of 'PIF'.
    Strip spaces before comparing so both formats are handled.
    """
    normalized = (loan_status or "").upper().replace(" ", "")
    if normalized == "PIF":
        return "SBA_LOAN_PIF"
    if normalized == "COMMIT":
        return "SBA_LOAN_PENDING"
    return "SBA_LOAN_ACTIVE"


def _is_active_loan(
    loan_status: str | None,
    approval_date: date,
) -> bool:
    """True if the loan is currently active/pending or recent enough to matter.

    EXEMPT and COMMIT loans are always included regardless of age — they
    represent a currently active or pending loan. Everything else (PIF,
    blank) must be within the 7-year recency window. Fixed per John Cox
    Miller, Porter Capital, July 2026.
    """
    normalized = (loan_status or "").upper().replace(" ", "")
    if normalized in _ALWAYS_ACTIVE_STATUSES:
        return True
    return approval_date >= _MIN_APPROVAL_DATE


# ─── Cache management ─────────────────────────────────────────────────────────


def _get_cache_path() -> Path:
    raw = os.getenv("SBA_CACHE_PATH", _DEFAULT_CACHE_PATH)
    return Path(raw)


def _cache_is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    age_days = age_seconds / 86400
    expiry = _env_int("SBA_CACHE_EXPIRY_DAYS", _DEFAULT_CACHE_EXPIRY_DAYS)
    return age_days < expiry


def _download_csv(log: structlog.BoundLogger) -> None:
    """Download SBA bulk CSV to the cache file. Streams to avoid loading into memory."""
    cache = _get_cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    timeout = _env_float("SBA_DOWNLOAD_TIMEOUT", 120.0)

    log.info("sba_cache_downloading", url=_DOWNLOAD_URL, cache=str(cache))
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", _DOWNLOAD_URL) as resp:
            resp.raise_for_status()
            with cache.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    fh.write(chunk)

    log.info("sba_cache_refreshed", cache=str(cache), size_bytes=cache.stat().st_size)


def _ensure_cache(log: structlog.BoundLogger) -> Path:
    """Return path to a valid (fresh or just-downloaded) cache file.

    If the remote download 404s (the SBA portal has reorganized resource
    URLs before — see _DOWNLOAD_URL comment) and a stale cache already
    exists, serve the stale cache instead of failing the run. A source
    with no cache at all still raises, since there is nothing to serve.
    """
    path = _get_cache_path()
    if _cache_is_fresh(path):
        log.info("sba_cache_hit", cache=str(path))
        return path
    try:
        _download_csv(log)
    except httpx.HTTPStatusError as exc:
        if not path.exists():
            raise
        age_days = (time.time() - path.stat().st_mtime) / 86400
        log.warning(
            "sba_url_gone_using_stale_cache",
            url=_DOWNLOAD_URL,
            status_code=exc.response.status_code,
            cache=str(path),
            cache_age_days=round(age_days, 1),
        )
    return path


# ─── Connector ────────────────────────────────────────────────────────────────


class SBALoansConnector:
    """
    Reads SBA 7(a) FOIA bulk CSV and stores raw events for B2B loan recipients.

    Downloads and caches the 150 MB CSV for 30 days. Reads in chunks of 1000
    rows — never loads the full 373K-row file into memory.

    Usage:
        connector = SBALoansConnector(session, source_run, source)
        connector.run()   # never raises; errors recorded in source_run

    Local testing (PowerShell env vars before running):
        $env:SBA_LOANS_TEST_LIMIT="10000"
        → process only the first 10 000 rows of the CSV
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
        self.test_limit = _env_int("SBA_LOANS_TEST_LIMIT", 0)

        self._log = logger.bind(
            connector="sba_loans",
            source_run_id=str(source_run.id),
        )

    def run(self) -> None:
        """Process SBA CSV. Mark source_run completed or failed. Never raises."""
        # Windows stdout defaults to cp1252 which can't encode chars like Ⓡ (U+24C7).
        # Reconfigure once so structlog can write company names with special characters.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        try:
            self._process_csv()
            self.source_run.status = "completed"
        except Exception as exc:
            self.source_run.status = "failed"
            self.source_run.error_text = str(exc)
            self._log.error("sba_run_failed", error=str(exc))
        finally:
            self.source_run.finished_at = _utcnow()
            self.session.commit()

    # ── Private ───────────────────────────────────────────────────────────────

    def _process_csv(self) -> None:
        cache_file = _ensure_cache(self._log)

        rows_read = 0
        chunk: list[dict] = []

        with cache_file.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            for raw_row in reader:
                rows_read += 1
                chunk.append(raw_row)
                if len(chunk) >= _CHUNK_SIZE:
                    self._process_chunk(chunk)
                    chunk = []
                if self.test_limit > 0 and rows_read >= self.test_limit:
                    self._log.info("sba_test_limit_reached", limit=self.test_limit)
                    break
            if chunk:
                self._process_chunk(chunk)

        self._log.info(
            "sba_csv_processing_complete",
            rows_read=rows_read,
            records_fetched=self.source_run.records_fetched,
            records_valid=self.source_run.records_valid,
            records_skipped=self.source_run.records_skipped,
            quarantine_count=self.source_run.quarantine_count,
        )

    def _process_chunk(self, rows: list[dict]) -> None:
        for raw_row in rows:
            self._process_row(raw_row)

    def _process_row(self, raw_row: dict) -> None:
        self.source_run.records_fetched += 1

        try:
            record = SBALoanRecord.model_validate(raw_row)
        except Exception as exc:
            self.source_run.quarantine_count += 1
            self._log.warning(
                "sba_validation_failure",
                company=raw_row.get("borrname"),
                error=str(exc),
            )
            return

        # Loan amount filter — minimum only; the SBA 7(a) program's own $5M
        # cap makes an upper filter redundant.
        if record.gross_approval < _MIN_LOAN_AMOUNT:
            self.source_run.records_skipped += 1
            return

        # LoanStatus hard-skip — only defaulted or cancelled loans.
        if record.loan_status and record.loan_status.upper() in _SKIP_LOAN_STATUSES:
            self.source_run.records_skipped += 1
            return

        # Activeness filter — EXEMPT/COMMIT always included; everything else
        # must be within the 7-year recency window.
        if not _is_active_loan(record.loan_status, record.approval_date):
            self.source_run.records_skipped += 1
            return

        if record.naics_code is None:
            self._log.info("sba_blank_naics_included", company=record.borr_name)

        # NAICS and noise-keyword exclusions are soft-flags, not hard blocks —
        # the company might still be a legitimate B2B lead. John decides, not
        # this filter. Fixed per John Cox Miller, Porter Capital, July 2026.
        sector_excluded = False
        sector_excluded_reason: str | None = None
        if _is_excluded_naics(record.naics_code):
            sector_excluded = True
            sector_excluded_reason = f"Healthcare B2C (NAICS {record.naics_code})"
        elif record.naics_code and not _is_included_naics(record.naics_code):
            sector_excluded = True
            sector_excluded_reason = f"Non-B2B NAICS sector (NAICS {record.naics_code})"
        elif _has_noise_keyword(record.borr_name):
            sector_excluded = True
            sector_excluded_reason = "Noise keyword match in company name"

        signal_type = _signal_type_for_status(record.loan_status)
        if signal_type == "SBA_LOAN_PIF":
            status_note = "Paid in full — proven financing need, now scaling"
        elif signal_type == "SBA_LOAN_PENDING":
            status_note = "SBA loan just approved — company needs working capital now"
        else:
            status_note = "Active SBA loan — lien on receivables, needs qualification"

        amount_str = str(record.gross_approval)
        loan_status_display = record.loan_status or "Active"

        payload: dict = {
            "BorrName": record.borr_name,
            "BorrStreet": record.borr_street,
            "BorrCity": record.borr_city,
            "BorrState": record.borr_state,
            "BorrZip": record.borr_zip,
            "NaicsCode": record.naics_code,
            "NaicsDescription": record.naics_description,
            "GrossApproval": amount_str,
            "ApprovalDate": record.approval_date.isoformat(),
            "LoanStatus": record.loan_status or "",
            "JobsSupported": record.jobs_supported,
            "sector_excluded": sector_excluded,
            "sector_excluded_reason": sector_excluded_reason,
            "sba_signal_type": signal_type,
            "sba_status_note": status_note,
            "description": (
                f"SBA 7(a) loan of ${record.gross_approval:,.0f} approved "
                f"{record.approval_date}. Status: {loan_status_display}. "
                f"NAICS: {record.naics_code or 'N/A'} — "
                f"{record.naics_description or 'N/A'}"
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
            f"{record.borr_name}|{record.borr_state}|"
            f"{record.approval_date.isoformat()}|{amount_str}"
        )

        event = RawSourceEvent(
            source_id=self.source.id,
            source_run_id=self.source_run.id,
            source_record_id=source_record_id[:500],
            company_name_raw=record.borr_name,
            payload=payload,
            content_hash=content_hash,
            source_url=_SOURCE_URL,
        )
        self.session.add(event)
        self.source_run.records_valid += 1

        self._log.info(
            "sba_record_stored",
            company=record.borr_name,
            state=record.borr_state,
            amount=amount_str,
            signal_type=signal_type,
        )
