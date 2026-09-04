"""Strict profile composition and broker-free CLI tests."""

from __future__ import annotations

import copy
import json
import tomllib
import traceback
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from adaptive_trader.platform import (
    BrokerAdapter,
    ExecutionMode,
    ExecutionSpec,
    ExperimentConfigError,
    ExperimentDefinition,
    ExperimentHashMismatchError,
    ExperimentSpec,
    MarketDataAdapter,
    PlatformConfig,
    PlatformProfile,
    SignalProviderSpec,
    load_platform_config,
)
from adaptive_trader.platform.cli import app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = PROJECT_ROOT / "configs"
EXPERIMENT_PATH = Path("experiments/semiconductor_network_intraday_v1.yaml")
EXPERIMENT_HASH = "c4e66f5a4886215306f3d25c98676ecf48479fac41db9b67848e445c1a46e431"
PROFILE_CASES = {
    "offline.yaml": {
        "mode": ExecutionMode.OFFLINE,
        "market_data": MarketDataAdapter.FIXTURE,
        "signal": "deterministic_fixture",
        "broker": BrokerAdapter.FAKE,
        "database_required": False,
        "fallback": "sqlite",
        "profile_hash": "e4a7df7afa8b92a25ad36cb6912de1265ea18f49a2296d0ece1e1e5c7b5924ef",
        "experiment_spec_hash": (
            "020005d7201d8c3000bdc78457ee736249350350af583836a71fc01c2a08bfe6"
        ),
        "config_hash": "efd16b562f413cf00720b07018df6e1c59543dbe84060c78c357de2deb395121",
    },
    "shadow.yaml": {
        "mode": ExecutionMode.SHADOW,
        "market_data": MarketDataAdapter.ALPACA,
        "signal": "always_flat",
        "broker": BrokerAdapter.NONE,
        "database_required": True,
        "fallback": "none",
        "profile_hash": "144118f7103a686741990d821963ac0de3ce23a4d713e184f002b89934c1c232",
        "experiment_spec_hash": (
            "d2f1edb0bfcc310c152be04906c87c73f31b259fecb181b22ffb3f54f03d7aca"
        ),
        "config_hash": "580dff71b897aa597d08289092a26d2f9d35d37d7c8c2fcdaedd1126899793d9",
    },
    "paper.yaml": {
        "mode": ExecutionMode.PAPER,
        "market_data": MarketDataAdapter.ALPACA,
        "signal": "always_flat",
        "broker": BrokerAdapter.ALPACA_PAPER,
        "database_required": True,
        "fallback": "none",
        "profile_hash": "9d9f6be07726ce8154a8ea06b7eb111784a33f9c83e56b8b738529cb87427f55",
        "experiment_spec_hash": (
            "5f0c8443b89bbf28f8d3dd0d3c8f46fb363c5dc1b689ed988752ff167defbfaa"
        ),
        "config_hash": "7dcea71dc0ed99c9a6d2388d2444447fc708cb42300d950489a3dcef1e4ffc5e",
    },
}


def _profile_mapping(name: str = "offline.yaml") -> dict[str, object]:
    loaded = yaml.safe_load((CONFIG_ROOT / "platform" / name).read_text(encoding="utf-8"))
    assert type(loaded) is dict
    return copy.deepcopy(loaded)


def _write_candidate_tree(
    root: Path,
    profile: dict[str, object],
    *,
    profile_text: str | None = None,
) -> Path:
    experiment_directory = root / "experiments"
    profile_directory = root / "platform"
    experiment_directory.mkdir(parents=True, exist_ok=True)
    profile_directory.mkdir(parents=True, exist_ok=True)
    (experiment_directory / EXPERIMENT_PATH.name).write_bytes(
        (CONFIG_ROOT / EXPERIMENT_PATH).read_bytes()
    )
    candidate = profile_directory / "candidate.yaml"
    candidate.write_text(
        profile_text if profile_text is not None else yaml.safe_dump(profile, sort_keys=False),
        encoding="utf-8",
    )
    return Path("platform/candidate.yaml")


