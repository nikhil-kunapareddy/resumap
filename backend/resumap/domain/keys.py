"""The keys mappings, rewrites and export overrides are filed under.

A resume block is keyed by its parsed id: its kind's first three letters plus
a counter ("bul012", "sec003"). An experience from the library is keyed by
``experience_key`` ("exp012"). No block kind starts with "exp", so both share
the same tables without colliding.
"""
from __future__ import annotations

EXP_PREFIX = "exp"


def experience_key(experience_id: int) -> str:
    return f"{EXP_PREFIX}{experience_id:03d}"


def experience_id(key: str) -> int | None:
    """The experience a key names, or None for a resume block."""
    if key.startswith(EXP_PREFIX) and key[len(EXP_PREFIX):].isdigit():
        return int(key[len(EXP_PREFIX):])
    return None
