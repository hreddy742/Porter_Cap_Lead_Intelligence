"""
USASpending.gov connector — fetches federal contract award records.

Paginates POST /api/v2/search/spending_by_award/ until hasNext=False.
Deduplicates by SHA-256(raw payload) against (source_id, content_hash).
Any exception during fetch is caught: source_run marked failed, no re-raise.
Pydantic validation failure increments quarantine_count and continues.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RawSourceEvent, SourceRegistry, SourceRun

logger = structlog.get_logger(__name__)

_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Award Amount",
    "Start Date",
    "Recipient UEI",
    "NAICS Code",
    "Place of Performance State Code",
    "Awarding Agency",
]

_AWARD_TYPE_CODES = ["A", "B", "C", "D"]  # contracts only


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _current_fiscal_year() -> int:
    """US fiscal year starts Oct 1. Month ≥ 10 means we are already in next FY."""
    now = _utcnow()
    return now.year + 1 if now.month >= 10 else now.year


def _fiscal_year_range(fy: int) -> tuple[str, str]:
    """Return ISO (start_date, end_date) for a US fiscal year."""
    return f"{fy - 1}-10-01", f"{fy}-09-30"


# ─── Pydantic v2 record model ─────────────────────────────────────────────────


class USASpendingRecord(BaseModel):
    """Validated representation of one USASpending contract award."""

    model_config = ConfigDict(populate_by_name=True)

    award_id: str = Field(alias="Award ID")
    recipient_name: str = Field(alias="Recipient Name")
    award_amount: Decimal = Field(alias="Award Amount")
    award_date: date = Field(alias="Start Date")
    recipient_uei: str | None = Field(None, alias="Recipient UEI")
    naics_code: str | None = Field(None, alias="NAICS Code")
    state_code: str | None = Field(None, alias="Place of Performance State Code")
    awarding_agency: str | None = Field(None, alias="Awarding Agency")

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
    Fetches federal contract awards from USASpending.gov and stores raw events.

    Usage:
        connector = USASpendingConnector(session, source_run, source)
        connector.run()   # never raises; all errors recorded in source_run
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
        with httpx.Client(timeout=30.0) as client:
            while True:
                results, has_next = self._fetch_page(client, page)
                for raw in results:
                    self._process_record(raw)
                if not has_next:
                    break
                page += 1

    def _fetch_page(self, client: httpx.Client, page: int) -> tuple[list[dict], bool]:
        start_date, end_date = _fiscal_year_range(self.fiscal_year)
        body = {
            "filters": {
                "award_type_codes": _AWARD_TYPE_CODES,
                "time_period": [{"start_date": start_date, "end_date": end_date}],
            },
            "fields": _FIELDS,
            "page": page,
            "limit": self.PAGE_LIMIT,
            "sort": "Award Amount",
            "order": "desc",
        }
        resp = client.post(f"{self.BASE_URL}/search/spending_by_award/", json=body)
        resp.raise_for_status()
        data = resp.json()
        results: list[dict] = data.get("results", [])
        has_next: bool = bool(data.get("page_metadata", {}).get("hasNext", False))
        return results, has_next

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

        event = RawSourceEvent(
            source_id=self.source.id,
            source_run_id=self.source_run.id,
            source_record_id=record.award_id,
            company_name_raw=record.recipient_name,
            payload=raw,
            content_hash=content_hash,
            source_url=f"https://www.usaspending.gov/award/{record.award_id}/",
        )
        self.session.add(event)
        self.source_run.records_valid += 1
