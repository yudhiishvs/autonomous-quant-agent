"""Strict, service-scoped runtime-settings tests with no ambient or external authority."""

from __future__ import annotations

import copy
import json
import pickle
import shutil
import traceback
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path, PosixPath
from typing import Any, cast

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

import adaptive_trader.platform.config as platform_config
from adaptive_trader.platform import (
    ExecutionMode,
    PaperOrderEnablement,
    RuntimeService,
    RuntimeSettings,
    RuntimeSettingsError,
    SecretFileReference,
    SecretFileVariable,
    load_runtime_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "TEST_AQA_OPERATOR_TOKEN_DO_NOT_LEAK"
PROFILE_PATHS = {
    ExecutionMode.OFFLINE: "configs/platform/offline.yaml",
    ExecutionMode.SHADOW: "configs/platform/shadow.yaml",
    ExecutionMode.PAPER: "configs/platform/paper.yaml",
}
SERVICE_SOURCES = {
    RuntimeService.MIGRATE: frozenset({SecretFileVariable.DATABASE_URL}),
    RuntimeService.CONTROL_API: frozenset(
        {SecretFileVariable.DATABASE_URL, SecretFileVariable.OPERATOR_TOKEN}
    ),
    RuntimeService.MARKET_DATA_WORKER: frozenset({SecretFileVariable.DATABASE_URL}),
    RuntimeService.SCHEDULER_WORKER: frozenset({SecretFileVariable.DATABASE_URL}),
    RuntimeService.STRATEGY_WORKER: frozenset({SecretFileVariable.DATABASE_URL}),
    RuntimeService.EXECUTION_WORKER: frozenset({SecretFileVariable.DATABASE_URL}),
    RuntimeService.DASHBOARD: frozenset({SecretFileVariable.OPERATOR_TOKEN}),
    RuntimeService.AUDIT_VERIFIER: frozenset({SecretFileVariable.DATABASE_URL}),
    RuntimeService.MARKET_DATA_LIVE: frozenset(
        {
            SecretFileVariable.DATABASE_URL,
            SecretFileVariable.ALPACA_DATA_API_KEY,
            SecretFileVariable.ALPACA_DATA_SECRET_KEY,
        }
    ),
    RuntimeService.PAPER_EXECUTION_WORKER: frozenset(
        {
            SecretFileVariable.DATABASE_URL,
            SecretFileVariable.ALPACA_PAPER_API_KEY,
            SecretFileVariable.ALPACA_PAPER_SECRET_KEY,
            SecretFileVariable.PAPER_ACCOUNT_ID_HASH,
        }
    ),
}
SOURCE_FIELDS = {
    SecretFileVariable.DATABASE_URL: "database_url_file",
    SecretFileVariable.OPERATOR_TOKEN: "operator_token_file",
    SecretFileVariable.ALPACA_DATA_API_KEY: "alpaca_data_api_key_file",
    SecretFileVariable.ALPACA_DATA_SECRET_KEY: "alpaca_data_secret_key_file",
    SecretFileVariable.ALPACA_PAPER_API_KEY: "alpaca_paper_api_key_file",
    SecretFileVariable.ALPACA_PAPER_SECRET_KEY: "alpaca_paper_secret_key_file",
    SecretFileVariable.PAPER_ACCOUNT_ID_HASH: "paper_account_id_hash_file",
}
SERVICE_MODES = {
    RuntimeService.MIGRATE: frozenset(ExecutionMode),
    RuntimeService.CONTROL_API: frozenset(ExecutionMode),
    RuntimeService.MARKET_DATA_WORKER: frozenset({ExecutionMode.OFFLINE}),
    RuntimeService.SCHEDULER_WORKER: frozenset(ExecutionMode),
    RuntimeService.STRATEGY_WORKER: frozenset(ExecutionMode),
    RuntimeService.EXECUTION_WORKER: frozenset({ExecutionMode.OFFLINE}),
    RuntimeService.DASHBOARD: frozenset(ExecutionMode),
    RuntimeService.AUDIT_VERIFIER: frozenset(ExecutionMode),
    RuntimeService.MARKET_DATA_LIVE: frozenset({ExecutionMode.SHADOW, ExecutionMode.PAPER}),
    RuntimeService.PAPER_EXECUTION_WORKER: frozenset({ExecutionMode.PAPER}),
}
FORBIDDEN_GENERIC_VARIABLES = {
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "APCA_API_BASE_URL",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
}


def _environment_for(service: RuntimeService, mode: ExecutionMode) -> dict[str, str]:
    environment = {"AQA_CONFIG": PROFILE_PATHS[mode]}
    for source in SERVICE_SOURCES[service]:
        if (
            source is SecretFileVariable.DATABASE_URL
            and mode is ExecutionMode.OFFLINE
            and service is not RuntimeService.MIGRATE
        ):
            continue
        environment[source.value] = f"/run/secrets/{source.name.lower()}"
    return environment


def _copy_application_root(tmp_path: Path) -> Path:
    root = tmp_path / "application"
    root.mkdir()
    shutil.copytree(PROJECT_ROOT / "configs", root / "configs")
    return root


class _GuardedMapping(Mapping[str, str]):
    def __init__(
        self,
        keys: Sequence[object],
        values: Mapping[Any, Any],
        *,
        prohibited_reads: frozenset[object] = frozenset(),
        iteration_error: Exception | None = None,
    ) -> None:
        self._keys = tuple(keys)
        self._values = values
        self._prohibited_reads = prohibited_reads
        self._iteration_error = iteration_error
        self.reads: dict[object, int] = {}

    def __iter__(self) -> Iterator[str]:
        if self._iteration_error is not None:
            raise self._iteration_error
        for key in self._keys:
            yield cast(str, key)

    def __len__(self) -> int:
        raise AssertionError("runtime settings must not trust mapping length")

    def __getitem__(self, key: str) -> str:
        if key in self._prohibited_reads:
            raise AssertionError(SENTINEL)
        self.reads[key] = self.reads.get(key, 0) + 1
        return cast(str, self._values[key])


def _runtime_model_payload(settings: RuntimeSettings) -> dict[str, object]:
    return dict(settings.__dict__)


class _RuntimeSettingsContainer(BaseModel):
    settings: RuntimeSettings


def test_runtime_service_and_setting_inventories_are_exact() -> None:
    assert {service.value for service in RuntimeService} == {
        "migrate",
        "control-api",
        "market-data-worker",
        "scheduler-worker",
        "strategy-worker",
        "execution-worker",
        "dashboard",
        "audit-verifier",
        "market-data-live",
        "paper-execution-worker",
    }
    assert {source.value for source in SecretFileVariable} == {
        "AQA_DATABASE_URL_FILE",
        "AQA_OPERATOR_TOKEN_FILE",
        "AQA_ALPACA_DATA_API_KEY_FILE",
        "AQA_ALPACA_DATA_SECRET_KEY_FILE",
        "AQA_ALPACA_PAPER_API_KEY_FILE",
        "AQA_ALPACA_PAPER_SECRET_KEY_FILE",
        "AQA_PAPER_ACCOUNT_ID_HASH_FILE",
    }
    nonsecret_defaults = {
        "AQA_CONFIG": "configs/platform/offline.yaml",
        "AQA_ARTIFACT_ROOT": "outputs/artifacts",
        "AQA_API_BASE_URL": "http://127.0.0.1:8000",
        "AQA_LOG_FORMAT": "json",
        "AQA_ENABLE_PAPER_ORDERS": "NO",
        "AQA_API_DOCS_ENABLED": "NO",
    }
    assert set(platform_config._NONSECRET_RUNTIME_VARIABLES) == set(nonsecret_defaults)
    assert nonsecret_defaults == platform_config._RUNTIME_DEFAULTS


def test_empty_injected_environment_uses_exact_offline_defaults() -> None:
    settings = load_runtime_settings(
        {}, service=RuntimeService.MARKET_DATA_WORKER, application_root=PROJECT_ROOT
    )

    assert settings.config_path == "configs/platform/offline.yaml"
    assert settings.platform.profile.mode is ExecutionMode.OFFLINE
    assert settings.artifact_root == PROJECT_ROOT / "outputs" / "artifacts"
    assert settings.api_base_url == "http://127.0.0.1:8000"
    assert settings.log_format == "json"
    assert settings.paper_order_enablement is PaperOrderEnablement.DISABLED
    assert settings.api_docs_enabled is False
    assert settings.offline_database_path == PROJECT_ROOT / "runtime" / "aqa-offline.sqlite3"
    assert all(getattr(settings, field_name) is None for field_name in SOURCE_FIELDS.values())


@pytest.mark.parametrize("service", tuple(RuntimeService))
@pytest.mark.parametrize("mode", tuple(ExecutionMode))
def test_service_profile_compatibility_and_exact_secret_scope(
    service: RuntimeService,
    mode: ExecutionMode,
) -> None:
    environment = _environment_for(service, mode)
    if mode not in SERVICE_MODES[service]:
        with pytest.raises(RuntimeSettingsError, match="incompatible"):
            load_runtime_settings(environment, service=service, application_root=PROJECT_ROOT)
        return

    settings = load_runtime_settings(
        environment,
        service=service,
        application_root=PROJECT_ROOT,
    )
    expected_sources = set(SERVICE_SOURCES[service])
    if mode is ExecutionMode.OFFLINE and service is not RuntimeService.MIGRATE:
        expected_sources.discard(SecretFileVariable.DATABASE_URL)
    for source, field_name in SOURCE_FIELDS.items():
        reference = getattr(settings, field_name)
        if source in expected_sources:
            assert isinstance(reference, SecretFileReference)
            assert reference.source is source
        else:
            assert reference is None
    expects_fallback = (
        mode is ExecutionMode.OFFLINE
        and SecretFileVariable.DATABASE_URL in SERVICE_SOURCES[service]
        and service is not RuntimeService.MIGRATE
    )
    assert (settings.offline_database_path is not None) is expects_fallback


@pytest.mark.parametrize("service", tuple(RuntimeService))
@pytest.mark.parametrize("source", tuple(SecretFileVariable))
def test_out_of_scope_secret_reference_is_rejected_without_reading_its_value(
    service: RuntimeService,
    source: SecretFileVariable,
) -> None:
    if source in SERVICE_SOURCES[service]:
        return
    mapping = _GuardedMapping(
        [source.value],
        {source.value: SENTINEL},
        prohibited_reads=frozenset({source.value}),
    )

    with pytest.raises(RuntimeSettingsError, match="outside its authority") as captured:
        load_runtime_settings(
            mapping,
            service=service,
            application_root=PROJECT_ROOT,
        )

    assert mapping.reads == {}
    assert SENTINEL not in str(captured.value)


@pytest.mark.parametrize("name", sorted(FORBIDDEN_GENERIC_VARIABLES))
@pytest.mark.parametrize("value", ["", SENTINEL])
def test_forbidden_generic_alpaca_variable_rejects_on_presence_without_value_access(
    name: str,
    value: str,
) -> None:
    mapping = _GuardedMapping(
        [name, "AQA_CONFIG"],
        {name: value, "AQA_CONFIG": SENTINEL},
        prohibited_reads=frozenset({name, "AQA_CONFIG"}),
    )

    with pytest.raises(RuntimeSettingsError, match="forbidden generic") as captured:
        load_runtime_settings(
            mapping,
            service=RuntimeService.MARKET_DATA_WORKER,
            application_root=PROJECT_ROOT,
        )

    assert mapping.reads == {}
    assert SENTINEL not in str(captured.value)


def test_unknown_aqa_variable_is_rejected_without_disclosure_or_value_access() -> None:
    hostile_name = f"AQA_UNKNOWN_{SENTINEL}\x1b[31m"
    mapping = _GuardedMapping(
        [hostile_name],
        {hostile_name: SENTINEL},
        prohibited_reads=frozenset({hostile_name}),
    )

    with pytest.raises(RuntimeSettingsError, match="unknown AQA") as captured:
        load_runtime_settings(
            mapping,
            service=RuntimeService.MARKET_DATA_WORKER,
            application_root=PROJECT_ROOT,
        )

    assert hostile_name not in str(captured.value)
    assert SENTINEL not in str(captured.value)
    assert mapping.reads == {}


def test_unrelated_and_legacy_environment_values_are_never_read() -> None:
    names = ["CI_TOKEN", "AWS_SECRET_ACCESS_KEY", "APA_ALPACA_DATA_API_KEY"]
    mapping = _GuardedMapping(
        names,
        {name: SENTINEL for name in names},
        prohibited_reads=frozenset(names),
    )

    settings = load_runtime_settings(
        mapping,
        service=RuntimeService.MARKET_DATA_WORKER,
        application_root=PROJECT_ROOT,
    )

    assert settings.platform.profile.mode is ExecutionMode.OFFLINE
    assert mapping.reads == {}


def test_each_relevant_environment_value_is_read_exactly_once() -> None:
    values = {
        "AQA_CONFIG": "configs/platform/offline.yaml",
        "AQA_ARTIFACT_ROOT": "outputs/artifacts",
        "AQA_API_BASE_URL": "http://127.0.0.1:8000",
        "AQA_LOG_FORMAT": "json",
        "AQA_ENABLE_PAPER_ORDERS": "NO",
        "AQA_API_DOCS_ENABLED": "NO",
        "AQA_DATABASE_URL_FILE": "/run/secrets/database-url",
    }
    mapping = _GuardedMapping(list(values), values)

    settings = load_runtime_settings(
        mapping,
        service=RuntimeService.MARKET_DATA_WORKER,
        application_root=PROJECT_ROOT,
    )

    assert settings.database_url_file is not None
    assert mapping.reads == {name: 1 for name in values}


def test_runtime_snapshot_does_not_retain_or_reread_the_environment() -> None:
    environment = {"AQA_API_BASE_URL": "http://control-api:8000"}
    settings = load_runtime_settings(
        environment,
        service=RuntimeService.MARKET_DATA_WORKER,
        application_root=PROJECT_ROOT,
    )
    environment["AQA_API_BASE_URL"] = "https://changed.invalid"

    assert settings.api_base_url == "http://control-api:8000"


def test_runtime_snapshot_bounds_infinite_key_iteration() -> None:
    class InfiniteMapping(Mapping[str, str]):
        yielded = 0

        def __iter__(self) -> Iterator[str]:
            index = 0
            while True:
                self.yielded += 1
                yield f"UNRELATED_{index}"
                index += 1

        def __len__(self) -> int:
            raise AssertionError("length must not be read")

        def __getitem__(self, key: str) -> str:
            raise AssertionError(key)

    environment = InfiniteMapping()
    with pytest.raises(RuntimeSettingsError, match="read safely"):
        load_runtime_settings(
            environment,
            service=RuntimeService.MARKET_DATA_WORKER,
            application_root=PROJECT_ROOT,
        )
    assert environment.yielded == 4_097


@pytest.mark.parametrize(
    "mapping",
    [
        _GuardedMapping(["AQA_CONFIG", "AQA_CONFIG"], {"AQA_CONFIG": "unused"}),
        _GuardedMapping([1], {1: "unused"}),
        _GuardedMapping([], {}, iteration_error=RuntimeError(SENTINEL)),
        _GuardedMapping(["AQA_CONFIG"], {"AQA_CONFIG": 1}),
        _GuardedMapping(
            ["AQA_CONFIG"],
            {"AQA_CONFIG": SENTINEL},
            prohibited_reads=frozenset({"AQA_CONFIG"}),
        ),
    ],
)
def test_hostile_mapping_failures_are_translated_without_context_or_disclosure(
    mapping: Mapping[str, str],
) -> None:
    with pytest.raises(RuntimeSettingsError) as captured:
        load_runtime_settings(
            mapping,
            service=RuntimeService.MARKET_DATA_WORKER,
            application_root=PROJECT_ROOT,
        )

    rendered = "".join(traceback.format_exception(captured.value))
    assert SENTINEL not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_exact_string_subclasses_are_rejected_for_keys_and_values() -> None:
    class TextSubclass(str):
        pass

    key_mapping = _GuardedMapping(
        [TextSubclass("AQA_CONFIG")],
        {TextSubclass("AQA_CONFIG"): "configs/platform/offline.yaml"},
    )
    value_mapping = _GuardedMapping(
        ["AQA_CONFIG"],
        {"AQA_CONFIG": TextSubclass("configs/platform/offline.yaml")},
    )

    for mapping in (key_mapping, value_mapping):
        with pytest.raises(RuntimeSettingsError, match="read safely"):
            load_runtime_settings(
                mapping,
                service=RuntimeService.MARKET_DATA_WORKER,
                application_root=PROJECT_ROOT,
            )


@pytest.mark.parametrize(
    "config_path",
    [
        "",
        "configs",
        "configs/",
        "platform/offline.yaml",
        "/configs/platform/offline.yaml",
        "configs//platform/offline.yaml",
        "configs/./platform/offline.yaml",
        "configs/platform/../offline.yaml",
        "configs\\platform\\offline.yaml",
        "configs/platform/offline.yaml/",
        "configs/platform/\x00offline.yaml",
        "configs/" + ("x" * 256),
    ],
)
def test_config_setting_is_canonical_and_confined(config_path: str) -> None:
    with pytest.raises(RuntimeSettingsError, match="AQA_CONFIG"):
        load_runtime_settings(
            {"AQA_CONFIG": config_path},
            service=RuntimeService.MARKET_DATA_WORKER,
            application_root=PROJECT_ROOT,
        )


def test_invalid_selected_profile_error_does_not_disclose_path() -> None:
    config_path = f"configs/platform/{SENTINEL}.yaml"
    with pytest.raises(RuntimeSettingsError) as captured:
        load_runtime_settings(
            {"AQA_CONFIG": config_path},
            service=RuntimeService.MARKET_DATA_WORKER,
            application_root=PROJECT_ROOT,
        )
    assert config_path not in str(captured.value)
    assert SENTINEL not in str(captured.value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "outputs/../artifacts",
        "outputs//artifacts",
        "outputs/artifacts/",
        "outputs\\artifacts",
        "outputs/\x00artifacts",
        "/tmp/outside-application-root",
    ],
)
def test_artifact_root_rejects_invalid_or_unconfined_paths(value: str) -> None:
    with pytest.raises(RuntimeSettingsError):
        load_runtime_settings(
            {"AQA_ARTIFACT_ROOT": value},
            service=RuntimeService.MARKET_DATA_WORKER,
            application_root=PROJECT_ROOT,
        )


