"""Content fingerprinting helpers: exact checksums and cheap shape signatures."""

import hashlib
import json
from typing import Any


def sha256_hex(raw_bytes: bytes) -> str:
    """Return the hex-encoded SHA-256 digest of ``raw_bytes``."""
    return hashlib.sha256(raw_bytes).hexdigest()


def _shape(obj: Any) -> Any:
    """Recursively describe the *shape* of ``obj`` (one level deep for dict values)."""
    if isinstance(obj, dict):
        return {key: type(obj[key]).__name__ for key in sorted(obj.keys())}
    if isinstance(obj, list):
        return type(obj).__name__
    return type(obj).__name__


def schema_signature(obj: Any) -> str:
    """Return a SHA-256 hex digest describing the *shape* of ``obj``, not its values.

    For a dict: the sorted list of keys plus each value's type name (nested
    dicts are described one level deep only). For a list: only the first
    element's shape is considered — these FPL endpoints return large,
    homogeneous arrays (hundreds of players, dozens of fixtures), so
    comparing every element would add real cost for no additional signal.
    This means a schema change confined to element N>0 of a list, with
    element 0 unaffected, will not be detected by this signature.
    """
    if isinstance(obj, dict):
        shape: Any = {key: _shape(obj[key]) for key in sorted(obj.keys())}
    elif isinstance(obj, list):
        shape = {"__list__": True, "first_element": _shape(obj[0]) if obj else None}
    else:
        shape = type(obj).__name__

    payload = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()
