"""
Tests for app/enrichment/status_logic.py

Pure function tests — no DB, no network, no fixtures required.

Coverage:
  1.  not_contactable when nothing found
  2.  partially_contactable — website only
  3.  contactable — website + phone
  4.  contactable — website + contact page
  5.  contactable — website + email
  6.  needs_paid_enrichment — SAM matched, no website
  7.  partially_contactable — website + SAM not found
  8.  score zero when nothing found
  9.  score website only (4)
  10. score website + contact page (6)
  11. score website + phone (6)
  12. score website + phone + contact page (8)
  13. score all fields (10)
  14. score clamped at 10
  15. sam not_found does not add score points
  16. sam matched adds one point
  17. email alone scores 1
  18. not_contactable when sam not_found and no website
"""
from __future__ import annotations

import pytest

from app.enrichment.status_logic import compute_contactability_score, compute_contactability_status


# ── Status tests ──────────────────────────────────────────────────────────────

def test_not_contactable_when_nothing():
    assert compute_contactability_status(None, None, None, None, None) == "not_contactable"


def test_not_contactable_sam_not_found():
    assert compute_contactability_status(None, None, None, None, "not_found") == "not_contactable"


def test_not_contactable_sam_no_uei():
    assert compute_contactability_status(None, None, None, None, "no_uei") == "not_contactable"


def test_not_contactable_sam_error():
    assert compute_contactability_status(None, None, None, None, "error") == "not_contactable"


def test_partially_contactable_website_only():
    assert compute_contactability_status("https://acme.com", None, None, None, None) == "partially_contactable"


def test_partially_contactable_website_sam_not_found():
    assert compute_contactability_status("https://acme.com", None, None, None, "not_found") == "partially_contactable"


def test_contactable_website_and_phone():
    assert compute_contactability_status("https://acme.com", None, "555-1234", None, None) == "contactable"


def test_contactable_website_and_contact_page():
    assert compute_contactability_status("https://acme.com", "https://acme.com/contact", None, None, None) == "contactable"


def test_contactable_website_and_email():
    assert compute_contactability_status("https://acme.com", None, None, "info@acme.com", None) == "contactable"


def test_contactable_website_all_contacts_and_sam():
    assert compute_contactability_status(
        "https://acme.com", "https://acme.com/contact", "555-1234", "info@acme.com", "matched"
    ) == "contactable"


def test_needs_paid_enrichment_sam_matched_no_website():
    assert compute_contactability_status(None, None, None, None, "matched") == "needs_paid_enrichment"


def test_needs_paid_enrichment_sam_matched_no_website_no_contact():
    assert compute_contactability_status(None, None, None, None, "matched") == "needs_paid_enrichment"


def test_contactable_website_with_sam_match():
    assert compute_contactability_status("https://acme.com", "https://acme.com/contact", None, None, "matched") == "contactable"


# ── Score tests ───────────────────────────────────────────────────────────────

def test_score_zero_nothing():
    assert compute_contactability_score(None, None, None, None, None) == 0


def test_score_website_only():
    assert compute_contactability_score("https://acme.com", None, None, None, None) == 4


def test_score_website_plus_contact_page():
    assert compute_contactability_score("https://acme.com", "https://acme.com/contact", None, None, None) == 6


def test_score_website_plus_phone():
    assert compute_contactability_score("https://acme.com", None, "555-1234", None, None) == 6


def test_score_website_plus_contact_plus_phone():
    assert compute_contactability_score("https://acme.com", "https://acme.com/contact", "555-1234", None, None) == 8


def test_score_all_fields():
    score = compute_contactability_score(
        "https://acme.com", "https://acme.com/contact", "555-1234", "info@acme.com", "matched"
    )
    assert score == 10


def test_score_clamped_at_10():
    score = compute_contactability_score(
        "https://acme.com", "https://acme.com/contact", "555-1234", "info@acme.com", "matched"
    )
    assert score <= 10


def test_score_sam_matched_adds_one():
    without_sam = compute_contactability_score("https://acme.com", None, None, None, None)
    with_sam = compute_contactability_score("https://acme.com", None, None, None, "matched")
    assert with_sam == without_sam + 1


def test_score_sam_not_found_no_points():
    score_none = compute_contactability_score(None, None, None, None, None)
    score_not_found = compute_contactability_score(None, None, None, None, "not_found")
    assert score_not_found == score_none == 0


def test_score_email_alone():
    assert compute_contactability_score(None, None, None, "info@acme.com", None) == 1


def test_score_sam_match_alone():
    assert compute_contactability_score(None, None, None, None, "matched") == 1


def test_score_never_negative():
    assert compute_contactability_score(None, None, None, None, None) >= 0
