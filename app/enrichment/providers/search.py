"""
Search provider stub — Phase 1 skeleton.

Live Brave Search / SerpAPI implementation is not built yet.
All calls return None without making HTTP requests.
"""
from __future__ import annotations

import structlog

from app.enrichment.providers.base import WebsiteResult

logger = structlog.get_logger(__name__)


class BraveSearchProvider:
    """Stub search provider. Returns None for all queries (no HTTP calls)."""

    def find_company_website(
        self,
        company_name: str,
        state: str | None,
        dry_run: bool = False,
    ) -> WebsiteResult | None:
        logger.info(
            "search_provider_stub",
            company_name=company_name,
            state=state,
            dry_run=dry_run,
            note="live search not implemented yet",
        )
        return None
