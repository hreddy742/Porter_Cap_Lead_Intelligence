"""
SAM.gov Entity Management API provider — live UEI entity validation.

Looks up a company by UEI against the SAM.gov Entity Management API (v3) and
returns a SAMResult with registration status and physical address.

SAM.gov is an entity-validation source only — it confirms a UEI maps to a real,
registered federal entity. It is NOT expected to provide sales contacts.

Safety contract (the batch must never crash because of one bad lookup):
  * No UEI                       → match_status="no_uei"        (no HTTP call)
  * dry_run                      → no HTTP call
  * SAM_GOV_API_KEY missing      → match_status="error"         (no HTTP call)
  * No entity match              → match_status="not_found"
  * 429 exhausted                → match_status="rate_limited"  (provider throttle)
  * 5xx / timeout / transport    → retry, then match_status="error" if exhausted
  * 400 / 401 / 403              → match_status="error", no retry
  * parse error                  → match_status="error"

A throttle (429) or transport failure is a PROVIDER failure, not evidence that
the company does not exist. It is reported as "rate_limited" / "error" so the
orchestrator can count it as a failure rather than silently storing the company
as not_contactable.

Rate limiting:
  * Outbound calls are paced to at most SAM_RATE_LIMIT_PER_MINUTE requests/minute
    (conservative default below) to avoid tripping the server-side throttle.
  * On a 429 we honour the Retry-After header when present, otherwise we back off
    more aggressively than for a 5xx so we stop hammering a throttled endpoint.

The API key is read from SAM_GOV_API_KEY and is NEVER logged.
"""
from __future__ import annotations

import os
import random
import time

import httpx
import structlog

from app.enrichment.providers.base import SAMResult

logger = structlog.get_logger(__name__)

# 429 is handled on its own path (Retry-After + longer backoff); these are the
# transient server errors that get the ordinary exponential backoff.
_RETRYABLE_5XX = frozenset({500, 502, 503, 504})

