"""
SBIR/STTR grants connector — federal R&D grant recipients as B2B leads.

Dual mode:
  API mode  (incremental, daily)  — api.www.sbir.gov public JSON API, current awards.
  Bulk mode (historical, monthly) — full award history CSV from sbir.gov/data-resources,
                                     cached locally for SBIR_BULK_CACHE_EXPIRY_DAYS.

Both modes normalize into the same field shape and share one filtering/storage
path (SBIRGrantsConnector._process_record), so a single content-hash computed
from (firm, state, award_year, award_amount) dedups awards seen by both modes.

A company that received an SBIR/STTR grant:
  - Is confirmed as a small B2B business
  - Is actively working on a government project (invoicing the government)
  - Is growing and may be cash-constrained
  - Is a strong A/R financing candidate (government receivables)

Confirmed by John Cox Miller, Porter Capital, July 2026.

API:  https://api.www.sbir.gov/public/api/awards (no key required)
Bulk: https://data.www.sbir.gov/mod_awarddatapublic_no_abstract/award_data_no_abstract.csv
      (full award_data.csv with abstracts is also public but 4x the size; abstracts
      are not part of Porter's scoring or contact-enrichment fields, so the smaller
      no-abstract file is the default. Override with SBIR_BULK_URL if needed.)

Filters applied (both modes; per John Cox Miller, Porter Capital, July 2026 —
collect everything, soft-flag rather than hard-block):
  - State: ALL 50 states (Porter is expanding nationally — geographic
    restriction removed)
  - Activeness: included if contract_end_date is in the future (project still
    active) OR award_year >= 2019 (7-year recency window)
  - Award amount: >= $50,000
  - Universities/colleges/research institutions/hospitals/associations are
    soft-flagged (sector_excluded=True), not hard-blocked. Hard block only
    fires for an exact match against a small list of unmistakable university
    names (e.g. "MIT") — Porter cannot factor those under any circumstance.
  - NAICS: SBIR has no NAICS field at all; every company has naics_code=None.
    This is expected — SAM.gov enrichment fills it in later.

Signal types produced:
  SBIR_GRANT — all phases produce this signal type.
  Signal strength depends on phase:
    Phase II, III → "strong" (larger grants, further into commercialization)
    Phase I       → "medium" (early-stage government relationship)

Contact enrichment (free, from both modes):
  poc_name, poc_title, poc_phone, poc_email, company_url, number_employees are
  extracted into the evidence payload for sales research — not used in scoring.

Env vars:
  SBIR_GRANTS_ENABLED         enable this connector via env (default false)
  SBIR_TEST_LIMIT             max records to process, all modes combined (0 = no limit)
  SBIR_API_ENABLED            run API incremental mode (default true — legacy behavior)
  SBIR_API_TIMEOUT            HTTP timeout in seconds (default 30.0)
  SBIR_PAGE_SIZE              records per API page (default 100, max 400)
  SBIR_BULK_ENABLED           run bulk historical mode (default false)
  SBIR_BULK_URL               override the bulk CSV download URL
  SBIR_BULK_CACHE_PATH        local cache file path (default data/sbir_awards_cache.csv)
  SBIR_BULK_CACHE_EXPIRY_DAYS cache TTL in days (default 30)
  SBIR_BULK_DOWNLOAD_TIMEOUT  HTTP download timeout in seconds (default 120.0)
"""

from __future__ import annotations

import csv
import hashlib
import os
import sys
import time
import re
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

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

# ─── Bulk mode constants ───────────────────────────────────────────────────────

_DEFAULT_BULK_URL = (
    "https://data.www.sbir.gov/mod_awarddatapublic_no_abstract/award_data_no_abstract.csv"
)
_BULK_SOURCE_URL = "https://www.sbir.gov/data-resources"
_DEFAULT_BULK_CACHE_PATH = "data/sbir_awards_cache.csv"
_DEFAULT_BULK_CACHE_EXPIRY_DAYS = 30
_DEFAULT_BULK_DOWNLOAD_TIMEOUT = 120.0
_BULK_CHUNK_SIZE = 1000