def test_artifact_root_rejects_oversized_components_and_anchored_paths() -> None:
    oversized_component = "x" * 256
    maximum_raw_relative_path = "/".join(["x" * 255] * 16)
    assert len(maximum_raw_relative_path.encode("utf-8")) == 4_095

    for value in (f"missing/{oversized_component}", maximum_raw_relative_path):
        with pytest.raises(RuntimeSettingsError, match=r"oversized|path-size"):
            load_runtime_settings(
                {"AQA_ARTIFACT_ROOT": value},
                service=RuntimeService.MARKET_DATA_WORKER,
                application_root=PROJECT_ROOT,
            )


def test_artifact_root_respects_the_pinned_filesystem_path_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_bytes = len(PROJECT_ROOT.as_posix().encode("utf-8"))
    monkeypatch.setattr(platform_config.os, "fpathconf", lambda descriptor, name: root_bytes + 10)

    with pytest.raises(RuntimeSettingsError, match="path-size"):
        load_runtime_settings(
            {"AQA_ARTIFACT_ROOT": "outputs/artifacts"},
            service=RuntimeService.MARKET_DATA_WORKER,
            application_root=PROJECT_ROOT,
        )


def test_artifact_root_accepts_canonical_relative_and_contained_absolute_paths() -> None:
    relative = load_runtime_settings(
        {"AQA_ARTIFACT_ROOT": "outputs/custom"},
        service=RuntimeService.MARKET_DATA_WORKER,
        application_root=PROJECT_ROOT,
    )
    absolute_path = PROJECT_ROOT / "outputs" / "another-custom"
    absolute = load_runtime_settings(
        {"AQA_ARTIFACT_ROOT": absolute_path.as_posix()},
        service=RuntimeService.MARKET_DATA_WORKER,
        application_root=PROJECT_ROOT,
    )

    assert relative.artifact_root == PROJECT_ROOT / "outputs" / "custom"
    assert absolute.artifact_root == absolute_path


