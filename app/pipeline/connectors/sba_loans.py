"""
SBA 7(a) FOIA bulk loan connector — fetches loan records for B2B companies.

Downloads the SBA 7(a) FOIA bulk CSV from data.sba.gov.
Caches locally for 30 days to avoid repeated large downloads (~150 MB).
Reads the CSV in chunks of 1000 rows — never loads 373K rows into memory.

Filters applied:
  - State: AL, GA, TN, FL, MS, TX, VA (Porter's geographic ICP)
  - Loan amount: $50K – $5M
  - Approval date: 2022-01-01 or later
  - NAICS: all B2B sectors (excludes retail 44/45 and restaurants/hotels 72)
  - LoanStatus: skip CHGOFF (defaulted), CANCLD (cancelled), EXEMPT

Signal types produced:
  SBA_LOAN_PIF    — paid-in-full loan (company grew, repaid, now scaling)
  SBA_LOAN_ACTIVE — active loan (lien on receivables, needs qualification call)

Confirmed by John Cox Miller (Porter Capital SVP), June 25 2026:
  - Include ALL B2B industries, not just government contractors
  - Exclude only pure B2C: restaurants, hotels, retail stores
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

_DOWNLOAD_URL = (
    "https://data.sba.gov/dataset/7-a-504-foia/resource/"
    "aab00a57-b3e5-4b01-8e72-2b1c4c7e8e98/download/"
    "foia-7afy2020-fy2012present.csv"
)

_DEFAULT_CACHE_PATH = "data/sba_loans_cache.csv"
_DEFAULT_CACHE_EXPIRY_DAYS = 30
_CHUNK_SIZE = 1000

# Porter's geographic ICP for SBA leads
_TARGET_STATES = frozenset({"AL", "GA", "TN", "FL", "MS", "TX", "VA"})

# Loan amount bounds (inclusive)
_MIN_LOAN_AMOUNT = Decimal("50000")
_MAX_LOAN_AMOUNT = Decimal("5000000")

# Earliest approval date to include
_MIN_APPROVAL_DATE = date(2022, 1, 1)

# LoanStatus values to skip
_SKIP_LOAN_STATUSES = frozenset({"CHGOFF", "CANCLD", "EXEMPT"})

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

_SOURCE_URL = "https://data.sba.gov/dataset/7-a-504-foia"


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

    borr_name: str = Field(alias="BorrName")
    borr_state: str = Field(alias="BorrState")
    borr_city: str | None = Field(None, alias="BorrCity")
    borr_street: str | None = Field(None, alias="BorrStreet")
    borr_zip: str | None = Field(None, alias="BorrZip")
    naics_code: str | None = Field(None, alias="NaicsCode")
    naics_description: str | None = Field(None, alias="NaicsDescription")
    gross_approval: Decimal = Field(alias="GrossApproval")
    approval_date: date = Field(alias="ApprovalDate")
    loan_status: str | None = Field(None, alias="LoanStatus")
    jobs_supported: int | None = Field(None, alias="JobsSupported")

    @field_validator("borr_name", mode="before")
    @classmethod
    def validate_borr_name(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("BorrName must be a non-empty string")
        return v.strip()

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


def _signal_type_for_status(loan_status: str | None) -> str:
    """Map LoanStatus to signal type. PIF = paid-in-full (strongest signal)."""
    if loan_status and loan_status.upper() == "PIF":
        return "SBA_LOAN_PIF"
    return "SBA_LOAN_ACTIVE"


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
    """Return path to a valid (fresh or just-downloaded) cache file."""
    path = _get_cache_path()
    if _cache_is_fresh(path):
        log.info("sba_cache_hit", cache=str(path))
        return path
    _download_csv(log)
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
                company=raw_row.get("BorrName"),
                error=str(exc),
            )
            return

        # State filter
        if record.borr_state not in _TARGET_STATES:
            self.source_run.records_skipped += 1
            return

        # Loan amount filter
        if record.gross_approval < _MIN_LOAN_AMOUNT or record.gross_approval > _MAX_LOAN_AMOUNT:
            self.source_run.records_skipped += 1
            return

        # Approval date filter
        if record.approval_date < _MIN_APPROVAL_DATE:
            self.source_run.records_skipped += 1
            return

        # LoanStatus filter — skip defaulted, cancelled, exempt
        if record.loan_status and record.loan_status.upper() in _SKIP_LOAN_STATUSES:
            self.source_run.records_skipped += 1
            return

        # NAICS filter — include only B2B sectors
        if not _is_included_naics(record.naics_code):
            self.source_run.records_skipped += 1
            return

        signal_type = _signal_type_for_status(record.loan_status)
        if signal_type == "SBA_LOAN_PIF":
            status_note = "Paid in full — proven financing need, now scaling"
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
