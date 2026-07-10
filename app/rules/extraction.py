"""Pure extraction of a RulesConfigSnapshot from a bootstrap-static payload.

No I/O here — this module only transforms an already-fetched
``bootstrap-static`` JSON payload into a flattened, sourced, confidence-tagged
set of ``RuleField``s.
"""

import json
from datetime import datetime
from typing import Any

from app.rules.constants import HAND_MAINTAINED_RULES, HAND_MAINTAINED_RULES_VERSION
from app.rules.models import RuleField, RulesConfigSnapshot
from packages.domain.rules import RuleConfidence
from packages.fpl_client.fingerprint import sha256_hex

# Scoring fields that are currently zero-weighted / informational-only in
# classic FPL (not real scoring inputs today, even though the API reports
# them) — surfaced with an explanatory note rather than silently dropped.
_INFORMATIONAL_SCORING_FIELDS = {"influence", "creativity", "threat", "ict_index", "bps"}

_INFORMATIONAL_NOTE = (
    "Reported by bootstrap-static but not a real scoring input in classic FPL today "
    "(currently zero-weighted / informational only)."
)
_MNG_MODE_NOTE = "Assistant-manager mode field, not applicable to classic FPL squads."


def _rules_source() -> str:
    return "bootstrap_static.game_config.rules"


def _scoring_source() -> str:
    return "bootstrap_static.game_config.scoring"


def _flatten_rules(raw_rules: dict[str, Any]) -> list[RuleField]:
    fields: list[RuleField] = []
    for key, value in raw_rules.items():
        fields.append(
            RuleField(
                path=f"rules.{key}",
                value=value,
                confidence=RuleConfidence.CONFIRMED,
                source=_rules_source(),
            )
        )
    return fields


def _scoring_note(key: str) -> str | None:
    if key.startswith("mng_"):
        return _MNG_MODE_NOTE
    if key in _INFORMATIONAL_SCORING_FIELDS:
        return _INFORMATIONAL_NOTE
    return None


def _flatten_scoring(raw_scoring: dict[str, Any]) -> list[RuleField]:
    fields: list[RuleField] = []
    for key, value in raw_scoring.items():
        note = _scoring_note(key)
        if isinstance(value, dict):
            # Per-position breakdown (e.g. goals_scored: {GKP: 10, DEF: 6, ...}).
            for position, position_value in value.items():
                fields.append(
                    RuleField(
                        path=f"scoring.{key}.{position}",
                        value=position_value,
                        confidence=RuleConfidence.CONFIRMED,
                        source=_scoring_source(),
                        notes=note,
                    )
                )
        else:
            fields.append(
                RuleField(
                    path=f"scoring.{key}",
                    value=value,
                    confidence=RuleConfidence.CONFIRMED,
                    source=_scoring_source(),
                    notes=note,
                )
            )
    return fields


def _flatten_chips(raw_chips: list[dict[str, Any]]) -> list[RuleField]:
    fields: list[RuleField] = []
    window_counters: dict[str, int] = {}
    for chip in raw_chips:
        name = chip["name"]
        window_counters[name] = window_counters.get(name, 0) + 1
        window = window_counters[name]
        prefix = f"chips.{name}.window_{window}"
        for attr in ("start_event", "stop_event", "chip_type"):
            fields.append(
                RuleField(
                    path=f"{prefix}.{attr}",
                    value=chip.get(attr),
                    confidence=RuleConfidence.CONFIRMED,
                    source="bootstrap_static.chips",
                )
            )
    return fields


def _flatten_element_types(raw_element_types: list[dict[str, Any]]) -> list[RuleField]:
    fields: list[RuleField] = []
    for element_type in raw_element_types:
        short_name = element_type["singular_name_short"]
        prefix = f"element_types.{short_name}"
        for attr in ("squad_select", "squad_min_play", "squad_max_play"):
            fields.append(
                RuleField(
                    path=f"{prefix}.{attr}",
                    value=element_type.get(attr),
                    confidence=RuleConfidence.CONFIRMED,
                    source="bootstrap_static.element_types",
                )
            )
    return fields


def _hand_maintained_fields() -> list[RuleField]:
    return [
        RuleField(
            path=path,
            value=entry["value"],
            confidence=RuleConfidence.PROVISIONAL,
            source="hand_maintained",
            notes=entry.get("notes"),
        )
        for path, entry in HAND_MAINTAINED_RULES.items()
    ]


def _compute_checksum(raw_rules: dict, raw_scoring: dict, raw_chips: list, raw_element_types: list) -> str:
    payload = json.dumps(
        {
            "rules": raw_rules,
            "scoring": raw_scoring,
            "chips": raw_chips,
            "element_types": raw_element_types,
            "hand_maintained": HAND_MAINTAINED_RULES,
            "hand_maintained_version": HAND_MAINTAINED_RULES_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256_hex(payload)


def extract_rules_snapshot(bootstrap_json: dict[str, Any], fetched_at: datetime) -> RulesConfigSnapshot:
    """Extract a flattened, sourced, confidence-tagged snapshot of the FPL rules config.

    Pure function — no I/O. ``bootstrap_json`` is the already-fetched, parsed
    ``bootstrap-static`` response body.
    """
    game_config = bootstrap_json["game_config"]
    raw_rules: dict = game_config["rules"]
    raw_scoring: dict = game_config["scoring"]
    raw_chips: list = bootstrap_json["chips"]
    raw_element_types: list = bootstrap_json["element_types"]

    fields: list[RuleField] = []
    fields.extend(_flatten_rules(raw_rules))
    fields.extend(_flatten_scoring(raw_scoring))
    fields.extend(_flatten_chips(raw_chips))
    fields.extend(_flatten_element_types(raw_element_types))
    fields.extend(_hand_maintained_fields())

    checksum = _compute_checksum(raw_rules, raw_scoring, raw_chips, raw_element_types)

    return RulesConfigSnapshot(
        fetched_at=fetched_at,
        checksum_sha256=checksum,
        fields=fields,
        raw_rules=raw_rules,
        raw_scoring=raw_scoring,
        raw_chips=raw_chips,
        raw_element_types=raw_element_types,
    )