def test_runtime_path_validation_rejects_files_and_symbolic_links(tmp_path: Path) -> None:
    root = _copy_application_root(tmp_path)
    existing_file = root / "artifact-file"
    existing_file.write_text("not a directory", encoding="utf-8")
    target = root / "target"
    target.mkdir()
    ancestor_link = root / "linked"
    ancestor_link.symlink_to(target, target_is_directory=True)

    for value in ("artifact-file", "linked", "linked/artifacts"):
        with pytest.raises(RuntimeSettingsError, match="runtime path"):
            load_runtime_settings(
                {"AQA_ARTIFACT_ROOT": value},
                service=RuntimeService.MARKET_DATA_WORKER,
                application_root=root,
            )


def test_runtime_settings_create_no_artifact_or_database_paths(tmp_path: Path) -> None:
    root = _copy_application_root(tmp_path)

    settings = load_runtime_settings(
        {},
        service=RuntimeService.MARKET_DATA_WORKER,
        application_root=root,
    )

    assert settings.artifact_root == root / "outputs" / "artifacts"
    assert settings.offline_database_path == root / "runtime" / "aqa-offline.sqlite3"
    assert not (root / "outputs").exists()
    assert not (root / "runtime").exists()


def test_application_root_must_be_exact_existing_canonical_nonsymlink_directory(
    tmp_path: Path,
) -> None:
    root = _copy_application_root(tmp_path)
    missing = tmp_path / "missing"
    file_path = tmp_path / "file"
    file_path.write_text("not a directory", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(root, target_is_directory=True)

    invalid_roots: list[object] = [
        root.as_posix(),
        Path("relative"),
        Path("/"),
        missing,
        file_path,
        linked,
        Path(root.as_posix() + "/child/.."),
    ]
    for invalid in invalid_roots:
        with pytest.raises(RuntimeSettingsError, match="application_root"):
            load_runtime_settings(
                {},
                service=RuntimeService.MARKET_DATA_WORKER,
                application_root=cast(Path, invalid),
            )


def test_application_root_rejects_path_subclasses_without_rendering_them(tmp_path: Path) -> None:
    class HostilePath(PosixPath):
        def __fspath__(self) -> str:
            raise RuntimeError(SENTINEL)

    root = HostilePath(tmp_path / SENTINEL)
    with pytest.raises(RuntimeSettingsError) as captured:
        load_runtime_settings({}, service=RuntimeService.MARKET_DATA_WORKER, application_root=root)
    assert SENTINEL not in str(captured.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://control-api:8000", "http://control-api:8000"),
        ("HTTPS://Example.COM:443", "https://example.com:443"),
    ],
)
def test_api_base_url_accepts_only_canonicalizable_http_origins(
    value: str,
    expected: str,
) -> None:
    settings = load_runtime_settings(
        {"AQA_API_BASE_URL": value},
        service=RuntimeService.MARKET_DATA_WORKER,
        application_root=PROJECT_ROOT,
    )
    assert settings.api_base_url == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "control-api:8000",
        "ftp://control-api",
        "http://",
        "http://user:password@control-api",
        "http://control-api/path",
        "http://control-api?query=value",
        "http://control-api#fragment",
        "http://control-api:0",
        "http://control-api:65536",
        "http://control-api:not-a-port",
        "http://control-api:",
        "http://bad..host",
        "http://-bad-host",
        "http://bad-host-",
        "http://control_api",
        "http://control-api ",
        "http://contröl-api",
        "http://control-api\\path",
    ],
)
def test_api_base_url_rejects_non_origins_without_network_access(value: str) -> None:
    with pytest.raises(RuntimeSettingsError, match="HTTP origin"):
        load_runtime_settings(
            {"AQA_API_BASE_URL": value},
            service=RuntimeService.MARKET_DATA_WORKER,
            application_root=PROJECT_ROOT,
        )


