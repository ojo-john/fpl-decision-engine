"""Hand-maintained rules constants that fill a confirmed gap in bootstrap-static.

``https://fantasy.premierleague.com/help/rules`` is a client-rendered JS SPA
with no server-side rules content, so it is not scraped. bootstrap-static's
``game_config.rules``/``game_config.scoring`` (plus top-level ``chips``,
``element_types``, ``events``) is instead treated as the primary,
authoritative, machine-readable rules source. One confirmed gap: the
well-known "-4 points per transfer beyond free transfers" hit cost, and the
mechanical *effects* of each chip (their scheduling windows ARE in
``chips[]``, but not what they *do*), are not present anywhere in the
structured API data. These are tracked here as hand-maintained constants
with PROVISIONAL confidence — never treated as equally authoritative as
API-sourced fields.

Bump ``HAND_MAINTAINED_RULES_VERSION`` whenever an entry's value changes;
this also flows into the checksum computed in
``app.rules.extraction.extract_rules_snapshot`` so an edit here is always
visible as a rules-config version change.
"""

HAND_MAINTAINED_RULES_VERSION = 3

HAND_MAINTAINED_RULES: dict[str, dict] = {
    "hand_maintained.transfer_hit_cost": {
        "value": -4,
        "notes": "Points deducted per transfer beyond available free transfers. Not present "
        "anywhere in bootstrap-static game_config. PROVISIONAL until cross-validated "
        "against observed transfer/points deltas in a later milestone.",
    },
    "hand_maintained.chip_effect.wildcard": {
        "value": "Unlimited free transfers for the remainder of this window; no points deducted for "
        "transfers made.",
        "notes": "Chip scheduling windows ARE in bootstrap chips[]; this mechanical description is not.",
    },
    "hand_maintained.chip_effect.freehit": {
        "value": "Unlimited free transfers for this gameweek only; squad reverts automatically afterward.",
        "notes": "Chip scheduling windows ARE in bootstrap chips[]; this mechanical description is not.",
    },
    "hand_maintained.chip_effect.bboost": {
        "value": "All 15 squad players' points count this gameweek, not just the starting XI.",
        "notes": "Chip scheduling windows ARE in bootstrap chips[]; this mechanical description is not.",
    },
    "hand_maintained.chip_effect.3xc": {
        "value": "Captain's score is multiplied by 3 instead of the normal 2.",
        "notes": "Chip scheduling windows ARE in bootstrap chips[]; this mechanical description is not.",
    },
    "hand_maintained.captain_multiplier": {
        "value": 2,
        "notes": "Base captain score multiplier (2x total points for the captained starter). "
        "Not present anywhere in bootstrap-static game_config. Overridden to 3x by the "
        "'3xc' (Triple Captain) chip — see hand_maintained.chip_effect.3xc. PROVISIONAL "
        "until cross-validated against observed gameweek scoring in a later milestone.",
    },
    "hand_maintained.free_transfers_per_gameweek": {
        "value": 1,
        "notes": "1 free transfer accrues per gameweek (capped at 1 + rules.max_extra_free_transfers "
        "total banked); frozen (neither consumed nor accrued) during a wildcard/free-hit "
        "gameweek. Not present anywhere in bootstrap-static game_config. PROVISIONAL until "
        "cross-validated against observed transfer/points deltas in a later milestone.",
    },
}
