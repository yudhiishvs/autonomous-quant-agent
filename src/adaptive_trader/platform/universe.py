"""Immutable symbol roles and allowlists for a configured experiment."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$", flags=re.ASCII)


class SymbolRole(StrEnum):
    """A symbol's authority within one immutable experiment."""

    ACTIVE_TRADABLE = "ACTIVE_TRADABLE"
    BENCHMARK_ONLY = "BENCHMARK_ONLY"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    EXCLUDED = "EXCLUDED"


def _normalize_symbol(value: object, *, index: int) -> str:
    if type(value) is not str:
        raise ValueError(f"symbol at index {index} must be a string")

    symbol = str.upper(value)
    if not value.isascii() or _SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise ValueError(f"symbol at index {index} has an invalid format")
    return symbol


def _normalize_symbol_tuple(value: object) -> tuple[str, ...]:
    if type(value) not in {list, tuple}:
        raise ValueError("symbol collection must be a list or tuple")

    sequence = cast(list[object] | tuple[object, ...], value)
    normalized = tuple(_normalize_symbol(item, index=index) for index, item in enumerate(sequence))
    if len(normalized) != len(set(normalized)):
        raise ValueError("symbol collection contains duplicates after normalization")
    return tuple(sorted(normalized))


class UniverseSpec(BaseModel):
    """Frozen role membership and the allowlists derived from it.

    Collection membership grants no execution authority. Only ``active_tradable`` is used to
    derive the order and target allowlist.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )

    active_tradable: tuple[str, ...]
    benchmark_only: tuple[str, ...]
    context_only: tuple[str, ...]
    excluded: tuple[str, ...]

    @field_validator(
        "active_tradable",
        "benchmark_only",
        "context_only",
        "excluded",
        mode="before",
    )
    @classmethod
    def normalize_symbols(cls, value: object) -> tuple[str, ...]:
        """Normalize one role collection without coercing arbitrary objects."""

        return _normalize_symbol_tuple(value)

    @model_validator(mode="after")
    def validate_role_partition(self) -> UniverseSpec:
        """Reject empty authority and membership shared by multiple roles."""

        if not self.active_tradable:
            raise ValueError("active_tradable must contain at least one symbol")

        role_memberships = (
            self.active_tradable,
            self.benchmark_only,
            self.context_only,
            self.excluded,
        )
        all_symbols = tuple(symbol for role in role_memberships for symbol in role)
        if len(all_symbols) != len(set(all_symbols)):
            raise ValueError("symbol roles must be disjoint")
        return self

    @property
    def collection_allowlist(self) -> tuple[str, ...]:
        """Symbols that a market-data adapter may collect, in deterministic order."""

        return tuple(sorted((*self.active_tradable, *self.benchmark_only, *self.context_only)))

    @property
    def order_allowlist(self) -> tuple[str, ...]:
        """Symbols that may appear in targets or orders."""

        return self.active_tradable

    @property
    def all_symbols(self) -> tuple[str, ...]:
        """Every declared symbol, including explicit exclusions."""

        return tuple(
            sorted(
                (
                    *self.active_tradable,
                    *self.benchmark_only,
                    *self.context_only,
                    *self.excluded,
                )
            )
        )

    def role_for(self, symbol: str) -> SymbolRole:
        """Return the exact configured role for ``symbol``.

        No aliases are applied. A symbol absent from the experiment raises ``KeyError``.
        """

        normalized = _normalize_symbol(symbol, index=0)
        memberships = (
            (SymbolRole.ACTIVE_TRADABLE, self.active_tradable),
            (SymbolRole.BENCHMARK_ONLY, self.benchmark_only),
            (SymbolRole.CONTEXT_ONLY, self.context_only),
            (SymbolRole.EXCLUDED, self.excluded),
        )
        for role, symbols in memberships:
            if normalized in symbols:
                return role
        raise KeyError(normalized)

    def permits_collection(self, symbol: str) -> bool:
        """Return whether ``symbol`` belongs to the collection allowlist."""

        try:
            normalized = _normalize_symbol(symbol, index=0)
        except ValueError:
            return False
        return normalized in self.collection_allowlist

    def permits_order(self, symbol: str) -> bool:
        """Return whether ``symbol`` belongs to the order and target allowlist."""

        try:
            normalized = _normalize_symbol(symbol, index=0)
        except ValueError:
            return False
        return normalized in self.order_allowlist