@pytest.mark.parametrize("value", ["JSON", "text", "", "json "])
def test_log_format_accepts_only_exact_json(value: str) -> None:
    with pytest.raises(RuntimeSettingsError, match="LOG_FORMAT"):
        load_runtime_settings(
            {"AQA_LOG_FORMAT": value},
            service=RuntimeService.MARKET_DATA_WORKER,
            application_root=PROJECT_ROOT,
        )


@pytest.mark.parametrize(("value", "expected"), [("NO", False), ("YES", True)])
def test_api_docs_setting_is_closed(value: str, expected: bool) -> None:
    settings = load_runtime_settings(
        {"AQA_API_DOCS_ENABLED": value},
        service=RuntimeService.MARKET_DATA_WORKER,
        application_root=PROJECT_ROOT,
    )
    assert settings.api_docs_enabled is expected


@pytest.mark.parametrize("value", ["yes", "TRUE", "", "1"])
def test_api_docs_setting_rejects_other_values(value: str) -> None:
    with pytest.raises(RuntimeSettingsError, match="DOCS_ENABLED"):
        load_runtime_settings(
            {"AQA_API_DOCS_ENABLED": value},
            service=RuntimeService.MARKET_DATA_WORKER,
            application_root=PROJECT_ROOT,
        )


def test_paper_acknowledgement_is_scoped_and_cannot_enable_tracked_submission() -> None:
    paper_environment = _environment_for(RuntimeService.PAPER_EXECUTION_WORKER, ExecutionMode.PAPER)
    paper_environment["AQA_ENABLE_PAPER_ORDERS"] = "I_ACKNOWLEDGE_AQA_PAPER_ONLY"
    settings = load_runtime_settings(
        paper_environment,
        service=RuntimeService.PAPER_EXECUTION_WORKER,
        application_root=PROJECT_ROOT,
    )

    assert settings.paper_order_enablement is PaperOrderEnablement.ACKNOWLEDGED
    assert settings.platform.profile.execution.submission_enabled is False

    with pytest.raises(RuntimeSettingsError, match="strict validation"):
        load_runtime_settings(
            {"AQA_ENABLE_PAPER_ORDERS": "I_ACKNOWLEDGE_AQA_PAPER_ONLY"},
            service=RuntimeService.EXECUTION_WORKER,
            application_root=PROJECT_ROOT,
        )


