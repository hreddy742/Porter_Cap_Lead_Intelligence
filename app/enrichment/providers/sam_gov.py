"""
SAM.gov entity lookup stub — Phase 1 skeleton.

Live SAM.gov API implementation is not built yet.
All calls return a 'not_implemented' status without making HTTP requests.
"""
from __future__ import annotations

import structlog

from app.enrichment.providers.base import SAMResult

logger = structlog.get_logger(__name__)


class SAMGovProvider:
    """Stub SAM.gov provider. Returns not_implemented for all queries."""

    def lookup_entity(
        self,
        uei: str | None,
        dry_run: bool = False,
    ) -> SAMResult:
        if uei is None:
            return SAMResult(uei=None, match_status="no_uei")
        logger.info(
            "sam_provider_stub",
            uei=uei,
            dry_run=dry_run,
            note="live SAM.gov lookup not implemented yet",
        )
        return SAMResult(uei=uei, match_status="not_implemented")
