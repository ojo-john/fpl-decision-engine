"""Unit tests for app.rules.extraction.extract_rules_snapshot against the trimmed bootstrap fixture."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.rules.extraction import extract_rules_snapshot
from packages.domain.rules import RuleConfidence

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "fpl_api" / "bootstrap_static_sample.json"


@pytest.fixture(scope="module")
def bootstrap_json():
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture(scope="module")
def snapshot(bootstrap_json):
    return extract_rules_snapshot(bootstrap_json, fetched_at=datetime(2026, 1, 1, tzinfo=UTC))


def _field(snapshot, path):
    return next(f for f in snapshot.fields if f.path == path)


def test_squad_squadsize_confirmed(snapshot):
    field = _field(snapshot, "rules.squad_squadsize")
    assert field.value == 15
    assert field.confidence == RuleConfidence.CONFIRMED
    assert field.source == "bootstrap_static.game_config.rules"


def test_scoring_goals_scored_gkp(snapshot):
    field = _field(snapshot, "scoring.goals_scored.GKP")
    assert field.value == 10
    assert field.confidence == RuleConfidence.CONFIRMED


def test_scoring_goals_scored_per_position_flattened(snapshot):
    for position in ("GKP", "DEF", "MID", "FWD"):
        field = _field(snapshot, f"scoring.goals_scored.{position}")
        assert field.confidence == RuleConfidence.CONFIRMED


def test_informational_scoring_fields_have_notes(snapshot):
    for name in ("influence", "creativity", "threat", "ict_index", "bps"):
        field = _field(snapshot, f"scoring.{name}")
        assert field.notes is not None


def test_mng_fields_have_notes(snapshot):
    mng_fields = [f for f in snapshot.fields if f.path.startswith("scoring.mng_")]
    assert mng_fields
    assert all(f.notes is not None for f in mng_fields)


def test_hand_maintained_transfer_hit_cost_present_as_provisional(snapshot):
    field = _field(snapshot, "hand_maintained.transfer_hit_cost")
    assert field.value == -4
    assert field.confidence == RuleConfidence.PROVISIONAL
    assert field.source == "hand_maintained"


def test_hand_maintained_chip_effects_present(snapshot):
    for chip_name in ("wildcard", "freehit", "bboost", "3xc"):
        field = _field(snapshot, f"hand_maintained.chip_effect.{chip_name}")
        assert field.confidence == RuleConfidence.PROVISIONAL


def test_hand_maintained_captain_multiplier_present_as_provisional(snapshot):
    field = _field(snapshot, "hand_maintained.captain_multiplier")
    assert field.value == 2
    assert field.confidence == RuleConfidence.PROVISIONAL
    assert field.source == "hand_maintained"


def test_chips_flattened_with_windows(snapshot):
    wildcard_fields = {f.path for f in snapshot.fields if f.path.startswith("chips.wildcard.")}
    assert "chips.wildcard.window_1.start_event" in wildcard_fields
    assert "chips.wildcard.window_2.start_event" in wildcard_fields


def test_element_types_flattened(snapshot):
    field = _field(snapshot, "element_types.GKP.squad_select")
    assert field.value == 2
    assert field.confidence == RuleConfidence.CONFIRMED


def test_checksum_is_deterministic_across_runs(bootstrap_json):
    s1 = extract_rules_snapshot(bootstrap_json, fetched_at=datetime(2026, 1, 1, tzinfo=UTC))
    s2 = extract_rules_snapshot(bootstrap_json, fetched_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert s1.checksum_sha256 == s2.checksum_sha256


def test_raw_blobs_preserved_verbatim(bootstrap_json, snapshot):
    assert snapshot.raw_rules == bootstrap_json["game_config"]["rules"]
    assert snapshot.raw_scoring == bootstrap_json["game_config"]["scoring"]
    assert snapshot.raw_chips == bootstrap_json["chips"]
    assert snapshot.raw_element_types == bootstrap_json["element_types"]