@pytest.mark.parametrize("value", ["YES", "I_ACKNOWLEDGE_PAPER_ONLY", "", "NO "])
def test_paper_acknowledgement_rejects_near_misses(value: str) -> None:
    with pytest.raises(RuntimeSettingsError, match="acknowledgement"):
        load_runtime_settings(
            {"AQA_ENABLE_PAPER_ORDERS": value},
            service=RuntimeService.EXECUTION_WORKER,
            application_root=PROJECT_ROOT,
        )


def test_paper_profile_does_not_require_or_receive_paper_credentials_for_other_services() -> None:
    settings = load_runtime_settings(
        {
            "AQA_CONFIG": PROFILE_PATHS[ExecutionMode.PAPER],
            "AQA_DATABASE_URL_FILE": "/run/secrets/database-url",
        },
        service=RuntimeService.STRATEGY_WORKER,
        application_root=PROJECT_ROOT,
    )
    assert settings.platform.profile.mode is ExecutionMode.PAPER
    assert settings.alpaca_paper_api_key_file is None
    assert settings.alpaca_paper_secret_key_file is None
    assert settings.paper_account_id_hash_file is None


@pytest.mark.parametrize(
    ("service", "mode", "environment"),
    [
        (RuntimeService.MIGRATE, ExecutionMode.OFFLINE, {}),
        (RuntimeService.CONTROL_API, ExecutionMode.OFFLINE, {}),
        (RuntimeService.DASHBOARD, ExecutionMode.OFFLINE, {}),
        (
            RuntimeService.STRATEGY_WORKER,
            ExecutionMode.SHADOW,
            {"AQA_CONFIG": PROFILE_PATHS[ExecutionMode.SHADOW]},
        ),
        (
            RuntimeService.MARKET_DATA_LIVE,
            ExecutionMode.SHADOW,
            {
                "AQA_CONFIG": PROFILE_PATHS[ExecutionMode.SHADOW],
                "AQA_ALPACA_DATA_API_KEY_FILE": "/run/secrets/data-key",
                "AQA_ALPACA_DATA_SECRET_KEY_FILE": "/run/secrets/data-secret",
            },
        ),
        (
            RuntimeService.PAPER_EXECUTION_WORKER,
            ExecutionMode.PAPER,
            {
                "AQA_CONFIG": PROFILE_PATHS[ExecutionMode.PAPER],
                "AQA_ALPACA_PAPER_API_KEY_FILE": "/run/secrets/paper-key",
                "AQA_ALPACA_PAPER_SECRET_KEY_FILE": "/run/secrets/paper-secret",
                "AQA_PAPER_ACCOUNT_ID_HASH_FILE": "/run/secrets/account-hash",
            },
        ),
    ],
)
def test_required_service_secret_omissions_fail_closed(
    service: RuntimeService,
    mode: ExecutionMode,
    environment: dict[str, str],
) -> None:
    assert mode in SERVICE_MODES[service]
    with pytest.raises(RuntimeSettingsError, match="required secret"):
        load_runtime_settings(environment, service=service, application_root=PROJECT_ROOT)


