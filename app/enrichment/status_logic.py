"""
Contactability status and score computation — pure functions, no DB or network.

compute_contactability_score: returns an integer 0–10.
compute_contactability_status: returns one of the four allowed status strings.

Status escalation:
  not_contactable       — nothing found from any free source
  needs_paid_enrichment — SAM match confirms company is real but no website found
  partially_contactable — website found but no contact details
  contactable           — website + at least one contact detail (phone/page/email)
"""
from __future__ import annotations

_VALID_STATUSES = frozenset(
    {"contactable", "partially_contactable", "not_contactable", "needs_paid_enrichment"}
)

# Score weights — total max is 10 without clamping
_W_WEBSITE = 4
_W_CONTACT_PAGE = 2
_W_PHONE = 2
_W_EMAIL = 1
_W_SAM_MATCH = 1


def compute_contactability_score(
    official_website: str | None,
    contact_page_url: str | None,
    phone: str | None,
    generic_email: str | None,
    sam_match_status: str | None,
) -> int:
    """Return a contactability score 0–10.

    Weights: website=4, contact_page=2, phone=2, email=1, sam_matched=1.
    Score is clamped to [0, 10].
    """
    score = 0
    if official_website:
        score += _W_WEBSITE
    if contact_page_url:
        score += _W_CONTACT_PAGE
    if phone:
        score += _W_PHONE
    if generic_email:
        score += _W_EMAIL
    if sam_match_status == "matched":
        score += _W_SAM_MATCH
    return min(max(score, 0), 10)


def compute_contactability_status(
    official_website: str | None,
    contact_page_url: str | None,
    phone: str | None,
    generic_email: str | None,
    sam_match_status: str | None,
) -> str:
    """Return the contactability status string.

    contactable           — website + any contact detail
    partially_contactable — website only (no contact), or SAM match but no website
    needs_paid_enrichment — SAM confirmed real company but no website or contact found
    not_contactable       — nothing found
    """
    has_website = bool(official_website)
    has_contact = bool(contact_page_url or phone or generic_email)
    has_sam = sam_match_status == "matched"

    if has_website and has_contact:
        return "contactable"
    if has_website:
        return "partially_contactable"
    if has_sam:
        return "needs_paid_enrichment"
    return "not_contactable"
