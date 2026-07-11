"""Unit tests for app.ingestion.normalization pure helpers."""

from decimal import Decimal

from app.ingestion.normalization import _parse_decimal


def test_parse_decimal_none_is_none():
    assert _parse_decimal(None) is None


def test_parse_decimal_empty_string_is_none():
    assert _parse_decimal("") is None


def test_parse_decimal_parses_string_number():
    assert _parse_decimal("4.4") == Decimal("4.4")


def test_parse_decimal_parses_numeric_value():
    assert _parse_decimal(8.8) == Decimal("8.8")