@pytest.mark.parametrize(
    "present",
    [
        frozenset(),
        frozenset({SecretFileVariable.ALPACA_DATA_API_KEY}),
        frozenset({SecretFileVariable.ALPACA_DATA_SECRET_KEY}),
    ],
)
def test_live_market_data_requires_complete_data_pair(
    present: frozenset[SecretFileVariable],
) -> None:
    environment = {
        "AQA_CONFIG": PROFILE_PATHS[ExecutionMode.SHADOW],
        "AQA_DATABASE_URL_FILE": "/run/secrets/database-url",
        **{source.value: f"/run/secrets/{source.name.lower()}" for source in present},
    }
    with pytest.raises(RuntimeSettingsError, match="required secret"):
        load_runtime_settings(
            environment,
            service=RuntimeService.MARKET_DATA_LIVE,
            application_root=PROJECT_ROOT,
        )


@pytest.mark.parametrize(
    "present",
    [
        frozenset(),
        frozenset({SecretFileVariable.ALPACA_PAPER_API_KEY}),
        frozenset({SecretFileVariable.ALPACA_PAPER_SECRET_KEY}),
        frozenset({SecretFileVariable.PAPER_ACCOUNT_ID_HASH}),
        frozenset(
            {
                SecretFileVariable.ALPACA_PAPER_API_KEY,
                SecretFileVariable.ALPACA_PAPER_SECRET_KEY,
            }
        ),
        frozenset(
            {
                SecretFileVariable.ALPACA_PAPER_API_KEY,
                SecretFileVariable.PAPER_ACCOUNT_ID_HASH,
            }
        ),
        frozenset(
            {
                SecretFileVariable.ALPACA_PAPER_SECRET_KEY,
                SecretFileVariable.PAPER_ACCOUNT_ID_HASH,
            }
        ),
    ],
)
def test_paper_executor_requires_complete_paper_triplet(
    present: frozenset[SecretFileVariable],
) -> None:
    environment = {
        "AQA_CONFIG": PROFILE_PATHS[ExecutionMode.PAPER],
        "AQA_DATABASE_URL_FILE": "/run/secrets/database-url",
        **{source.value: f"/run/secrets/{source.name.lower()}" for source in present},
    }
    with pytest.raises(RuntimeSettingsError, match="required secret"):
        load_runtime_settings(
            environment,
            service=RuntimeService.PAPER_EXECUTION_WORKER,
            application_root=PROJECT_ROOT,
        )


