"""Canonical JSON shared by the tap's protected ABI staging code."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from types import MappingProxyType
from typing import Any


MIN_INTEGER = -(2**63)
MAX_INTEGER = 2**64 - 1
MAX_VFS_COMPOSITION_JSON_ITEMS = 4_000_000


class CanonicalJsonError(ValueError):
    """Raised when bytes are not the one accepted canonical JSON encoding."""


def _parse_integer(value: str) -> int:
    parsed = int(value, 10)
    if parsed < MIN_INTEGER or parsed > MAX_INTEGER:
        raise CanonicalJsonError("canonical JSON integer is outside the i64/u64 range")
    return parsed


def _reject_float(value: str) -> float:
    raise CanonicalJsonError(f"canonical JSON permits integer numbers only: {value}")


def _reject_constant(value: str) -> None:
    raise CanonicalJsonError(f"canonical JSON rejects non-finite number {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalJsonError(f"canonical JSON contains duplicate object key {key!r}")
        result[key] = value
    return result


def _plain_and_validate(
    value: Any,
    *,
    maximum_depth: int,
    maximum_items: int,
    maximum_string_bytes: int,
) -> Any:
    item_count = 0
    active: set[int] = set()

    def visit(candidate: Any, depth: int) -> Any:
        nonlocal item_count
        item_count += 1
        if item_count > maximum_items:
            raise CanonicalJsonError("canonical JSON contains too many values")
        if depth > maximum_depth:
            raise CanonicalJsonError("canonical JSON exceeds its nesting-depth limit")
        if candidate is None or isinstance(candidate, bool):
            return candidate
        if isinstance(candidate, int):
            if candidate < MIN_INTEGER or candidate > MAX_INTEGER:
                raise CanonicalJsonError("canonical JSON integer is outside the i64/u64 range")
            return candidate
        if isinstance(candidate, float):
            raise CanonicalJsonError("canonical JSON permits integer numbers only")
        if isinstance(candidate, str):
            try:
                encoded = candidate.encode("utf-8", errors="strict")
            except UnicodeEncodeError as error:
                raise CanonicalJsonError("canonical JSON string is not valid UTF-8") from error
            if len(encoded) > maximum_string_bytes:
                raise CanonicalJsonError("canonical JSON string exceeds its byte limit")
            return candidate
        if isinstance(candidate, Mapping):
            identity = id(candidate)
            if identity in active:
                raise CanonicalJsonError("canonical JSON cannot contain a cycle")
            active.add(identity)
            try:
                result: dict[str, Any] = {}
                for key, child in candidate.items():
                    if not isinstance(key, str):
                        raise CanonicalJsonError("canonical JSON object keys must be strings")
                    visit(key, depth + 1)
                    result[key] = visit(child, depth + 1)
                return result
            finally:
                active.remove(identity)
        if isinstance(candidate, Sequence) and not isinstance(
            candidate, (str, bytes, bytearray)
        ):
            identity = id(candidate)
            if identity in active:
                raise CanonicalJsonError("canonical JSON cannot contain a cycle")
            active.add(identity)
            try:
                return [visit(child, depth + 1) for child in candidate]
            finally:
                active.remove(identity)
        raise CanonicalJsonError(
            f"value of type {type(candidate).__name__} is not canonical JSON"
        )

    if maximum_depth < 1 or maximum_items < 1 or maximum_string_bytes < 1:
        raise CanonicalJsonError("canonical JSON bounds must be positive")
    return visit(value, 0)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def canonical_bytes(
    value: Any,
    *,
    maximum_depth: int = 64,
    maximum_items: int = 100_000,
    maximum_string_bytes: int = 4 * 1024 * 1024,
) -> bytes:
    """Return recursively key-sorted compact UTF-8 JSON with one line feed."""

    plain = _plain_and_validate(
        value,
        maximum_depth=maximum_depth,
        maximum_items=maximum_items,
        maximum_string_bytes=maximum_string_bytes,
    )
    try:
        encoded = json.dumps(
            plain,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise CanonicalJsonError(f"cannot encode canonical JSON: {error}") from error
    return encoded + b"\n"


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def parse_canonical_bytes(
    body: bytes,
    *,
    maximum_bytes: int,
    maximum_depth: int = 64,
    maximum_items: int = 100_000,
    maximum_string_bytes: int = 4 * 1024 * 1024,
) -> MappingProxyType[str, Any]:
    """Parse bounded canonical bytes and recursively freeze the result."""

    parsed = parse_json_bytes(
        body,
        maximum_bytes=maximum_bytes,
        maximum_depth=maximum_depth,
        maximum_items=maximum_items,
        maximum_string_bytes=maximum_string_bytes,
    )
    if canonical_bytes(
        parsed,
        maximum_depth=maximum_depth,
        maximum_items=maximum_items,
        maximum_string_bytes=maximum_string_bytes,
    ) != body:
        raise CanonicalJsonError("JSON bytes are not canonical")
    return parsed


def parse_json_bytes(
    body: bytes,
    *,
    maximum_bytes: int,
    maximum_depth: int = 64,
    maximum_items: int = 100_000,
    maximum_string_bytes: int = 4 * 1024 * 1024,
) -> MappingProxyType[str, Any]:
    """Parse bounded unambiguous JSON and recursively freeze the result."""

    if not isinstance(body, bytes):
        raise CanonicalJsonError("JSON input must be bytes")
    if not body or len(body) > maximum_bytes:
        raise CanonicalJsonError(
            f"JSON must contain 1 through {maximum_bytes} bytes"
        )
    try:
        text = body.decode("utf-8", errors="strict")
        parsed = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_float=_reject_float,
            parse_int=_parse_integer,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, CanonicalJsonError) as error:
        if isinstance(error, CanonicalJsonError):
            raise
        raise CanonicalJsonError(f"canonical JSON is invalid: {error}") from error
    plain = _plain_and_validate(
        parsed,
        maximum_depth=maximum_depth,
        maximum_items=maximum_items,
        maximum_string_bytes=maximum_string_bytes,
    )
    if not isinstance(plain, dict):
        raise CanonicalJsonError("canonical document root must be an object")
    return _freeze(plain)
