"""
SAM.gov Entity Management API provider — live UEI entity validation.

Looks up a company by UEI against the SAM.gov Entity Management API (v3) and
returns a SAMResult with registration status and physical address.

SAM.gov is an entity-validation source only — it confirms a UEI maps to a real,
registered federal entity. It is NOT expected to provide sales contacts.

Safety contract (the batch must never crash because of one bad lookup):
  * No UEI                       → match_status="no_uei"   (no HTTP call)
  * dry_run                      → no HTTP call
  * SAM_GOV_API_KEY missing      → match_status="error"    (no HTTP call)
  * No entity match              → match_status="not_found"
  * 429 / 5xx                    → retry with exponential backoff + jitter
  * 400 / 401 / 403              → match_status="error", no retry
  * timeout / transport / parse  → match_status="error"

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

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Address parts pulled from coreData.physicalAddress, in display order.
_ADDRESS_FIELDS = (
    "addressLine1",
    "addressLine2",
    "city",
    "stateOrProvinceCode",
    "zipCode",
    "countryCode",
)


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
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max

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

        with httpx.Client(timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                if attempt > 0:
                    self._sleep_backoff(attempt)
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
                    continue

                status = resp.status_code
                if status in _RETRYABLE_STATUS:
                    last_status = status
                    logger.warning(
                        "sam_lookup_retryable_status",
                        uei=uei,
                        status_code=status,
                        attempt=attempt,
                    )
                    continue
                if status >= 400:
                    # 400/401/403 and any other client/server error we don't retry:
                    # return a clear error status without re-trying endlessly.
                    logger.error(
                        "sam_lookup_client_error",
                        uei=uei,
                        status_code=status,
                    )
                    return SAMResult(uei=uei, match_status="error")

                return self._parse_response(uei, resp.json())

        logger.error(
            "sam_lookup_exhausted",
            uei=uei,
            last_status=str(last_status),
            attempts=self.max_retries + 1,
        )
        return SAMResult(uei=uei, match_status="error")

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(
            self.backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 1),
            self.backoff_max,
        )
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
