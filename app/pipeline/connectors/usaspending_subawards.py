"""
USASpending.gov subawards connector — second lead source.

Fetches FFATA subcontract records from POST /api/v2/subawards/.
Subcontractors are smaller companies doing real delivery work under a prime
federal contract. They invoice the prime on net-30 to net-90 terms while
paying their own workers and suppliers — a structural cash-flow gap that
A/R financing solves.

Key differences from the prime-award connector (usaspending.py):

  Endpoint:      /api/v2/subawards/   (not /search/spending_by_transaction/)
  Response keys: lowercase (recipient_name, amount, action_date)
  Sort:          "id" descending — action_date sort returns corrupted future
                 dates (year 6010, 2202) at the top; id is monotonically
                 increasing so id desc = most-recently reported first
  Filters:       NOT USED — the subawards endpoint accepts filter keys but
                 silently ignores them (verified by testing all documented
                 filter types in June 2026; revisit if USASpending fixes this)
  UEI:           NOT in the response — company resolution is name-only;
                 a warning is logged on every attempt (see resolution module)
  NAICS:         NOT in the response — industry filtering is handled by scoring
  Amount cap:    Quarantine records above $1 B — the raw data contains rows
                 with amounts like $39 trillion that are clearly corrupt
  Date guard:    Quarantine records with action_date year outside [2000, 2030]

Valid sort values (as of June 2026 API schema):
  id, subaward_number, description, action_date, amount, recipient_name

Env vars (all optional):
  USASPENDING_SUBAWARDS_PAGE_LIMIT     records per page   (default 100)
  USASPENDING_SUBAWARDS_MAX_PAGES      page cap           (default 200)
  USASPENDING_SUBAWARDS_TIMEOUT_SECONDS                   (default 30.0)
  USASPENDING_SUBAWARDS_MAX_RETRIES                       (default 3)
  USASPENDING_SUBAWARDS_BACKOFF_BASE_SECONDS              (default 2.0)
  USASPENDING_SUBAWARDS_BACKOFF_MAX_SECONDS               (default 60.0)
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

_ENDPOINT = "https://api.usaspending.gov/api/v2/subawards/"

# $1 B cap guards against data-corruption rows (e.g. $39-trillion radar antenna).
# Legitimate federal subcontracts in Porter's ICP are well below this.
_AMOUNT_MAX = Decimal("1000000000")

# Year bounds guard against corrupt action_date values (year 6010, 2202, etc.)
_DATE_YEAR_MIN = 2000
_DATE_YEAR_MAX = 2030

_RETRYABLE_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Records whose description matches a noise keyword are quarantined before DB write.
# These are social-service grants, not A/R-financing candidates.
_NOISE_KEYWORDS = frozenset({
    "childcare", "child care", "child development", "early learning",
    "head start", "daycare", "day care", "preschool", "pre-school",
    "after school", "afterschool", "foster care", "homeless shelter",
    "food bank", "food pantry", "nutrition program", "snap benefit",
    "wic program", "housing assistance", "rental assistance",
    "substance abuse", "mental health counseling", "drug treatment",
    "domestic violence", "senior center", "adult day care",
    "disability services", "special education",
})

# Records matching a signal keyword are annotated in payload["description_signal_keyword"].
# They still pass through — gates decide eligibility.
_SIGNAL_KEYWORDS = frozenset({
    "manufacturing", "fabrication", "assembly", "machining",
    "staffing", "temporary staffing", "labor services",
    "distribution", "logistics", "warehousing", "freight",
    "information technology", "software", "systems integration",
    "construction", "engineering services", "technical services",
    "maintenance", "repair", "overhaul", "equipment",
    "supplies", "materials", "components", "parts",
    "professional services", "consulting", "training",
})


class ConnectorError(Exception):
    """Raised when all retry attempts for a transient connector error are exhausted."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


class USASpendingSubawardsRecord(BaseModel):
    """Validated representation of one USASpending subaward record."""

    model_config = ConfigDict(populate_by_name=True)

    record_id: int = Field(alias="id")
    subaward_number: str | None = Field(None, alias="subaward_number")
    description: str | None = Field(None, alias="description")
    award_date: date = Field(alias="action_date")
    award_amount: Decimal = Field(alias="amount")
    recipient_name: str = Field(alias="recipient_name")

    @field_validator("recipient_name", mode="before")
    @classmethod
    def validate_recipient_name(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("recipient_name must be a non-empty string")
        return v.strip()

    @field_validator("award_amount", mode="before")
    @classmethod
    def validate_award_amount(cls, v: object) -> Decimal:
        try:
            amount = Decimal(str(v))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f"award_amount must be numeric, got {v!r}")
        if amount <= 0:
            raise ValueError(f"award_amount must be positive, got {amount}")
        if amount > _AMOUNT_MAX:
            raise ValueError(
                f"award_amount {amount} exceeds ${_AMOUNT_MAX:,} cap "
                "(data corruption guard — real subcontracts are below this)"
            )
        return amount

    @field_validator("award_date", mode="before")
    @classmethod
    def parse_award_date(cls, v: object) -> date:
        if isinstance(v, date):
            parsed = v
        elif isinstance(v, str):
            try:
                parsed = date.fromisoformat(v)
            except ValueError:
                raise ValueError(f"award_date must be an ISO date string, got {v!r}")
        else:
            raise ValueError(f"award_date expected str or date, got {type(v).__name__}")

        if not (_DATE_YEAR_MIN <= parsed.year <= _DATE_YEAR_MAX):
            raise ValueError(
                f"award_date year {parsed.year} outside valid range "
                f"[{_DATE_YEAR_MIN}, {_DATE_YEAR_MAX}] "
                "(data corruption guard — subawards endpoint has rows with year 6010)"
            )
        return parsed


