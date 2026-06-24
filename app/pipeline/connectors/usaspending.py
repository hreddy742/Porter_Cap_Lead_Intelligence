"""
USASpending.gov connector — fetches federal contract award records.

Paginates POST /api/v2/search/spending_by_transaction/ until hasNext=False.
Deduplicates by SHA-256(raw payload) against (source_id, content_hash).
Any exception during fetch is caught: source_run marked failed, no re-raise.
Pydantic validation failure increments quarantine_count and continues.

Endpoint: spending_by_transaction (not spending_by_award)
  Reason: the transaction endpoint populates naics_code/naics_description in the
  response. The award endpoint returns NAICS Code = null for most DoD contracts,
  which prevents NAICS-based scoring. The transaction endpoint also exposes
  Action Date (real obligation date) rather than the period-of-performance Start
  Date, which can be years in the future and produces misleading freshness scores.

Field name differences from spending_by_award:
  "Transaction Amount"  (was "Award Amount")
  "Action Date"         (was "Start Date")
  "naics_code"          lowercase (was "NAICS Code" — always null)
  "naics_description"   lowercase (new)
  "pop_state_code"      (was "Place of Performance State Code")
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RawSourceEvent, SourceRegistry, SourceRun

logger = structlog.get_logger(__name__)

# Fields to request from spending_by_transaction.
# Field names are endpoint-specific — do not mix with spending_by_award names.
# generated_internal_id is the USASpending unique key used in award detail URLs
# (e.g. CONT_AWD_FA880321D0002_9700_-NONE-_-NONE-). "Award ID" is just the PIID
# and cannot be used as a URL path parameter — it redirects to the homepage.
_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Transaction Amount",
    "Action Date",
    "Recipient UEI",
    "naics_code",
    "naics_description",
    "pop_state_code",
    "Awarding Agency",
    "generated_internal_id",
]

_AWARD_TYPE_CODES = ["A", "B", "C", "D"]  # contracts only (excludes grants/loans)

# ICP sectors confirmed by John Cox Miller,
# Porter Capital, June 24 2026.
# Soft-flagged excluded sectors (11,22,23,52,61,
# 62,71,92) are not targeted here but are stored
# if found via other signals.
_TARGET_NAICS_PREFIXES = frozenset({
    # Original ICP sectors
    "31", "32", "33",  # Manufacturing
    "42",              # Wholesale Trade
    "48", "49",        # Transportation and Warehousing
    "54",              # Professional/Technical Services
    "56",              # Administrative/Staffing Services

    # New sectors confirmed by John Cox Miller June 24 2026
    "21",              # Mining
    "44", "45",        # Retail Trade
    "51",              # Information
    "55",              # Management of Companies
    "72",              # Accommodation and Food Services
    "81",              # Other Services
})

_RETRYABLE_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class ConnectorError(Exception):
    """Raised when all retry attempts for a transient connector error are exhausted."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _current_fiscal_year() -> int:
    """US fiscal year starts Oct 1. Month ≥ 10 means we are already in next FY."""
    now = _utcnow()
    return now.year + 1 if now.month >= 10 else now.year


def _fiscal_year_range(fy: int) -> tuple[str, str]:
    """Return ISO (start_date, end_date) for a US fiscal year."""
    return f"{fy - 1}-10-01", f"{fy}-09-30"


def _env_float(var: str, default: float) -> float:
    raw = os.getenv(var)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{var}={raw!r} is not a valid number")


def _env_int(var: str, default: int) -> int:
    raw = os.getenv(var)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{var}={raw!r} is not a valid integer")


# ─── Pydantic v2 record model ─────────────────────────────────────────────────


