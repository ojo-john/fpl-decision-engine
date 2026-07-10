"""Unit tests for packages.fpl_client.fingerprint: checksums and shape signatures."""

from packages.fpl_client.fingerprint import schema_signature, sha256_hex


def test_sha256_hex_is_deterministic():
    assert sha256_hex(b"hello") == sha256_hex(b"hello")


def test_sha256_hex_differs_for_different_content():
    assert sha256_hex(b"hello") != sha256_hex(b"world")


def test_sha256_hex_matches_known_digest():
    # sha256("hello") is a well-known digest.
    assert sha256_hex(b"hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_schema_signature_same_shape_same_signature():
    a = {"id": 1, "name": "Arsenal", "played": 0}
    b = {"id": 2, "name": "Chelsea", "played": 3}
    assert schema_signature(a) == schema_signature(b)


def test_schema_signature_changes_on_key_added():
    a = {"id": 1, "name": "Arsenal"}
    b = {"id": 1, "name": "Arsenal", "extra": True}
    assert schema_signature(a) != schema_signature(b)


def test_schema_signature_changes_on_value_type_change():
    a = {"now_cost": 55}
    b = {"now_cost": "55"}
    assert schema_signature(a) != schema_signature(b)


def test_schema_signature_list_uses_only_first_element():
    a = [{"id": 1, "name": "x"}, {"id": 2, "name": "y"}]
    b = [{"id": 1, "name": "x"}, {"totally": "different shape"}]
    assert schema_signature(a) == schema_signature(b)


def test_schema_signature_empty_list_is_stable():
    assert schema_signature([]) == schema_signature([])


def test_schema_signature_nested_dict_one_level():
    a = {"scoring": {"goals_scored": {"GKP": 10, "DEF": 6}}}
    b = {"scoring": {"goals_scored": {"GKP": 99, "DEF": 1}}}
    assert schema_signature(a) == schema_signature(b)