# ─── Connector ────────────────────────────────────────────────────────────────


class USASpendingSubawardsConnector:
    """
    Fetches federal subcontract records from USASpending.gov.

    Paginates POST /api/v2/subawards/ sorted by id descending.
    Deduplicates by SHA-256(raw payload) against (source_id, content_hash).
    Any exception during fetch is caught: source_run marked failed, no re-raise.
    Pydantic validation failure increments quarantine_count and continues.

    Usage:
        connector = USASpendingSubawardsConnector(session, source_run, source)
        connector.run()   # never raises; all errors recorded in source_run

    Local testing (PowerShell env vars before running):
        $env:USASPENDING_SUBAWARDS_PAGE_LIMIT="10"
        $env:USASPENDING_SUBAWARDS_MAX_PAGES="1"
    """

    PAGE_LIMIT = 100

    def __init__(
        self,
        session: Session,
        source_run: SourceRun,
        source: SourceRegistry,
    ) -> None:
        self.session = session
        self.source_run = source_run
        self.source = source

        page_limit_raw = os.getenv("USASPENDING_SUBAWARDS_PAGE_LIMIT")
        max_pages_raw = os.getenv("USASPENDING_SUBAWARDS_MAX_PAGES")
        self.page_limit = int(page_limit_raw) if page_limit_raw else self.PAGE_LIMIT
        self.max_pages = int(max_pages_raw) if max_pages_raw else 200

        self.timeout = _env_float("USASPENDING_SUBAWARDS_TIMEOUT_SECONDS", 30.0)
        self.max_retries = _env_int("USASPENDING_SUBAWARDS_MAX_RETRIES", 3)
        self.backoff_base = _env_float("USASPENDING_SUBAWARDS_BACKOFF_BASE_SECONDS", 2.0)
        self.backoff_max = _env_float("USASPENDING_SUBAWARDS_BACKOFF_MAX_SECONDS", 60.0)

        self._log = logger.bind(
            connector="usaspending_subawards",
            source_run_id=str(source_run.id),
        )

    def run(self) -> None:
        """Fetch all pages. Mark source_run completed or failed. Never raises."""
        self._log.info(
            "usaspending_subawards_starting",
            page_limit=self.page_limit,
            max_pages=self.max_pages,
        )
        try:
            self._fetch_all_pages()
            self.source_run.status = "completed"
        except Exception as exc:
            self.source_run.status = "failed"
            self.source_run.error_text = str(exc)
            self._log.error("usaspending_subawards_run_failed", error=str(exc))
        finally:
            self.source_run.finished_at = _utcnow()
            self.session.commit()

    # ── Private ───────────────────────────────────────────────────────────────

    def _fetch_all_pages(self) -> None:
        page = 1
        with httpx.Client(timeout=self.timeout) as client:
            while True:
                results, has_next = self._fetch_page(client, page)
                self._log.info(
                    "usaspending_subawards_page_fetched",
                    page=page,
                    records=len(results),
                    has_next=has_next,
                )
                for raw in results:
                    self._process_record(raw)
                if not has_next:
                    break
                if self.max_pages is not None and page >= self.max_pages:
                    break
                page += 1

    def _fetch_page(self, client: httpx.Client, page: int) -> tuple[list[dict], bool]:
        body = {
            "filters": {},
            "limit": self.page_limit,
            "page": page,
            "sort": "id",
            "order": "desc",
        }
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                delay = min(
                    self.backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 1),
                    self.backoff_max,
                )
                self._log.warning(
                    "usaspending_subawards_retry",
                    attempt=attempt,
                    page=page,
                    delay_seconds=round(delay, 2),
                )
                time.sleep(delay)
            try:
                resp = client.post(_ENDPOINT, json=body)
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
                        "usaspending_subawards_client_error",
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
            f"USASpending subawards request failed after {self.max_retries + 1} attempts"
            f" on page {page}: {last_exc}"
        )

    def _process_record(self, raw: dict) -> None:
        self.source_run.records_fetched += 1

        try:
            record = USASpendingSubawardsRecord.model_validate(raw)
        except Exception as exc:
            self.source_run.quarantine_count += 1
            self._log.warning(
                "usaspending_subawards_validation_failure",
                record_id=raw.get("id"),
                recipient=raw.get("recipient_name"),
                error=str(exc),
            )
            return

        desc = (record.description or "").lower()

        for kw in _NOISE_KEYWORDS:
            if kw in desc:
                self.source_run.quarantine_count += 1
                self._log.info(
                    "subaward_quarantine_noise_keyword",
                    record_id=record.record_id,
                    recipient=record.recipient_name,
                    matched_keyword=kw,
                )
                return

        signal_keyword: str | None = None
        for kw in _SIGNAL_KEYWORDS:
            if kw in desc:
                signal_keyword = kw
                break

        content_hash = hashlib.sha256(
            json.dumps(raw, sort_keys=True, default=str).encode()
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

        payload: dict = dict(raw)
        if signal_keyword is not None:
            payload["description_signal_keyword"] = signal_keyword

        event = RawSourceEvent(
            source_id=self.source.id,
            source_run_id=self.source_run.id,
            source_record_id=str(record.record_id),
            company_name_raw=record.recipient_name,
            payload=payload,
            content_hash=content_hash,
            source_url=f"https://www.usaspending.gov/subaward/?id={record.record_id}",
        )
        self.session.add(event)
        self.source_run.records_valid += 1