# Bulk CSV header (Title Case, spaced) -> API field name (snake_case).
# Both sources feed the same SBIRGrantRecord model once normalized.
_BULK_FIELD_MAP = {
    "Company": "firm",
    "Award Title": "award_title",
    "Agency": "agency",
    "Branch": "branch",
    "Phase": "phase",
    "Program": "program",
    "Award Year": "award_year",
    "Award Amount": "award_amount",
    "UEI": "uei",
    "Duns": "duns",
    "Number Employees": "number_employees",
    "Company Website": "company_url",
    "Address1": "address1",
    "Address2": "address2",
    "City": "city",
    "State": "state",
    "Zip": "zip",
    "Contact Name": "poc_name",
    "Contact Title": "poc_title",
    "Contact Phone": "poc_phone",
    "Contact Email": "poc_email",
    "Contract End Date": "contract_end_date",
}

# The bulk CSV spells out full state names ("Alabama"); the API uses 2-letter
# codes. Both are normalized to 2-letter codes in SBIRGrantRecord.validate_state.
_STATE_NAME_TO_ABBR = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "PUERTO RICO": "PR", "GUAM": "GU", "VIRGIN ISLANDS": "VI",
}

# All US states + DC + territories the bulk/API state field can report.
# Porter is expanding nationally — the old 7-state geographic ICP filter was
# removed per John Cox Miller, Porter Capital, July 2026.
_ALL_STATES = frozenset(_STATE_NAME_TO_ABBR.values())

# Earliest award year to include for awards with no future contract_end_date
# (7-year recency window). Fixed per John Cox Miller, Porter Capital, July 2026.
_MIN_AWARD_YEAR = 2019

# Minimum grant size (very small grants are likely pre-revenue)
_MIN_AWARD_AMOUNT = Decimal("50000")

# Company name tokens that indicate an academic, research, or non-factorable
# institutional recipient. Porter cannot factor universities, labs, hospitals,
# or member associations/foundations.
#
# "institute", "center for", and "foundation for" were removed — too broad,
# they collided with legitimate B2B names (e.g. "Center for Applied
# Engineering LLC"). Replaced with precise multi-word phrases, mirroring the
# OFAC gate's approach of using longer, less ambiguous match strings.
# Fixed per John Cox Miller, Porter Capital, July 2026.
_ACADEMIC_KEYWORDS = frozenset({
    "university",
    "college",
    "laboratory",
    "laboratories",
    "research foundation",
    "polytechnic",
    "technical college",
    "school of",
    "dept of",
    "department of",
    "national center",
    "medical center",
    "hospital",
    "health system",
    "association of",
    "academy of",
    "national institute of",
    "national center for",
    "national foundation for",
    "university of",
    "state university",
    "community college",
})