@pytest.mark.parametrize(("profile_name", "expected"), PROFILE_CASES.items())
def test_tracked_profiles_match_closed_contract_and_known_hashes(
    profile_name: str,
    expected: dict[str, object],
) -> None:
    config = load_platform_config(Path("platform") / profile_name, config_root=CONFIG_ROOT)

    assert config.profile.schema_version == 1
    assert config.profile.profile_id == str(expected["mode"])
    assert config.profile.mode is expected["mode"]
    assert config.profile.experiment_path == EXPERIMENT_PATH.as_posix()
    assert config.profile.expected_experiment_hash == EXPERIMENT_HASH
    assert config.profile.market_data_adapter is expected["market_data"]
    assert config.profile.signal_provider.id == expected["signal"]
    assert config.profile.execution.broker is expected["broker"]
    assert config.profile.execution.submission_enabled is False
    assert config.profile.execution.paper_only is True
    assert config.profile.storage.database_url_source == "AQA_DATABASE_URL_FILE"
    assert config.profile.storage.database_required is expected["database_required"]
    assert config.profile.storage.offline_fallback == expected["fallback"]
    assert config.profile.storage.artifact_root_source == "AQA_ARTIFACT_ROOT"
    assert config.experiment.experiment_id == "semiconductor_network_intraday_v1"
    assert config.experiment.experiment_version == 1
    assert config.experiment.definition_hash == EXPERIMENT_HASH
    assert config.profile.content_hash == expected["profile_hash"]
    assert config.experiment.content_hash == expected["experiment_spec_hash"]
    assert config.content_hash == expected["config_hash"]


def test_tracked_profiles_are_explicit_anchor_free_and_submission_disabled() -> None:
    for profile_name in PROFILE_CASES:
        text = (CONFIG_ROOT / "platform" / profile_name).read_text(encoding="utf-8")
        assert "&" not in text
        assert "*" not in text
        assert "<<:" not in text
        assert text.count("submission_enabled: false") == 1
        assert text.count("paper_only: true") == 1


def test_composed_configuration_is_deeply_immutable() -> None:
    config = load_platform_config(Path("platform/offline.yaml"), config_root=CONFIG_ROOT)
    original_hash = config.content_hash

    with pytest.raises(ValidationError, match="frozen"):
        config.profile.mode = ExecutionMode.SHADOW  # type: ignore[misc]
    with pytest.raises(ValidationError, match="frozen"):
        config.profile.execution.submission_enabled = True  # type: ignore[misc]
    with pytest.raises(ValidationError, match="frozen"):
        config.experiment.signal_provider.id = "changed"  # type: ignore[misc]
    assert config.content_hash == original_hash


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), "1"),
        (("mode",), True),
        (("market_data_adapter",), 1),
        (("signal_provider", "id"), 1),
        (("execution", "submission_enabled"), 0),
        (("execution", "paper_only"), 1),
        (("storage", "database_required"), 1),
    ],
)
def test_profile_rejects_cross_type_coercion(path: tuple[str, ...], value: object) -> None:
    payload = _profile_mapping()
    target = payload
    for component in path[:-1]:
        nested = target[component]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        PlatformProfile.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        (None, "unexpected"),
        ("signal_provider", "module"),
        ("signal_provider", "promotable"),
        ("execution", "trading_host"),
        ("storage", "database_url"),
    ],
)
def test_profile_rejects_unknown_fields(section: str | None, field: str) -> None:
    payload = _profile_mapping()
    target = payload if section is None else payload[section]
    assert isinstance(target, dict)
    target[field] = "prohibited"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PlatformProfile.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        (None, "mode"),
        (None, "expected_experiment_hash"),
        ("signal_provider", "id"),
        ("execution", "submission_enabled"),
        ("storage", "database_required"),
    ],
)
def test_profile_requires_every_identity_and_policy_field(section: str | None, field: str) -> None:
    payload = _profile_mapping()
    target = payload if section is None else payload[section]
    assert isinstance(target, dict)
    del target[field]

    with pytest.raises(ValidationError, match="Field required"):
        PlatformProfile.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("market_data_adapter",), "alpaca"),
        (("execution", "broker"), "none"),
        (("execution", "submission_enabled"), True),
        (("storage", "database_required"), True),
        (("storage", "offline_fallback"), "none"),
    ],
)
def test_offline_profile_rejects_cross_mode_authority(
    path: tuple[str, ...],
    value: object,
) -> None:
    payload = _profile_mapping()
    target = payload
    for component in path[:-1]:
        nested = target[component]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        PlatformProfile.model_validate(payload)