class USASpendingRecord(BaseModel):
    """Validated representation of one USASpending contract transaction."""

    model_config = ConfigDict(populate_by_name=True)

    award_id: str = Field(alias="Award ID")
    recipient_name: str = Field(alias="Recipient Name")
    award_amount: Decimal = Field(alias="Transaction Amount")
    award_date: date = Field(alias="Action Date")
    recipient_uei: str | None = Field(None, alias="Recipient UEI")
    naics_code: str | None = Field(None, alias="naics_code")
    naics_description: str | None = Field(None, alias="naics_description")
    state_code: str | None = Field(None, alias="pop_state_code")
    awarding_agency: str | None = Field(None, alias="Awarding Agency")
    generated_internal_id: str | None = Field(None)
    action_type: str | None = Field(None, alias="Action Type")
    action_type_description: str | None = Field(None, alias="Action Type Description")

    @field_validator("award_id", mode="before")
    @classmethod
    def validate_award_id(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("award_id must be a non-empty string")
        return v.strip()

    @field_validator("recipient_name", mode="before")
    @classmethod
    def validate_recipient_name(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("recipient_name must be a non-empty string")
        stripped = v.strip()
        if any(kw in stripped.lower() for kw in {"domestic awardees", "undisclosed"}):
            raise ValueError(f"placeholder recipient name: {stripped}")
        return stripped

    @field_validator("award_amount", mode="before")
    @classmethod
    def validate_award_amount(cls, v: object) -> Decimal:
        try:
            amount = Decimal(str(v))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f"award_amount must be numeric, got {v!r}")
        if amount <= 0:
            raise ValueError(f"award_amount must be positive, got {amount}")
        return amount

    @field_validator("award_date", mode="before")
    @classmethod
    def parse_award_date(cls, v: object) -> date:
        if isinstance(v, date):
            return v
        if isinstance(v, str):
            try:
                return date.fromisoformat(v)
            except ValueError:
                raise ValueError(f"award_date must be an ISO date string, got {v!r}")
        raise ValueError(f"award_date expected str or date, got {type(v).__name__}")

    @field_validator("recipient_uei", mode="before")
    @classmethod
    def clean_uei(cls, v: object) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            return None
        stripped = v.strip()
        return stripped or None


# ─── Connector ────────────────────────────────────────────────────────────────


class USASpendingConnector:
    """
    Fetches federal contract transactions from USASpending.gov and stores raw events.

    Uses the spending_by_transaction endpoint filtered to AR-heavy NAICS sectors
    so that response records have naics_code populated (unlike spending_by_award
    which returns NAICS Code = null for most DoD contracts).

    Usage:
        connector = USASpendingConnector(session, source_run, source)
        connector.run()   # never raises; all errors recorded in source_run

    Default behavior:
        - 100 records per page
        - fetch all pages until hasNext=False

    Local testing (PowerShell env vars before running):
        $env:USASPENDING_PAGE_LIMIT="10"
        $env:USASPENDING_MAX_PAGES="1"
        → 10 records per page, stop after 1 page (max 10 records total)
    """

    BASE_URL = "https://api.usaspending.gov/api/v2"
    PAGE_LIMIT = 100

    def __init__(
        self,
        session: Session,
        source_run: SourceRun,
        source: SourceRegistry,
        *,
        fiscal_year: int | None = None,
    ) -> None:
        self.session = session
        self.source_run = source_run
        self.source = source
        self.fiscal_year = fiscal_year or _current_fiscal_year()

        page_limit_raw = os.getenv("USASPENDING_PAGE_LIMIT")
        max_pages_raw = os.getenv("USASPENDING_MAX_PAGES")
        self.page_limit = int(page_limit_raw) if page_limit_raw else self.PAGE_LIMIT
        # Default to 200 pages (20 000 records) to avoid long-running pulls
        # that hit server-side disconnects on page 250+. Override via env var.
        self.max_pages = int(max_pages_raw) if max_pages_raw else 200

        self.timeout = _env_float("USASPENDING_TIMEOUT_SECONDS", 30.0)
        self.max_retries = _env_int("USASPENDING_MAX_RETRIES", 3)
        self.backoff_base = _env_float("USASPENDING_BACKOFF_BASE_SECONDS", 2.0)
        self.backoff_max = _env_float("USASPENDING_BACKOFF_MAX_SECONDS", 60.0)

        self._log = logger.bind(
            connector="usaspending",
            source_run_id=str(source_run.id),
        )

    def run(self) -> None:
        """Fetch all award pages. Mark source_run completed or failed. Never raises."""
        try:
            self._fetch_all_pages()
            self.source_run.status = "completed"
        except Exception as exc:
            self.source_run.status = "failed"
            self.source_run.error_text = str(exc)
            self._log.error("usaspending_run_failed", error=str(exc))
        finally:
            self.source_run.finished_at = _utcnow()
            self.session.commit()

    # ── Private ───────────────────────────────────────────────────────────────

    def _fetch_all_pages(self) -> None:
        page = 1
        with httpx.Client(timeout=self.timeout) as client:
            while True:
                results, has_next = self._fetch_page(client, page)
                for raw in results:
                    self._process_record(raw)
                if not has_next:
                    break
                if self.max_pages is not None and page >= self.max_pages:
                    break
                page += 1

    def _fetch_page(self, client: httpx.Client, page: int) -> tuple[list[dict], bool]:
        start_date, end_date = _fiscal_year_range(self.fiscal_year)
        body = {
            "filters": {
                "award_type_codes": _AWARD_TYPE_CODES,
                "time_period": [{"start_date": start_date, "end_date": end_date}],
                "naics_codes": sorted(_TARGET_NAICS_PREFIXES),
            },
            "fields": _FIELDS,
            "page": page,
            "limit": self.page_limit,
            "sort": "Action Date",
            "order": "desc",
        }
        url = f"{self.BASE_URL}/search/spending_by_transaction/"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                delay = min(
                    self.backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 1),
                    self.backoff_max,
                )
                self._log.warning(
                    "usaspending_retry",
                    attempt=attempt,
                    page=page,
                    delay_seconds=round(delay, 2),
                )
                time.sleep(delay)
            try:
                resp = client.post(url, json=body)
                if resp.status_code in _RETRYABLE_HTTP_STATUS_CODES:
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                    continue
                if resp.status_code >= 400:
                    try:
                        error_snippet = resp.text[:500]
                    except Exception:
                        error_snippet = "<unreadable>"
                    self._log.error(
                        "usaspending_client_error",
                        status_code=resp.status_code,
                        response_snippet=error_snippet,
                        page=page,
                    )
                resp.raise_for_status()
                data = resp.json()
                results: list[dict] = data.get("results", [])
                has_next: bool = bool(data.get("page_metadata", {}).get("hasNext", False))
                return results, has_next
            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
            except httpx.HTTPStatusError:
                raise  # permanent client error — do not retry

        raise ConnectorError(
            f"USASpending request failed after {self.max_retries + 1} attempts"
            f" on page {page}: {last_exc}"
        )

    def _process_record(self, raw: dict) -> None:
        # Count every record received from the API regardless of outcome
        self.source_run.records_fetched += 1

        # Validate with Pydantic v2
        try:
            record = USASpendingRecord.model_validate(raw)
        except Exception as exc:
            self.source_run.quarantine_count += 1
            self._log.warning(
                "usaspending_validation_failure",
                award_id=raw.get("Award ID"),
                error=str(exc),
            )
            return

        # Deterministic content hash of the raw payload
        content_hash = hashlib.sha256(
            json.dumps(raw, sort_keys=True, default=str).encode()
        ).hexdigest()

        # Dedup: skip if this (source, hash) pair already exists
        existing = self.session.execute(
            select(RawSourceEvent).where(
                RawSourceEvent.source_id == self.source.id,
                RawSourceEvent.content_hash == content_hash,
            )
        ).scalar_one_or_none()

        if existing is not None:
            self.source_run.records_skipped += 1
            return

        # generated_internal_id is the slug USASpending uses in its own award URLs.
        # award_id is just the PIID and does not resolve as a URL path parameter.
        url_key = record.generated_internal_id or record.award_id
        event = RawSourceEvent(
            source_id=self.source.id,
            source_run_id=self.source_run.id,
            source_record_id=record.award_id,
            company_name_raw=record.recipient_name,
            payload=raw,
            content_hash=content_hash,
            source_url=f"https://www.usaspending.gov/award/{url_key}/",
        )
        self.session.add(event)
        self.source_run.records_valid += 1