# Company names that are hard-blocked outright — unmistakable, non-factorable
# academic institutions. Exact match only (not substring) so legitimate
# companies whose name happens to contain one of these tokens are still
# soft-flagged via _ACADEMIC_KEYWORDS instead of hard-blocked.
_EXACT_ACADEMIC_NAMES = frozenset({
    "MIT",
    "CALTECH",
    "STANFORD UNIVERSITY",
    "HARVARD UNIVERSITY",
    "MASSACHUSETTS INSTITUTE OF TECHNOLOGY",
    "CALIFORNIA INSTITUTE OF TECHNOLOGY",
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


def _env_bool(var: str, default: bool) -> bool:
    raw = os.getenv(var)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() == "true"


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
    poc_name: str | None = None
    poc_title: str | None = None
    poc_phone: str | None = None
    poc_email: str | None = None
    company_url: str | None = None
    number_employees: int | None = None
    contract_end_date: date | None = None

    @field_validator("firm", mode="before")
    @classmethod
    def validate_firm(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("firm must be a non-empty string")
        return v.strip()

    @field_validator("state", mode="before")
    @classmethod
    def validate_state(cls, v: object) -> str:
        """Accept either a 2-letter code (API) or a full name (bulk CSV)."""
        if not isinstance(v, str) or not v.strip():
            raise ValueError("state must be a non-empty string")
        raw = v.strip()
        if len(raw) == 2:
            return raw.upper()
        abbr = _STATE_NAME_TO_ABBR.get(raw.upper())
        if abbr:
            return abbr
        raise ValueError(f"state must be a recognizable US state, got {raw!r}")

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
        # Round to whole dollars. The bulk CSV reports amounts like
        # "664827.0000" while the API reports "664827" for the same award —
        # normalizing here keeps cross-mode dedup (firm|state|year|amount) stable.
        return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    @field_validator("number_employees", mode="before")
    @classmethod
    def parse_number_employees(cls, v: object) -> int | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            return int(str(v).strip().split(".")[0])
        except (ValueError, TypeError):
            return None

    @field_validator("award_year", mode="before")
    @classmethod
    def validate_award_year(cls, v: object) -> int:
        if v is None:
            raise ValueError("award_year is required")
        try:
            return int(str(v).strip())
        except (ValueError, TypeError):
            raise ValueError(f"award_year must be an integer, got {v!r}")

    @field_validator("contract_end_date", mode="before")
    @classmethod
    def parse_contract_end_date(cls, v: object) -> date | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        if isinstance(v, date):
            return v
        if isinstance(v, str):
            try:
                return date.fromisoformat(v.strip())
            except ValueError:
                return None
        return None

    @field_validator("city", "zip", "address1", "address2", "agency", "branch",
                     "phase", "program", "award_title", "abstract", "uei", "duns",
                     "poc_name", "poc_title", "poc_phone", "poc_email", "company_url",
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
    but Porter Capital cannot factor academic institutions — this now soft-flags
    (sector_excluded) rather than hard-blocking; see _is_exact_academic_name for
    the small hard-block list. Word-boundary matching avoids false positives
    like "college" inside "Collegiate Sports LLC".
    """
    name_lower = company_name.lower()
    return any(
        re.search(rf"\b{re.escape(kw)}\b", name_lower) for kw in _ACADEMIC_KEYWORDS
    )


def _is_exact_academic_name(company_name: str) -> bool:
    """Return True only for an exact match against an unmistakable university name.

    Hard-blocks (records_skipped, not stored) — reserved for the handful of
    institutions Porter can never factor under any circumstance. Everything
    else caught by is_academic() is soft-flagged instead.
    """
    return company_name.strip().upper() in _EXACT_ACADEMIC_NAMES


def _is_active_sbir(award_year: int, contract_end_date: date | None) -> bool:
    """True if the SBIR project is still active or the award is recent.

    Include if EITHER contract_end_date is in the future (project still
    running) OR award_year is within the recency window. Mirrors the
    activeness rule used by the USASpending and SBA connectors. Fixed per
    John Cox Miller, Porter Capital, July 2026.
    """
    if contract_end_date is not None and contract_end_date >= date.today():
        return True
    return award_year >= _MIN_AWARD_YEAR


# Government email domains/markers seen in the SBIR "Contact" fields — these
# are the sponsoring agency's program officer, not the awarded company.
_GOV_CONTACT_EMAIL_MARKERS = (
    ".mil", ".gov", ".af.mil", ".army.mil", ".navy.mil",
    ".marines.mil", ".uscg.mil", "afwerx", "sbir@",
)


def _is_government_contact(email: str | None, phone: str | None) -> bool:
    """Return True if a contact's email/phone belongs to a government program
    officer rather than the awarded company (SBIR bulk data mixes the two).
    """
    if email:
        email_lower = email.lower()
        if any(marker in email_lower for marker in _GOV_CONTACT_EMAIL_MARKERS):
            return True
    if phone:
        # Placeholder numbers (e.g. 999-999-9999)
        if "9999999" in phone.replace("-", "").replace(" ", ""):
            return True
    return False


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


def _normalize_bulk_row(raw_row: dict) -> dict:
    """Map one bulk CSV row (Title Case headers) to the API's snake_case field names.

    Both the bulk CSV and the live API feed the same SBIRGrantRecord model once
    normalized, so filtering/storage logic in the connector runs identically.
    """
    return {
        norm_key: raw_row.get(csv_key)
        for csv_key, norm_key in _BULK_FIELD_MAP.items()
    }


# ─── Bulk cache management ────────────────────────────────────────────────────


def _get_bulk_url() -> str:
    return os.getenv("SBIR_BULK_URL") or _DEFAULT_BULK_URL


def _get_bulk_cache_path() -> Path:
    raw = os.getenv("SBIR_BULK_CACHE_PATH", _DEFAULT_BULK_CACHE_PATH)
    return Path(raw)


def _bulk_cache_is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    age_days = age_seconds / 86400
    expiry = _env_int("SBIR_BULK_CACHE_EXPIRY_DAYS", _DEFAULT_BULK_CACHE_EXPIRY_DAYS)
    return age_days < expiry


def _download_bulk_csv(log: structlog.BoundLogger) -> None:
    """Download the SBIR bulk award CSV to the cache file. Streams to avoid loading into memory."""
    cache = _get_bulk_cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    timeout = _env_float("SBIR_BULK_DOWNLOAD_TIMEOUT", _DEFAULT_BULK_DOWNLOAD_TIMEOUT)
    url = _get_bulk_url()

    log.info("sbir_bulk_cache_downloading", url=url, cache=str(cache))
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with cache.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    fh.write(chunk)

    log.info("sbir_bulk_cache_refreshed", cache=str(cache), size_bytes=cache.stat().st_size)


def _ensure_bulk_cache(log: structlog.BoundLogger) -> Path:
    """Return path to a valid (fresh or just-downloaded) bulk cache file."""
    path = _get_bulk_cache_path()
    if _bulk_cache_is_fresh(path):
        log.info("sbir_bulk_cache_hit", cache=str(path))
        return path
    _download_bulk_csv(log)
    return path


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
    Fetches SBIR/STTR award records and stores raw events for B2B companies
    in Porter's target states. Two independent modes feed the same filtering
    and storage path (_process_record):

      API mode  — api.www.sbir.gov, paginated state-by-state, current awards.
                  Default ON (SBIR_API_ENABLED defaults to true) — this is the
                  connector's original/legacy behavior.
      Bulk mode — full award history CSV, cached locally for 30 days.
                  Default OFF (SBIR_BULK_ENABLED) — opt in for the one-time
                  historical backfill, then run monthly to refresh the cache.

    Usage:
        connector = SBIRGrantsConnector(session, source_run, source)
        connector.run()   # never raises; errors recorded in source_run

    Local testing (PowerShell env vars before running):
        $env:SBIR_BULK_ENABLED="true"
        $env:SBIR_TEST_LIMIT="1000"
        → bulk mode only, first 1000 records processed
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
        # API mode defaults ON to preserve this connector's original behavior.
        # Bulk mode is an explicit opt-in (one-time historical backfill).
        self._api_enabled = _env_bool("SBIR_API_ENABLED", True)
        self._bulk_enabled = _env_bool("SBIR_BULK_ENABLED", False)

        self._log = logger.bind(
            connector="sbir_grants",
            source_run_id=str(source_run.id),
        )

    def run(self) -> None:
        """Run enabled modes. Mark source_run completed or failed. Never raises."""
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

        try:
            if self._bulk_enabled:
                self._run_bulk()

            if self._api_enabled:
                try:
                    self._run_api()
                except ConnectorError as exc:
                    # API 429/maintenance is expected — skip gracefully and let
                    # the next scheduled run retry. Any bulk-mode records already
                    # stored above are unaffected.
                    self.source_run.error_text = f"API mode skipped: {exc}"
                    self._log.warning("sbir_api_mode_skipped", error=str(exc))

            self.source_run.status = "completed"
        except Exception as exc:
            self.source_run.status = "failed"
            self.source_run.error_text = str(exc)
            self._log.error("sbir_run_unexpected_error", error=str(exc))
        finally:
            self.source_run.finished_at = _utcnow()
            self.session.commit()

    # ── API mode ──────────────────────────────────────────────────────────────

    def _run_api(self) -> None:
        """Loop over all US states and paginate through current awards."""
        for state in sorted(_ALL_STATES):
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
                self._process_record(raw_record, mode="api")

            if len(batch) < self._page_size:
                break

            start += self._page_size
            time.sleep(_PAGE_DELAY)

    # ── Bulk mode ─────────────────────────────────────────────────────────────

    def _run_bulk(self) -> None:
        """Download (or reuse cached) full award history CSV and process every row."""
        cache_file = _ensure_bulk_cache(self._log)

        rows_read = 0
        with cache_file.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            for raw_row in reader:
                if self.test_limit > 0 and self.source_run.records_fetched >= self.test_limit:
                    self._log.info("sbir_test_limit_reached", limit=self.test_limit)
                    break
                rows_read += 1
                self._process_record(raw_row, mode="bulk")

        self._log.info(
            "sbir_bulk_processing_complete",
            rows_read=rows_read,
            records_fetched=self.source_run.records_fetched,
            records_valid=self.source_run.records_valid,
            records_skipped=self.source_run.records_skipped,
            quarantine_count=self.source_run.quarantine_count,
        )

    # ── Shared processing path ───────────────────────────────────────────────

    def _process_record(self, raw_record: dict, mode: str) -> bool:
        """Validate, filter, and store one award. Shared by API and bulk modes.

        Returns True if a new RawSourceEvent was stored, False otherwise.
        """
        self.source_run.records_fetched += 1

        normalized = _normalize_bulk_row(raw_record) if mode == "bulk" else raw_record

        try:
            record = SBIRGrantRecord.model_validate(normalized)
        except Exception as exc:
            self.source_run.quarantine_count += 1
            self._log.warning(
                "sbir_validation_failure",
                company=normalized.get("firm"),
                mode=mode,
                error=str(exc),
            )
            return False

        # Activeness filter — replaces the flat award_year cutoff. Included if
        # the project is still active (contract_end_date in the future) or the
        # award is within the recency window.
        if not _is_active_sbir(record.award_year, record.contract_end_date):
            self.source_run.records_skipped += 1
            return False

        # Amount filter — very small grants are unlikely A/R candidates
        if record.award_amount < _MIN_AWARD_AMOUNT:
            self.source_run.records_skipped += 1
            return False

        # Hard block only unmistakable, non-factorable university names.
        if _is_exact_academic_name(record.firm):
            self.source_run.records_skipped += 1
            self._log.info(
                "sbir_exact_academic_hard_blocked",
                company=record.firm,
                state=record.state,
                mode=mode,
            )
            return False

        # Institution soft-flag — universities, labs, hospitals, etc. are still
        # collected but hidden from sales by default. John decides, not this
        # filter. Fixed per John Cox Miller, Porter Capital, July 2026.
        sector_excluded = False
        sector_excluded_reason: str | None = None
        if is_academic(record.firm):
            sector_excluded = True
            sector_excluded_reason = "Academic institution"
            self._log.info(
                "sbir_academic_soft_flagged",
                company=record.firm,
                state=record.state,
                mode=mode,
            )

        strength = signal_strength_for_phase(record.phase)
        phase_display = record.phase or "Unknown Phase"
        program_display = record.program or "SBIR"

        poc_name, poc_title, poc_phone, poc_email = (
            record.poc_name, record.poc_title, record.poc_phone, record.poc_email,
        )
        if _is_government_contact(record.poc_email, record.poc_phone):
            self._log.info(
                "sbir_government_contact_filtered",
                company=record.firm,
                email=record.poc_email,
                phone=record.poc_phone,
                mode=mode,
            )
            poc_name = poc_title = poc_phone = poc_email = None

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
            "poc_name": poc_name,
            "poc_title": poc_title,
            "poc_phone": poc_phone,
            "poc_email": poc_email,
            "company_url": record.company_url,
            "number_employees": record.number_employees,
            "sbir_signal_type": "SBIR_GRANT",
            "sbir_signal_strength": strength,
            "sbir_source_mode": mode,
            "sector_excluded": sector_excluded,
            "sector_excluded_reason": sector_excluded_reason,
            "description": (
                f"{program_display} {phase_display} grant of "
                f"${record.award_amount:,.0f} from "
                f"{record.agency or 'federal agency'}"
                + (f" ({record.branch})" if record.branch else "")
                + f" for: {(record.award_title or 'N/A')[:100]}"
            ),
        }

        # Dedup key is the award's identity (firm|state|year|amount), not the
        # full payload — bulk and API rows for the same award can differ in
        # field completeness (e.g. abstract, contact fields), but the identity
        # is the same. This is what lets the two modes dedup against each other.
        identity_key = (
            f"{record.firm.strip().lower()}|{record.state}|"
            f"{record.award_year}|{record.award_amount}"
        )
        content_hash = hashlib.sha256(identity_key.encode()).hexdigest()

        existing = self.session.execute(
            select(RawSourceEvent).where(
                RawSourceEvent.source_id == self.source.id,
                RawSourceEvent.content_hash == content_hash,
            )
        ).scalar_one_or_none()

        if existing is not None:
            self.source_run.records_skipped += 1
            return False

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
            source_url=_SOURCE_URL if mode == "api" else _BULK_SOURCE_URL,
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
            mode=mode,
        )
        return True