# Conservative outbound cap. SAM.gov throttles aggressively; default low and let
# operators raise it via SAM_RATE_LIMIT_PER_MINUTE if their key allows more.
_DEFAULT_RATE_LIMIT_PER_MINUTE = 30
# Address parts pulled from coreData.physicalAddress, in display order.
_ADDRESS_FIELDS = (
    "addressLine1",
    "addressLine2",
    "city",
    "stateOrProvinceCode",
    "zipCode",
    "countryCode",
)


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to default on missing/invalid."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("sam_env_int_invalid", name=name, value=raw, default=default)
        return default


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header expressed as integer seconds.

    Only the delta-seconds form is supported; an HTTP-date form (or anything
    unparseable) returns None so the caller falls back to exponential backoff.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class SAMGovProvider:
    """Live SAM.gov Entity Management API provider (UEI lookup only)."""

    BASE_URL = "https://api.sam.gov/entity-information/v3/entities"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        rate_limit_per_minute: int | None = None,
        rate_429_backoff_base: float = 5.0,
        rate_429_backoff_max: float = 120.0,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        # 429 backoff is deliberately slower and capped higher than 5xx backoff:
        # a throttle means "stop hammering me", so we wait longer between tries.
        self.rate_429_backoff_base = rate_429_backoff_base
        self.rate_429_backoff_max = rate_429_backoff_max

        if rate_limit_per_minute is None:
            rate_limit_per_minute = _env_int(
                "SAM_RATE_LIMIT_PER_MINUTE", _DEFAULT_RATE_LIMIT_PER_MINUTE
            )
        self.rate_limit_per_minute = max(1, rate_limit_per_minute)
        self._min_interval = 60.0 / self.rate_limit_per_minute
        # Monotonic timestamp of the last outbound request; paces consecutive
        # calls (across lookups) so the whole batch respects the per-minute cap.
        self._last_request_at: float | None = None

    def lookup_entity(
        self,
        uei: str | None,
        dry_run: bool = False,
    ) -> SAMResult:
        cleaned = (uei or "").strip()
        if not cleaned:
            return SAMResult(uei=None, match_status="no_uei")

        if dry_run:
            logger.info("sam_lookup_skipped_dry_run", uei=cleaned)
            return SAMResult(uei=cleaned, match_status="error")

        api_key = os.getenv("SAM_GOV_API_KEY")
        if not api_key:
            logger.error("sam_api_key_missing", uei=cleaned)
            return SAMResult(uei=cleaned, match_status="error")

        try:
            return self._lookup_live(cleaned, api_key)
        except Exception as exc:  # final safety net — never crash the batch
            logger.error("sam_lookup_unexpected_error", uei=cleaned, error=str(exc))
            return SAMResult(uei=cleaned, match_status="error")

    # ── Private ───────────────────────────────────────────────────────────────

    def _lookup_live(self, uei: str, api_key: str) -> SAMResult:
        # api_key travels in the query string but is never logged.
        params = {"api_key": api_key, "ueiSAM": uei}
        headers = {"Accept": "application/json"}
        last_status: object = None
        saw_429 = False

        with httpx.Client(timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                self._throttle()
                try:
                    resp = client.get(self.BASE_URL, params=params, headers=headers)
                except (
                    httpx.TimeoutException,
                    httpx.ConnectError,
                    httpx.RemoteProtocolError,
                ) as exc:
                    last_status = type(exc).__name__
                    logger.warning(
                        "sam_lookup_transport_error",
                        uei=uei,
                        error=type(exc).__name__,
                        attempt=attempt,
                    )
                    if attempt < self.max_retries:
                        self._sleep_backoff(attempt + 1)
                    continue

                status = resp.status_code
                if status == 429:
                    saw_429 = True
                    last_status = 429
                    logger.warning(
                        "sam_lookup_rate_limited",
                        uei=uei,
                        attempt=attempt,
                    )
                    if attempt < self.max_retries:
                        self._sleep_after_429(attempt + 1, resp.headers.get("Retry-After"))
                    continue
                if status in _RETRYABLE_5XX:
                    last_status = status
                    logger.warning(
                        "sam_lookup_retryable_status",
                        uei=uei,
                        status_code=status,
                        attempt=attempt,
                    )
                    if attempt < self.max_retries:
                        self._sleep_backoff(attempt + 1)
                    continue
                if status >= 400:
                    # 400/401/403 and any other client error we don't retry:
                    # return a clear error status without re-trying endlessly.
                    logger.error(
                        "sam_lookup_client_error",
                        uei=uei,
                        status_code=status,
                    )
                    return SAMResult(uei=uei, match_status="error")

                return self._parse_response(uei, resp.json())

        # Retries exhausted. A throttle is reported distinctly from other errors
        # so the orchestrator never mistakes "we got throttled" for "no match".
        if saw_429:
            logger.error(
                "sam_lookup_rate_limited_exhausted",
                uei=uei,
                attempts=self.max_retries + 1,
            )
            return SAMResult(uei=uei, match_status="rate_limited")

        logger.error(
            "sam_lookup_exhausted",
            uei=uei,
            last_status=str(last_status),
            attempts=self.max_retries + 1,
        )
        return SAMResult(uei=uei, match_status="error")

    def _throttle(self) -> None:
        """Pace outbound requests to the configured per-minute cap."""
        if self._min_interval <= 0:
            return
        if self._last_request_at is not None:
            wait = self._min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(
            self.backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 1),
            self.backoff_max,
        )
        time.sleep(delay)

    def _sleep_after_429(self, attempt: int, retry_after: str | None) -> None:
        """Back off after a 429: honour Retry-After if given, else slow backoff."""
        delay = _parse_retry_after(retry_after)
        if delay is None:
            delay = self.rate_429_backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 1)
        delay = min(delay, self.rate_429_backoff_max)
        logger.warning("sam_lookup_429_backoff", delay_seconds=round(delay, 2))
        time.sleep(delay)

    def _parse_response(self, uei: str, data: dict) -> SAMResult:
        entities = data.get("entityData") or []
        if not entities:
            return SAMResult(uei=uei, match_status="not_found")

        entity = entities[0]
        registration = entity.get("entityRegistration") or {}
        core = entity.get("coreData") or {}

        matched_uei = registration.get("ueiSAM") or uei
        registration_status = registration.get("registrationStatus")
        address = self._format_address(core.get("physicalAddress") or {})

        return SAMResult(
            uei=matched_uei,
            match_status="matched",
            registration_status=registration_status,
            address=address,
        )

    @staticmethod
    def _format_address(physical: dict) -> str | None:
        parts = [str(physical[f]).strip() for f in _ADDRESS_FIELDS if physical.get(f)]
        return ", ".join(parts) if parts else None