def test_settings_composition_never_loads_secret_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_loaded(*args: object, **kwargs: object) -> None:
        raise AssertionError("settings composition must not load a secret")

    monkeypatch.setattr("adaptive_trader.platform.security.load_secret_file", fail_if_loaded)
    settings = load_runtime_settings(
        {
            "AQA_OPERATOR_TOKEN_FILE": f"/run/secrets/{SENTINEL}",
        },
        service=RuntimeService.CONTROL_API,
        application_root=PROJECT_ROOT,
    )
    assert settings.operator_token_file is not None


def test_runtime_settings_and_references_serialize_without_secret_paths() -> None:
    secret_path = f"/run/secrets/{SENTINEL}"
    settings = load_runtime_settings(
        {"AQA_OPERATOR_TOKEN_FILE": secret_path},
        service=RuntimeService.CONTROL_API,
        application_root=PROJECT_ROOT,
    )

    rendered = (
        str(settings),
        repr(settings),
        repr(settings.model_dump()),
        settings.model_dump_json(),
        json.dumps(settings.model_dump(), default=str),
    )
    assert all(secret_path not in value and SENTINEL not in value for value in rendered)
    assert settings.model_dump()["operator_token_file"] == {
        "source": "AQA_OPERATOR_TOKEN_FILE",
        "configured": True,
    }


