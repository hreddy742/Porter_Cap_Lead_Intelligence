"""Seed the default Phase 1 scoring config.

Revision ID: 003_seed_default_scoring_config
Revises: 002_add_contactability

Fixes the NoActiveScoringConfigError-on-every-record bug that occurs on a
fresh deployment because nothing seeds scoring_configs.

What this inserts:
  - Deterministic UUID (00000000-0000-0000-0000-000000000001) so
    downgrade() can delete it by id without a table scan.
  - version_label = "phase1-v1"
  - active = True
  - config_hash = SHA-256 of the canonical JSON (sort_keys, no whitespace)
    so every lead_score row is traceable to the exact config that produced it.

Hot threshold rationale (70, not 75):
  Max achievable Phase 1 score is 73:
    porter_fit     25  (15 NAICS + 5 US country + 5 award in range)
    why_now        30  (freshness >= 0.8)
    ar_fit         10  (phase1_cap; raw would be 10 anyway)
    evidence_quality 8 (2+ items + source_url)
    contactability  0  (not built Phase 1)
    risk_clean      0  (not built Phase 1)
    ─────────────────
    total          73

  A threshold of 75 makes 'hot' structurally unreachable. Lowered to 70
  so that a company hitting all Phase 1 signals can actually score Hot.
"""

import hashlib
import json
import uuid as uuid_module

from alembic import op
import sqlalchemy as sa

revision = "003_seed_default_scoring_config"
down_revision = "002_add_contactability"
branch_labels = None
depends_on = None

_CONFIG_ID = uuid_module.UUID("00000000-0000-0000-0000-000000000001")

_CONFIG: dict = {
    "phase": "1",
    "tiers": {"hot": 70, "warm": 55, "cold": 35},
    "ar_heavy_naics": ["54", "56", "33", "48", "23", "62"],
    "components": {
        "porter_fit":       {"max": 25},
        "why_now":          {"max": 30},
        "ar_fit":           {"max": 15, "phase1_cap": 10},
        "contactability":   {"max": 10, "note": "always 0 in phase 1"},
        "risk_clean":       {"max": 10, "note": "always 0 in phase 1"},
        "evidence_quality": {"max": 10},
    },
}

_CONFIG_JSON: str = json.dumps(_CONFIG, sort_keys=True, separators=(",", ":"))
_CONFIG_HASH: str = hashlib.sha256(_CONFIG_JSON.encode()).hexdigest()

_NOTES = (
    "Default Phase 1 config. Hot threshold set to 70 — "
    "max achievable Phase 1 score is 73, threshold of 75 was structurally unreachable."
)


def upgrade() -> None:
    # INSERT only when no active config exists yet (idempotent for existing deployments).
    # A fresh deployment has an empty scoring_configs table and gets this row as its
    # first active config.  An existing deployment already has an active row and the
    # SELECT ... WHERE NOT EXISTS guard skips the insert cleanly.
    conn = op.get_bind()
    conn.execute(
        sa.text("""
            INSERT INTO scoring_configs
                (id, version_label, config, config_hash, active, notes, created_by, created_at)
            SELECT
                CAST(:id AS uuid),
                :version_label,
                CAST(:config AS jsonb),
                :config_hash,
                TRUE,
                :notes,
                :created_by,
                NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM scoring_configs WHERE active = TRUE
            )
        """),
        {
            "id": str(_CONFIG_ID),
            "version_label": "phase1-v1",
            "config": _CONFIG_JSON,
            "config_hash": _CONFIG_HASH,
            "notes": _NOTES,
            "created_by": "migration-003",
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM scoring_configs WHERE id = :id"),
        {"id": str(_CONFIG_ID)},
    )
