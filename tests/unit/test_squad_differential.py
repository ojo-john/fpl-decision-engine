"""Unit tests for app.h2h.differential.compute_squad_differential (pure, no DB/network)."""

import uuid

from app.h2h.differential import compute_squad_differential

P1, P2, P3, P4, P5 = (uuid.uuid4() for _ in range(5))


def test_compute_squad_differential_returns_players_unique_to_each_side():
    result = compute_squad_differential([P1, P2, P3], [P2, P3, P4])

    assert result.only_first == frozenset({P1})
    assert result.only_second == frozenset({P4})


def test_compute_squad_differential_identical_squads_yields_no_differential():
    result = compute_squad_differential([P1, P2, P3], [P3, P2, P1])

    assert result.only_first == frozenset()
    assert result.only_second == frozenset()


def test_compute_squad_differential_disjoint_squads_yields_everything_on_each_side():
    result = compute_squad_differential([P1, P2], [P3, P4, P5])

    assert result.only_first == frozenset({P1, P2})
    assert result.only_second == frozenset({P3, P4, P5})


def test_compute_squad_differential_handles_empty_first_squad():
    result = compute_squad_differential([], [P1, P2])

    assert result.only_first == frozenset()
    assert result.only_second == frozenset({P1, P2})


def test_compute_squad_differential_handles_both_squads_empty():
    result = compute_squad_differential([], [])

    assert result.only_first == frozenset()
    assert result.only_second == frozenset()
