"""
Contactability-Lite provider interfaces and result models.

Pydantic v2 result models (frozen, validated at construction).
Protocol interfaces define the expected API for each provider.
Stubs in search.py and sam_gov.py implement these for Phase 1.
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, field_validator


class WebsiteResult(BaseModel):
    """Result from a search provider's website discovery query."""

    model_config = ConfigDict(frozen=True)

    url: str
    confidence: float  # 0.0–1.0
    source: str        # e.g. 'brave_search', 'serpapi'
    source_url: str    # the search result page URL

    @field_validator("confidence")
    @classmethod
    def confidence_in_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence must be 0.0–1.0, got {v}")
        return v


class SAMResult(BaseModel):
    """Result from a SAM.gov entity lookup."""

    model_config = ConfigDict(frozen=True)

    uei: str | None
    match_status: str          # 'matched' | 'not_found' | 'no_uei' | 'error' | 'not_implemented'
    registration_status: str | None = None
    address: str | None = None


class WebsiteContactResult(BaseModel):
    """Result from website contact extraction."""

    model_config = ConfigDict(frozen=True)

    contact_page_url: str | None = None
    phone: str | None = None
    phone_source_url: str | None = None
    generic_email: str | None = None
    email_source_url: str | None = None
    address: str | None = None
    robots_blocked: bool = False


class SearchProvider(Protocol):
    def find_company_website(
        self,
        company_name: str,
        state: str | None,
        dry_run: bool = False,
    ) -> WebsiteResult | None: ...


class SAMProvider(Protocol):
    def lookup_entity(
        self,
        uei: str | None,
        dry_run: bool = False,
    ) -> SAMResult: ...