def test_runtime_settings_are_strict_frozen_and_use_the_public_factory() -> None:
    settings = load_runtime_settings(
        {}, service=RuntimeService.MARKET_DATA_WORKER, application_root=PROJECT_ROOT
    )
    with pytest.raises(ValidationError, match="frozen"):
        settings.api_docs_enabled = True

    assert settings.model_config["strict"] is True
    assert settings.model_config["frozen"] is True
    assert settings.model_config["extra"] == "forbid"

    hostile_path = Path(f"/tmp/{SENTINEL}")
    payload = {**_runtime_model_payload(settings), "artifact_root": hostile_path}
    construction_attempts = (
        lambda: RuntimeSettings(**payload),
        lambda: RuntimeSettings.model_validate(payload),
        lambda: RuntimeSettings.model_validate_json(json.dumps(payload, default=str)),
    )
    for construct in construction_attempts:
        with pytest.raises(TypeError, match="load_runtime_settings") as captured:
            construct()
        assert SENTINEL not in str(captured.value)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None


def test_runtime_unvalidated_copy_and_serialization_paths_are_rejected() -> None:
    settings = load_runtime_settings(
        {}, service=RuntimeService.MARKET_DATA_WORKER, application_root=PROJECT_ROOT
    )
    assert settings.model_copy() is settings
    assert copy.copy(settings) is settings
    assert copy.deepcopy(settings) is settings

    invalid_updates = (
        {"service": RuntimeService.PAPER_EXECUTION_WORKER},
        {"artifact_root": Path("/tmp")},
        {"operator_token_file": SENTINEL},
    )
    for update in invalid_updates:
        with pytest.raises(TypeError, match="cannot be copied") as captured:
            settings.model_copy(update=update)
        assert SENTINEL not in str(captured.value)

    with pytest.raises(TypeError, match="cannot be replaced"):
        settings.__replace__(artifact_root=Path("/tmp"))
    with pytest.raises(TypeError, match="cannot bypass validation"):
        RuntimeSettings.model_construct()
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(settings)
    uninitialized = RuntimeSettings.__new__(RuntimeSettings)
    with pytest.raises(TypeError, match="cannot be deserialized"):
        uninitialized.__setstate__({})


def test_runtime_settings_integrate_as_immutable_pydantic_instances() -> None:
    settings = load_runtime_settings(
        {}, service=RuntimeService.MARKET_DATA_WORKER, application_root=PROJECT_ROOT
    )

    assert RuntimeSettings.model_validate(settings) is settings
    assert TypeAdapter(RuntimeSettings).validate_python(settings) is settings
    assert _RuntimeSettingsContainer(settings=settings).settings is settings


def test_runtime_settings_json_schema_exposes_no_secret_path() -> None:
    validation_schema = RuntimeSettings.model_json_schema(mode="validation")
    serialization_schema = RuntimeSettings.model_json_schema(mode="serialization")
    rendered = json.dumps((validation_schema, serialization_schema), sort_keys=True)

    assert SENTINEL not in rendered
    assert "/run/secrets" not in rendered
    assert validation_schema["not"] == {}
    assert serialization_schema["readOnly"] is True


def test_injected_environment_is_independent_of_ambient_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AQA_CONFIG", "configs/platform/paper.yaml")
    monkeypatch.setenv("APCA_API_KEY_ID", SENTINEL)
    monkeypatch.setenv("AQA_ALPACA_PAPER_SECRET_KEY_FILE", f"/run/secrets/{SENTINEL}")

    settings = load_runtime_settings(
        {}, service=RuntimeService.MARKET_DATA_WORKER, application_root=PROJECT_ROOT
    )

    assert settings.platform.profile.mode is ExecutionMode.OFFLINE
    assert settings.paper_order_enablement is PaperOrderEnablement.DISABLED


def test_invalid_service_and_environment_container_are_rejected() -> None:
    with pytest.raises(RuntimeSettingsError, match="trusted startup"):
        load_runtime_settings(
            {}, service=cast(RuntimeService, "market-data-worker"), application_root=PROJECT_ROOT
        )
    with pytest.raises(RuntimeSettingsError, match="read safely"):
        load_runtime_settings(
            cast(Mapping[str, str], []),
            service=RuntimeService.MARKET_DATA_WORKER,
            application_root=PROJECT_ROOT,
        )
