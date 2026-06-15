"""
Website contact extractor stub — Phase 1 skeleton.

Live HTTP fetching, robots.txt checking, and contact extraction are
not built yet. All calls return an empty WebsiteContactResult.
"""
from __future__ import annotations

import structlog

from app.enrichment.providers.base import WebsiteContactResult

logger = structlog.get_logger(__name__)


def extract_website_contacts(
    website_url: str,
    dry_run: bool = False,
) -> WebsiteContactResult:
    """Stub — returns empty result without making HTTP requests."""
    logger.info(
        "website_extractor_stub",
        url=website_url,
        dry_run=dry_run,
        note="live website extraction not implemented yet",
    )
    return WebsiteContactResult()
