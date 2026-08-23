"""Private runtime validation helpers for safety domain boundaries."""

from __future__ import annotations

from enum import Enum


def require_bool(value: object, *, field_name: str) -> None:
    """Require a real bool, rejecting truthy strings and integer lookalikes."""

    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a bool")


def require_optional_bool(value: object, *, field_name: str) -> None:
    """Require ``bool | None`` without coercion."""

    if value is not None:
        require_bool(value, field_name=field_name)


def require_string(value: object, *, field_name: str) -> None:
    """Require a string without coercing external parser values."""

    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")


def require_optional_string(value: object, *, field_name: str) -> None:
    """Require ``str | None`` without coercion."""

    if value is not None:
        require_string(value, field_name=field_name)


def require_enum(value: object, *, enum_type: type[Enum], field_name: str) -> None:
    """Reject raw strings where a reviewed domain enum is required."""

    if not isinstance(value, enum_type):
        raise TypeError(f"{field_name} must be a {enum_type.__name__}")