def test_generic_profile_accepts_custom_identity_and_registered_provider_name() -> None:
    payload = _profile_mapping()
    payload["profile_id"] = "research_offline"
    signal_provider = payload["signal_provider"]
    assert isinstance(signal_provider, dict)
    signal_provider["id"] = "locally_registered_provider"

    profile = PlatformProfile.model_validate(payload)
    assert profile.profile_id == "research_offline"
    assert profile.signal_provider.id == "locally_registered_provider"


@pytest.mark.parametrize(
    ("profile_name", "profile_id"),
    [("offline.yaml", "paper"), ("paper.yaml", "offline")],
)
def test_reserved_profile_id_must_match_mode(profile_name: str, profile_id: str) -> None:
    payload = _profile_mapping(profile_name)
    payload["profile_id"] = profile_id

    with pytest.raises(ValidationError, match="reserved profile_id"):
        PlatformProfile.model_validate(payload)


@pytest.mark.parametrize("profile_name", ["shadow.yaml", "paper.yaml"])
def test_deterministic_fixture_signal_provider_is_offline_only(
    tmp_path: Path,
    profile_name: str,
) -> None:
    payload = _profile_mapping(profile_name)
    signal_provider = payload["signal_provider"]
    execution = payload["execution"]
    assert isinstance(signal_provider, dict)
    assert isinstance(execution, dict)
    signal_provider["id"] = "deterministic_fixture"
    if profile_name == "paper.yaml":
        execution["submission_enabled"] = True
    candidate = _write_candidate_tree(tmp_path, payload)

    with pytest.raises(ExperimentConfigError, match="profile failed strict validation"):
        load_platform_config(candidate, config_root=tmp_path)


def test_shadow_submission_is_always_disabled_but_untracked_paper_can_represent_gate_two(
    tmp_path: Path,
) -> None:
    shadow = _profile_mapping("shadow.yaml")
    shadow_execution = shadow["execution"]
    assert isinstance(shadow_execution, dict)
    shadow_execution["submission_enabled"] = True
    with pytest.raises(ValidationError, match="submission"):
        PlatformProfile.model_validate(shadow)

    paper = _profile_mapping("paper.yaml")
    paper_execution = paper["execution"]
    assert isinstance(paper_execution, dict)
    paper_execution["submission_enabled"] = True
    candidate = _write_candidate_tree(tmp_path, paper)
    config = load_platform_config(candidate, config_root=tmp_path)
    assert config.profile.execution.submission_enabled is True
    assert config.profile.execution.paper_only is True
    assert config.profile.execution.broker is BrokerAdapter.ALPACA_PAPER


@pytest.mark.parametrize("broker", [BrokerAdapter.NONE, BrokerAdapter.FAKE])
def test_execution_spec_rejects_submission_without_paper_adapter(broker: BrokerAdapter) -> None:
    with pytest.raises(ValidationError, match="only for the Alpaca paper adapter"):
        ExecutionSpec(broker=broker, submission_enabled=True, paper_only=True)

    paper = ExecutionSpec(
        broker=BrokerAdapter.ALPACA_PAPER,
        submission_enabled=True,
        paper_only=True,
    )
    assert paper.submission_enabled is True


@pytest.mark.parametrize(
    "experiment_path",
    [
        "",
        "/tmp/experiment.yaml",
        "../experiment.yaml",
        "./experiments/x.yaml",
        "experiments/./x.yaml",
        "experiments//x.yaml",
        "experiments/",
        "a\\b.yaml",
    ],
)
def test_profile_rejects_unsafe_experiment_paths(experiment_path: str) -> None:
    payload = _profile_mapping()
    payload["experiment_path"] = experiment_path

    with pytest.raises(ValidationError):
        PlatformProfile.model_validate(payload)


