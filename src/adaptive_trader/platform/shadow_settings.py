"""Database-only shadow execution composition outside the frozen service configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import Engine, text

from adaptive_trader.platform.config import (
    BrokerAdapter,
    ExecutionMode,
    PlatformConfig,
    RuntimeService,
    RuntimeSettings,
    load_runtime_settings,
)
from adaptive_trader.platform.storage.engine import create_platform_engine


@dataclass(frozen=True, slots=True)
class ShadowExecutionSettings:
    """Validated diagnostic profile with no provider or broker secret capability.

    The existing database-only validator supplies path and secret-file validation. Its
    verifier service label is not runtime authority: this adapter enforces execution-role
    identity independently before any PostgreSQL read or write in the shadow cycle.
    """

    _database_configuration: RuntimeSettings
    fixture: bool = False

    def __post_init__(self) -> None:
        runtime = self._database_configuration
        if (
            type(runtime) is not RuntimeSettings
            or runtime.service is not RuntimeService.AUDIT_VERIFIER
        ):
            raise TypeError("shadow settings require database-only validated configuration")
        if type(self.fixture) is not bool:
            raise TypeError("shadow fixture selection must be explicit")
        profile = runtime.platform.profile
        expected = (
            (ExecutionMode.OFFLINE, BrokerAdapter.FAKE)
            if self.fixture
            else (
                ExecutionMode.SHADOW,
                BrokerAdapter.NONE,
            )
        )
        if (
            profile.execution.submission_enabled
            or (profile.mode, profile.execution.broker) != expected
        ):
            raise ValueError("shadow requires disabled submission and an exact diagnostic mode")

    @property
    def platform(self) -> PlatformConfig:
        return self._database_configuration.platform


def load_shadow_execution_settings(
    environment: Mapping[str, str],
    *,
    application_root: Path,
    fixture: bool = False,
) -> ShadowExecutionSettings:
    return ShadowExecutionSettings(
        load_runtime_settings(
            environment, service=RuntimeService.AUDIT_VERIFIER, application_root=application_root
        ),
        fixture=fixture,
    )


def create_shadow_execution_engine(settings: ShadowExecutionSettings) -> Engine:
    if type(settings) is not ShadowExecutionSettings:
        raise TypeError("shadow engine requires validated execution settings")
    return create_platform_engine(
        settings._database_configuration, application_name="aqa-shadow-execution"
    )


def require_shadow_database_role(
    engine: Engine,
    *,
    role: Literal["aqa_execution", "aqa_strategy"],
    fixture: bool,
) -> None:
    """Reject cross-role credentials before touching a durable shadow boundary."""
    if role not in {"aqa_execution", "aqa_strategy"}:
        raise ValueError("shadow database authority is invalid")
    if engine.dialect.name == "sqlite":
        if not fixture:
            raise ValueError("operational shadow requires PostgreSQL")
        return
    if engine.dialect.name != "postgresql" or fixture:
        raise ValueError("shadow fixture mode requires isolated SQLite")
    with engine.connect() as connection:
        actual = connection.scalar(text("SELECT current_user"))
    if actual not in {role, f"{role}_login"}:
        raise ValueError("shadow database role does not match the process authority")


def shadow_readiness_is_current(
    engine: Engine,
    *,
    experiment_hash: str,
    timeframe: str,
    interval_end: datetime,
    now: datetime,
) -> bool:
    """Recheck collector repair and publication fences through a read-only projection."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT contiguous_through, updated_at FROM aqa.aqa_operational_readiness_v "
                    "WHERE experiment_hash = :experiment AND timeframe = :timeframe "
                    "AND role = 'active' AND status = 'ready' "
                    "AND NOT pending_work AND NOT unresolved_gaps"
                ),
                {"experiment": experiment_hash, "timeframe": timeframe},
            )
            .mappings()
            .one_or_none()
        )
    return bool(
        row is not None
        and row["contiguous_through"] is not None
        and row["contiguous_through"] >= interval_end
        and row["updated_at"] <= now
    )
