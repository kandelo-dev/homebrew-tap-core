"""Canonical tap-side durable record readers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import CanonicalJsonError, parse_canonical_bytes
from .plan import PlanError, validate_tap_plan


MAX_TAP_PLAN_BYTES = 32 * 1024 * 1024


class TapRecordError(ValueError):
    """Raised when a tap-owned record is malformed or semantically invalid."""


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(child) for child in value]
    return value


def load_tap_plan_record(body: bytes) -> dict[str, Any]:
    try:
        value = _plain(parse_canonical_bytes(body, maximum_bytes=MAX_TAP_PLAN_BYTES))
        validate_tap_plan(value)
    except (CanonicalJsonError, PlanError) as error:
        raise TapRecordError(f"tap plan record is invalid: {error}") from error
    return value