def test_profile_hash_pin_is_mandatory_and_fails_closed(tmp_path: Path) -> None:
    payload = _profile_mapping()
    payload["expected_experiment_hash"] = "0" * 64
    candidate = _write_candidate_tree(tmp_path, payload)

    with pytest.raises(ExperimentHashMismatchError, match="does not match"):
        load_platform_config(candidate, config_root=tmp_path)


def test_profile_and_experiment_reads_reject_symlinks(tmp_path: Path) -> None:
    payload = _profile_mapping()
    candidate = _write_candidate_tree(tmp_path, payload)
    profile_link = tmp_path / "profile-link.yaml"
    profile_link.symlink_to(tmp_path / candidate)
    with pytest.raises(ExperimentConfigError):
        load_platform_config(Path(profile_link.name), config_root=tmp_path)

    experiment = tmp_path / EXPERIMENT_PATH
    experiment_copy = tmp_path / "experiment-copy.yaml"
    experiment_copy.write_bytes(experiment.read_bytes())
    experiment.unlink()
    experiment.symlink_to(experiment_copy)
    with pytest.raises(ExperimentConfigError):
        load_platform_config(candidate, config_root=tmp_path)

    root_link = tmp_path / "root-link"
    root_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ExperimentConfigError):
        load_platform_config(candidate, config_root=root_link)


