"""Versioned collection universe with roles independent of trading authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum


def _normalize_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a nonempty string")
    normalized = symbol.strip().upper()
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for character in normalized):
        raise ValueError(f"Unsupported symbol format: {symbol!r}")
    return normalized


class CollectionRole(StrEnum):
    """A symbol's data-collection role, which grants no trading authority."""

    COLLECTED_EQUITY = "collected_equity"
    CONTEXT = "context"
    BENCHMARK = "benchmark"


@dataclass(frozen=True, slots=True)
class UniverseMemberV1:
    """One symbol in the collection universe."""

    symbol: str
    company_name: str
    role: CollectionRole
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _normalize_symbol(self.symbol))
        if not isinstance(self.company_name, str) or not self.company_name.strip():
            raise ValueError("company_name must be a nonempty string")
        object.__setattr__(self, "company_name", self.company_name.strip())
        if not isinstance(self.role, CollectionRole):
            try:
                object.__setattr__(self, "role", CollectionRole(self.role))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Unsupported collection role: {self.role!r}") from exc
        if self.execution_authorized is not False:
            raise ValueError("Collection-universe membership cannot authorize execution")


@dataclass(frozen=True, slots=True)
class CollectionUniverseV1:
    """An immutable, content-addressed set of symbols collected by the platform."""

    members: tuple[UniverseMemberV1, ...]

    SCHEMA_VERSION = "collection-universe.v1"

    def __post_init__(self) -> None:
        members = tuple(self.members)
        if not members:
            raise ValueError("CollectionUniverseV1.members cannot be empty")
        if not all(isinstance(member, UniverseMemberV1) for member in members):
            raise ValueError("CollectionUniverseV1.members must contain UniverseMemberV1 records")
        symbols = [member.symbol for member in members]
        duplicates = sorted({symbol for symbol in symbols if symbols.count(symbol) > 1})
        if duplicates:
            raise ValueError(f"CollectionUniverseV1 contains duplicate symbols: {duplicates}")
        if any(member.execution_authorized for member in members):
            raise ValueError("A collection universe cannot grant execution authority")
        object.__setattr__(self, "members", members)

    @property
    def symbols(self) -> tuple[str, ...]:
        """Return symbols in declared collection order."""

        return tuple(member.symbol for member in self.members)

    @property
    def execution_symbols(self) -> tuple[str, ...]:
        """Return the separately authorized execution set, empty for this contract."""

        return tuple(member.symbol for member in self.members if member.execution_authorized)

    def member(self, symbol: str) -> UniverseMemberV1:
        """Return metadata for ``symbol`` or raise ``KeyError`` when it is not collected."""

        normalized = _normalize_symbol(symbol)
        for member in self.members:
            if member.symbol == normalized:
                return member
        raise KeyError(normalized)

    def symbols_for_role(self, role: CollectionRole | str) -> tuple[str, ...]:
        """Return symbols assigned to one collection role."""

        try:
            normalized_role = role if isinstance(role, CollectionRole) else CollectionRole(role)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unsupported collection role: {role!r}") from exc
        return tuple(member.symbol for member in self.members if member.role is normalized_role)

    @property
    def universe_hash(self) -> str:
        """Return a deterministic SHA-256 digest of the complete versioned contract."""

        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "members": [
                {
                    "company_name": member.company_name,
                    "execution_authorized": member.execution_authorized,
                    "role": member.role.value,
                    "symbol": member.symbol,
                }
                for member in sorted(self.members, key=lambda item: item.symbol)
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _equity(symbol: str, company_name: str) -> UniverseMemberV1:
    return UniverseMemberV1(
        symbol=symbol,
        company_name=company_name,
        role=CollectionRole.COLLECTED_EQUITY,
    )


COLLECTION_UNIVERSE_V1 = CollectionUniverseV1(
    members=(
        _equity("TSLA", "Tesla, Inc."),
        _equity("UBER", "Uber Technologies, Inc."),
        _equity("GOOGL", "Alphabet Inc."),
        _equity("NVDA", "NVIDIA Corporation"),
        _equity("AMZN", "Amazon.com, Inc."),
        _equity("AAPL", "Apple Inc."),
        _equity("META", "Meta Platforms, Inc."),
        _equity("AMD", "Advanced Micro Devices, Inc."),
        _equity("CSCO", "Cisco Systems, Inc."),
        _equity("NET", "Cloudflare, Inc."),
        _equity("OKTA", "Okta, Inc."),
        _equity("ROKU", "Roku, Inc."),
        _equity("BOX", "Box, Inc."),
        _equity("ZG", "Zillow Group, Inc."),
        _equity("RBLX", "Roblox Corporation"),
        _equity("SOUN", "SoundHound AI, Inc."),
        _equity("PUBM", "PubMatic, Inc."),
        _equity("HLIT", "Harmonic Inc."),
        _equity("PAYC", "Paycom Software, Inc."),
        _equity("WDAY", "Workday, Inc."),
        _equity("SNDK", "Sandisk Corporation"),
        _equity("RIVN", "Rivian Automotive, Inc."),
        _equity("LCID", "Lucid Group, Inc."),
        _equity("AAOI", "Applied Optoelectronics, Inc."),
        _equity("AXTI", "AXT, Inc."),
        _equity("INSG", "Inseego Corp."),
        UniverseMemberV1("SPY", "SPDR S&P 500 ETF Trust", CollectionRole.CONTEXT),
        UniverseMemberV1("QQQ", "Invesco QQQ Trust", CollectionRole.CONTEXT),
        UniverseMemberV1("SOXX", "iShares Semiconductor ETF", CollectionRole.BENCHMARK),
    )
)
