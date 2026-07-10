"""Shared rules-sync logic used by both the RULES_SYNC and DATA_INGESTION job handlers.

``DATA_INGESTION``'s handler reuses this exact function as one step of its
orchestration (rather than fetching bootstrap-static a second time);
``RULES_SYNC`` stays a separate, lighter-weight, manual-only job that calls
this same function after its own (single) bootstrap-static fetch.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.rules.diff import diff_rules_versions
from app.rules.extraction import extract_rules_snapshot
from app.rules.models import RuleField, RulesConfigSnapshot
from app.rules.report import render_rules_change_report_markdown
from app.rules.status import compute_rules_status
from packages.database.models.rules_config import RulesConfig
from packages.database.models.rules_config_field import RulesConfigField
from packages.domain.ingestion import EndpointHealth
from packages.domain.rules import RuleConfidence


def _load_current_rules_config(session: Session, season_id: uuid.UUID) -> RulesConfig | None:
    return session.execute(
        select(RulesConfig).where(RulesConfig.season_id == season_id, RulesConfig.is_current.is_(True))
    ).scalar_one_or_none()


def _reconstruct_snapshot(session: Session, current_row: RulesConfig) -> RulesConfigSnapshot:
    """Rebuild a RulesConfigSnapshot from a persisted rules_config row + its fields, for diffing."""
    field_rows = (
        session.execute(select(RulesConfigField).where(RulesConfigField.rules_config_id == current_row.id))
        .scalars()
        .all()
    )
    fields = [
        RuleField(
            path=row.field_path,
            value=row.value_json,
            confidence=RuleConfidence(row.confidence),
            source=row.source,
            notes=row.notes,
        )
        for row in field_rows
    ]
    return RulesConfigSnapshot(
        fetched_at=current_row.effective_from,
        checksum_sha256=current_row.checksum_sha256,
        fields=fields,
        raw_rules=current_row.raw_rules_json,
        raw_scoring=current_row.raw_scoring_json,
        raw_chips=current_row.raw_chips_json,
        raw_element_types=current_row.raw_element_types_json,
    )


def sync_rules(
    session: Session,
    bootstrap_json: dict[str, Any],
    fetched_at: datetime,
    ingestion_run_id: uuid.UUID,
    season_id: uuid.UUID,
    endpoint_health: EndpointHealth,
) -> dict[str, Any]:
    """Extract, diff, and (if changed) version the FPL rules configuration.

    Returns a dict with ``rules_config_id``, ``version_number``, ``changed``,
    ``rules_status``, and ``report_markdown`` (``None`` if unchanged) —
    intended to be embedded directly into the calling job handler's
    ``JobResult.output``.
    """
    snapshot = extract_rules_snapshot(bootstrap_json, fetched_at)

    current_row = _load_current_rules_config(session, season_id)

    if current_row is not None and current_row.checksum_sha256 == snapshot.checksum_sha256:
        current_row.last_confirmed_at = fetched_at
        session.flush()
        rules_status = compute_rules_status(snapshot, endpoint_health, current_row.last_confirmed_at)
        current_row.rules_status = rules_status
        session.flush()
        return {
            "rules_config_id": str(current_row.id),
            "version_number": current_row.version_number,
            "changed": False,
            "rules_status": rules_status.value,
            "report_markdown": None,
        }

    previous_snapshot = _reconstruct_snapshot(session, current_row) if current_row is not None else None
    diff = diff_rules_versions(previous_snapshot, snapshot)
    next_version_number = (current_row.version_number + 1) if current_row is not None else 1
    report_markdown = render_rules_change_report_markdown(diff, next_version_number)

    rules_status = compute_rules_status(snapshot, endpoint_health, last_confirmed_at=None)

    if current_row is not None:
        current_row.is_current = False
        current_row.superseded_at = fetched_at

    new_row = RulesConfig(
        season_id=season_id,
        version_number=next_version_number,
        source_ingestion_run_id=ingestion_run_id,
        checksum_sha256=snapshot.checksum_sha256,
        raw_rules_json=snapshot.raw_rules,
        raw_scoring_json=snapshot.raw_scoring,
        raw_chips_json=snapshot.raw_chips,
        raw_element_types_json=snapshot.raw_element_types,
        rules_status=rules_status,
        is_current=True,
        effective_from=fetched_at,
        superseded_at=None,
        last_confirmed_at=fetched_at,
        change_summary_markdown=report_markdown,
    )
    session.add(new_row)
    session.flush()

    session.bulk_save_objects(
        [
            RulesConfigField(
                rules_config_id=new_row.id,
                field_path=f.path,
                value_json=f.value,
                confidence=f.confidence,
                source=f.source,
                notes=f.notes,
            )
            for f in snapshot.fields
        ]
    )
    session.flush()

    return {
        "rules_config_id": str(new_row.id),
        "version_number": new_row.version_number,
        "changed": True,
        "rules_status": rules_status.value,
        "report_markdown": report_markdown,
    }