def test_profile_loader_and_cli_reject_symlinked_config_root_ancestor(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    config_root = real_parent / "configs"
    candidate = _write_candidate_tree(config_root, _profile_mapping())
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    linked_root = linked_parent / "configs"

    with pytest.raises(ExperimentConfigError):
        load_platform_config(candidate, config_root=linked_root)

    result = CliRunner().invoke(
        app,
        [
            "config",
            "validate",
            "--config-root",
            str(linked_root),
            "--config",
            candidate.as_posix(),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr) == {
        "check": "config",
        "error": "experiment configuration could not be read",
        "status": "error",
    }


def test_profile_loader_and_cli_translate_config_root_symlink_loop(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.symlink_to(second, target_is_directory=True)
    second.symlink_to(first, target_is_directory=True)

    with pytest.raises(ExperimentConfigError, match="could not be read"):
        load_platform_config(Path("platform/offline.yaml"), config_root=first)

    result = CliRunner().invoke(
        app,
        [
            "config",
            "validate",
            "--config-root",
            str(first),
            "--config",
            "platform/offline.yaml",
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr) == {
        "check": "config",
        "error": "experiment configuration could not be read",
        "status": "error",
    }


@pytest.mark.parametrize(
    "malicious_yaml",
    [
        "schema_version: 1\nschema_version: 1\n",
        "defaults: &defaults\n  mode: offline\ncopy: *defaults\n",
        "schema_version: !!int '1'\n",
        "schema_version: 1\n---\nschema_version: 1\n",
    ],
)
def test_profile_loader_reuses_strict_yaml_boundary(
    tmp_path: Path,
    malicious_yaml: str,
) -> None:
    candidate = _write_candidate_tree(tmp_path, {}, profile_text=malicious_yaml)
    with pytest.raises(ExperimentConfigError):
        load_platform_config(candidate, config_root=tmp_path)


def test_profile_errors_are_context_free_and_do_not_disclose_input(tmp_path: Path) -> None:
    sentinel = "private-profile-sentinel-must-not-escape"
    candidate = _write_candidate_tree(tmp_path, {}, profile_text=f"{sentinel}: exposed\n")

    with pytest.raises(ExperimentConfigError) as captured:
        load_platform_config(candidate, config_root=tmp_path)

    rendered = "\n".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.args),
            "".join(traceback.format_exception(captured.value)),
        )
    )
    assert sentinel not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_composed_experiment_hash_binds_signal_and_execution_identity() -> None:
    offline = load_platform_config(Path("platform/offline.yaml"), config_root=CONFIG_ROOT)
    changed_signal = ExperimentSpec(
        schema_version=1,
        definition=offline.experiment.definition,
        definition_hash=offline.experiment.definition_hash,
        signal_provider=SignalProviderSpec(id="other_local_provider"),
        execution_mode=ExecutionMode.OFFLINE,
    )
    assert changed_signal.content_hash != offline.experiment.content_hash

    with pytest.raises(ValidationError, match="definition hash"):
        ExperimentSpec(
            schema_version=1,
            definition=offline.experiment.definition,
            definition_hash="0" * 64,
            signal_provider=offline.experiment.signal_provider,
            execution_mode=ExecutionMode.OFFLINE,
        )
    with pytest.raises(ValidationError, match="offline-only"):
        ExperimentSpec(
            schema_version=1,
            definition=offline.experiment.definition,
            definition_hash=offline.experiment.definition_hash,
            signal_provider=SignalProviderSpec(id="deterministic_fixture"),
            execution_mode=ExecutionMode.PAPER,
        )


def test_platform_config_rejects_mixed_profile_and_experiment_identity() -> None:
    offline = load_platform_config(Path("platform/offline.yaml"), config_root=CONFIG_ROOT)
    shadow = load_platform_config(Path("platform/shadow.yaml"), config_root=CONFIG_ROOT)

    with pytest.raises(ValidationError, match="identities do not match"):
        PlatformConfig(profile=offline.profile, experiment=shadow.experiment)


@pytest.mark.parametrize(
    ("field", "value"),
    [("provider", "other_provider"), ("feed", "sip"), ("adjustment", "split")],
)
def test_external_mode_must_match_definition_provider_feed_and_adjustment(
    field: str,
    value: str,
) -> None:
    experiment_payload = yaml.safe_load((CONFIG_ROOT / EXPERIMENT_PATH).read_text(encoding="utf-8"))
    assert isinstance(experiment_payload, dict)
    market_data = experiment_payload["market_data"]
    assert isinstance(market_data, dict)
    market_data[field] = value
    definition = ExperimentDefinition.model_validate(experiment_payload)

    with pytest.raises(ValidationError, match="external execution mode conflicts"):
        ExperimentSpec(
            schema_version=1,
            definition=definition,
            definition_hash=definition.content_hash,
            signal_provider=SignalProviderSpec(id="always_flat"),
            execution_mode=ExecutionMode.SHADOW,
        )


def test_cli_doctor_and_config_validate_are_deterministic_and_broker_free() -> None:
    runner = CliRunner()
    doctor_result = runner.invoke(
        app,
        ["doctor", "--config-root", str(CONFIG_ROOT), "--json"],
    )
    validate_result = runner.invoke(
        app,
        [
            "config",
            "validate",
            "--config-root",
            str(CONFIG_ROOT),
            "--config",
            "platform/paper.yaml",
            "--json",
        ],
    )

    assert doctor_result.exit_code == 0, doctor_result.output
    assert validate_result.exit_code == 0, validate_result.output
    doctor_payload = json.loads(doctor_result.stdout)
    validate_payload = json.loads(validate_result.stdout)
    assert doctor_payload == {
        "check": "doctor",
        "config_hash": PROFILE_CASES["offline.yaml"]["config_hash"],
        "experiment_hash": EXPERIMENT_HASH,
        "experiment_id": "semiconductor_network_intraday_v1",
        "mode": "offline",
        "profile": "offline",
        "status": "ok",
        "submission_enabled": False,
    }
    assert validate_payload["status"] == "ok"
    assert validate_payload["mode"] == "paper"
    assert validate_payload["submission_enabled"] is False


def test_cli_reports_safe_validation_failure(tmp_path: Path) -> None:
    sentinel = "private-cli-sentinel-must-not-escape"
    candidate = _write_candidate_tree(tmp_path, {}, profile_text=f"{sentinel}: exposed\n")
    result = CliRunner().invoke(
        app,
        [
            "config",
            "validate",
            "--config-root",
            str(tmp_path),
            "--config",
            candidate.as_posix(),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert sentinel not in result.output
    assert json.loads(result.stderr) == {
        "check": "config",
        "error": "platform profile failed strict validation",
        "status": "error",
    }


def test_new_cli_aliases_preserve_legacy_entry_points() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]

    assert project["scripts"] == {
        "adaptive-market-data": "adaptive_trader.collection.cli:main",
        "adaptive-portfolio-agent": "adaptive_trader.cli:main",
        "aqa": "adaptive_trader.platform.cli:main",
        "autonomous-quant-agent": "adaptive_trader.platform.cli:main",
    }
