"""Contract, known-answer, and hostile-input tests for experiment configuration."""

from __future__ import annotations

import copy
import traceback
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from adaptive_trader.platform import (
    CanonicalizationError,
    ExperimentConfigError,
    ExperimentDefinition,
    ExperimentHashMismatchError,
    RiskPolicySpec,
    SymbolRole,
    UniverseSpec,
    canonical_json_bytes,
    load_experiment,
)

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "configs"
SHIPPED_PATH = Path("experiments/semiconductor_network_intraday_v1.yaml")
EXPECTED_CONTENT_HASH = "c4e66f5a4886215306f3d25c98676ecf48479fac41db9b67848e445c1a46e431"
EXPECTED_CANONICAL_BYTES = (
    b'{"active_tradable":["AAOI","AMD","AXTI","CSCO","HLIT","INSG","NVDA","SNDK"],'
    b'"benchmark_only":["SOXX"],"context_only":["QQQ","SPY"],"excluded":["AAPL",'
    b'"AMZN","BOX","GOOGL","LCID","META","NET","OKTA","PAYC","PUBM","RBLX","RIVN",'
    b'"ROKU","SOUN","TSLA","UBER","WDAY","ZG"],"experiment_id":'
    b'"semiconductor_network_intraday_v1","experiment_version":1,"market_data":'
    b'{"adjustment":"raw","decision_timeframe":"15Min","exchange_calendar":"XNAS",'
    b'"feed":"iex","provider":"alpaca","regular_hours_only":true,"source_timeframe":'
    b'"1Min"},"risk_groups":[{"id":"compute_storage_materials","max_gross_weight":'
    b'"0.6","symbols":["AMD","AXTI","NVDA","SNDK"]},{"id":"connectivity",'
    b'"max_gross_weight":"0.6","symbols":["AAOI","CSCO","HLIT","INSG"]}],'
    b'"risk_policy":{"correlation_edge_threshold":"0.8","covariance_eigenvalue_floor":'
    b'"0.00000001","deployment_drawdown_trigger":"-0.15","edge_saturation_bps":"25",'
    b'"id":"paper_v1","max_absolute_symbol_weight":"0.15","max_cluster_gross_weight":'
    b'"0.4","max_gross_weight":"1","max_net_weight":"0.3","min_net_weight":"-0.3",'
    b'"minimum_rebalance_equity_fraction":"0.0025","session_loss_trigger":"-0.02",'
    b'"sigma_floor":"0.2","target_annualized_volatility":"0.1","version":1},'
    b'"schema_version":1,"session":{"close":"16:00:00",'
    b'"decision_deadline_delay_seconds":120,"decision_ready_delay_seconds":60,'
    b'"first_strategy_bar_close":"09:45:00","forced_flat_submit_deadline":"15:44:00",'
    b'"forced_flat_target_time":"15:43:00","last_strategy_bar_close":"14:30:00",'
    b'"open":"09:30:00","required_flat_time":"15:45:00"}}'
)


def _shipped_mapping() -> dict[str, object]:
    loaded = yaml.safe_load((CONFIG_ROOT / SHIPPED_PATH).read_text(encoding="utf-8"))
    assert type(loaded) is dict
    return copy.deepcopy(loaded)


def _write_candidate(root: Path, contents: str | bytes) -> Path:
    path = root / "candidate.yaml"
    if isinstance(contents, bytes):
        path.write_bytes(contents)
    else:
        path.write_text(contents, encoding="utf-8")
    return Path(path.name)


def _load_candidate(root: Path, contents: str | bytes) -> ExperimentDefinition:
    return load_experiment(_write_candidate(root, contents), config_root=root)


def test_shipped_experiment_matches_every_normative_value_and_known_hash() -> None:
    experiment = load_experiment(
        SHIPPED_PATH,
        config_root=CONFIG_ROOT,
        expected_hash=EXPECTED_CONTENT_HASH,
    )

    assert experiment.schema_version == 1
    assert experiment.experiment_id == "semiconductor_network_intraday_v1"
    assert experiment.experiment_version == 1
    assert experiment.active_tradable == (
        "AAOI",
        "AMD",
        "AXTI",
        "CSCO",
        "HLIT",
        "INSG",
        "NVDA",
        "SNDK",
    )
    assert experiment.benchmark_only == ("SOXX",)
    assert experiment.context_only == ("QQQ", "SPY")
    assert experiment.excluded == (
        "AAPL",
        "AMZN",
        "BOX",
        "GOOGL",
        "LCID",
        "META",
        "NET",
        "OKTA",
        "PAYC",
        "PUBM",
        "RBLX",
        "RIVN",
        "ROKU",
        "SOUN",
        "TSLA",
        "UBER",
        "WDAY",
        "ZG",
    )
    assert experiment.market_data.model_dump() == {
        "provider": "alpaca",
        "feed": "iex",
        "adjustment": "raw",
        "source_timeframe": "1Min",
        "decision_timeframe": "15Min",
        "exchange_calendar": "XNAS",
        "regular_hours_only": True,
    }
    assert experiment.session.model_dump() == {
        "open": "09:30:00",
        "close": "16:00:00",
        "first_strategy_bar_close": "09:45:00",
        "last_strategy_bar_close": "14:30:00",
        "decision_ready_delay_seconds": 60,
        "decision_deadline_delay_seconds": 120,
        "forced_flat_target_time": "15:43:00",
        "forced_flat_submit_deadline": "15:44:00",
        "required_flat_time": "15:45:00",
    }
    assert tuple(group.id for group in experiment.risk_groups) == (
        "compute_storage_materials",
        "connectivity",
    )
    assert experiment.risk_groups[0].symbols == ("AMD", "AXTI", "NVDA", "SNDK")
    assert experiment.risk_groups[1].symbols == ("AAOI", "CSCO", "HLIT", "INSG")
    assert all(group.max_gross_weight == Decimal("0.60") for group in experiment.risk_groups)
    assert experiment.risk_policy.model_dump() == {
        "id": "paper_v1",
        "version": 1,
        "max_absolute_symbol_weight": Decimal("0.15"),
        "max_gross_weight": Decimal("1.00"),
        "min_net_weight": Decimal("-0.30"),
        "max_net_weight": Decimal("0.30"),
        "max_cluster_gross_weight": Decimal("0.40"),
        "correlation_edge_threshold": Decimal("0.80"),
        "minimum_rebalance_equity_fraction": Decimal("0.0025"),
        "session_loss_trigger": Decimal("-0.02"),
        "deployment_drawdown_trigger": Decimal("-0.15"),
        "target_annualized_volatility": Decimal("0.10"),
        "sigma_floor": Decimal("0.20"),
        "edge_saturation_bps": Decimal("25"),
        "covariance_eigenvalue_floor": Decimal("0.00000001"),
    }
    assert canonical_json_bytes(experiment.hash_payload()) == EXPECTED_CANONICAL_BYTES
    assert experiment.content_hash == EXPECTED_CONTENT_HASH


def test_role_derived_allowlists_are_sorted_and_default_deny() -> None:
    experiment = load_experiment(SHIPPED_PATH, config_root=CONFIG_ROOT)

    assert tuple(SymbolRole) == (
        SymbolRole.ACTIVE_TRADABLE,
        SymbolRole.BENCHMARK_ONLY,
        SymbolRole.CONTEXT_ONLY,
        SymbolRole.EXCLUDED,
    )
    assert experiment.collection_allowlist == (
        "AAOI",
        "AMD",
        "AXTI",
        "CSCO",
        "HLIT",
        "INSG",
        "NVDA",
        "QQQ",
        "SNDK",
        "SOXX",
        "SPY",
    )
    assert experiment.order_allowlist == experiment.active_tradable
    for symbol in experiment.active_tradable:
        assert experiment.role_for(symbol) is SymbolRole.ACTIVE_TRADABLE
        assert experiment.permits_collection(symbol)
        assert experiment.permits_order(symbol)
    for symbol in (*experiment.benchmark_only, *experiment.context_only):
        assert experiment.permits_collection(symbol)
        assert not experiment.permits_order(symbol)
    for symbol in experiment.excluded:
        assert not experiment.permits_collection(symbol)
        assert not experiment.permits_order(symbol)


def test_wdc_never_satisfies_or_aliases_sndk() -> None:
    experiment = load_experiment(SHIPPED_PATH, config_root=CONFIG_ROOT)

    assert experiment.role_for("sndk") is SymbolRole.ACTIVE_TRADABLE
    assert experiment.permits_order("SNDK")
    assert not experiment.permits_order("WDC")
    assert not experiment.permits_collection("WDC")
    with pytest.raises(KeyError, match="WDC"):
        experiment.role_for("WDC")


def test_universe_normalizes_case_and_order_without_mutable_collections() -> None:
    universe = UniverseSpec.model_validate(
        {
            "active_tradable": ["nvda", "amd"],
            "benchmark_only": ["soxx"],
            "context_only": ["spy", "qqq"],
            "excluded": [],
        }
    )

    assert universe.active_tradable == ("AMD", "NVDA")
    assert universe.context_only == ("QQQ", "SPY")
    assert isinstance(universe.active_tradable, tuple)
    with pytest.raises(ValidationError, match="frozen"):
        universe.active_tradable = ("AMD",)  # type: ignore[misc]


@pytest.mark.parametrize(
    "symbol",
    ["", ".AMD", "AMD-1", "ABCDEFGHIJK", " AMD", "AMD ", "ß", "\u0391MD", 1, True],
)
def test_universe_rejects_invalid_or_coercible_symbols(symbol: object) -> None:
    with pytest.raises(ValidationError):
        UniverseSpec.model_validate(
            {
                "active_tradable": [symbol],
                "benchmark_only": [],
                "context_only": [],
                "excluded": [],
            }
        )


@pytest.mark.parametrize(
    "roles",
    [
        {
            "active_tradable": ["amd", "AMD"],
            "benchmark_only": [],
            "context_only": [],
            "excluded": [],
        },
        {"active_tradable": ["AMD"], "benchmark_only": ["amd"], "context_only": [], "excluded": []},
        {"active_tradable": [], "benchmark_only": ["SOXX"], "context_only": [], "excluded": []},
    ],
)
def test_universe_rejects_duplicates_overlap_and_empty_active(
    roles: dict[str, list[str]],
) -> None:
    with pytest.raises(ValidationError):
        UniverseSpec.model_validate(roles)


def test_experiment_models_are_strict_frozen_and_deeply_immutable() -> None:
    experiment = load_experiment(SHIPPED_PATH, config_root=CONFIG_ROOT)
    original_hash = experiment.content_hash

    with pytest.raises(ValidationError, match="frozen"):
        experiment.experiment_version = 2  # type: ignore[misc]
    with pytest.raises(ValidationError, match="frozen"):
        experiment.market_data.feed = "sip"  # type: ignore[misc]
    with pytest.raises(TypeError):
        experiment.risk_groups[0] = experiment.risk_groups[1]  # type: ignore[index]

    detached_payload = experiment.hash_payload()
    detached_payload["experiment_id"] = "changed"
    assert experiment.experiment_id == "semiconductor_network_intraday_v1"
    assert experiment.content_hash == original_hash


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        (("schema_version",), "1"),
        (("experiment_version",), True),
        (("market_data", "regular_hours_only"), 1),
        (("session", "decision_ready_delay_seconds"), "60"),
        (("risk_policy", "max_gross_weight"), 1.0),
    ],
)
def test_experiment_rejects_cross_type_coercion(
    mutation: tuple[str, ...],
    value: object,
) -> None:
    payload = _shipped_mapping()
    target = payload
    for component in mutation[:-1]:
        nested = target[component]
        assert isinstance(nested, dict)
        target = nested
    target[mutation[-1]] = value

    with pytest.raises(ValidationError):
        ExperimentDefinition.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        (None, "unexpected"),
        ("market_data", "endpoint"),
        ("session", "timezone"),
        ("risk_policy", "override"),
    ],
)
def test_unknown_fields_are_rejected(section: str | None, field: str) -> None:
    payload = _shipped_mapping()
    target = payload if section is None else payload[section]
    assert isinstance(target, dict)
    target[field] = "prohibited"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExperimentDefinition.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        (None, "experiment_id"),
        (None, "risk_groups"),
        ("market_data", "feed"),
        ("session", "open"),
        ("risk_policy", "version"),
    ],
)
def test_required_fields_have_no_implicit_defaults(section: str | None, field: str) -> None:
    payload = _shipped_mapping()
    target = payload if section is None else payload[section]
    assert isinstance(target, dict)
    del target[field]

    with pytest.raises(ValidationError, match="Field required"):
        ExperimentDefinition.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("session", "open", "9:30:00"),
        ("session", "last_strategy_bar_close", "15:44:00"),
        ("session", "decision_ready_delay_seconds", 121),
        ("risk_policy", "max_absolute_symbol_weight", "1.01"),
        ("risk_policy", "max_cluster_gross_weight", "1.01"),
        ("risk_policy", "correlation_edge_threshold", "1.01"),
        ("risk_policy", "session_loss_trigger", "0"),
        ("risk_policy", "deployment_drawdown_trigger", "-0.01"),
        ("risk_policy", "sigma_floor", "0"),
        ("risk_policy", "covariance_eigenvalue_floor", "NaN"),
        ("risk_policy", "edge_saturation_bps", Decimal("1E+100")),
        ("risk_policy", "edge_saturation_bps", "1" * 129),
    ],
)
def test_invalid_session_and_risk_semantics_are_rejected(
    section: str,
    field: str,
    value: object,
) -> None:
    payload = _shipped_mapping()
    target = payload[section]
    assert isinstance(target, dict)
    target[field] = value

    with pytest.raises(ValidationError):
        ExperimentDefinition.model_validate(payload)


@pytest.mark.parametrize(
    ("last_bar_close", "deadline_delay"),
    [("15:41:00", 120), ("15:42:00", 3_600)],
)
def test_final_decision_deadline_must_precede_forced_flattening(
    last_bar_close: str,
    deadline_delay: int,
) -> None:
    payload = _shipped_mapping()
    session = payload["session"]
    assert isinstance(session, dict)
    session["last_strategy_bar_close"] = last_bar_close
    session["decision_deadline_delay_seconds"] = deadline_delay

    with pytest.raises(ValidationError, match="deadline must precede forced flattening"):
        ExperimentDefinition.model_validate(payload)


def test_decimal_policy_comparisons_are_independent_of_ambient_precision() -> None:
    payload = _shipped_mapping()["risk_policy"]
    assert isinstance(payload, dict)
    payload.update(
        {
            "max_absolute_symbol_weight": "0.10",
            "max_gross_weight": "0.123456789012345678901234567890123",
            "min_net_weight": "-0.123456789012345678901234567895",
            "max_net_weight": "0.10",
            "max_cluster_gross_weight": "0.10",
        }
    )

    with pytest.raises(ValidationError, match="net weight limits"):
        RiskPolicySpec.model_validate(payload)


def test_risk_groups_must_be_unique_active_partition_with_bounded_limits() -> None:
    payload = _shipped_mapping()
    groups = payload["risk_groups"]
    assert isinstance(groups, list)
    first = groups[0]
    second = groups[1]
    assert isinstance(first, dict)
    assert isinstance(second, dict)

    second["id"] = first["id"]
    with pytest.raises(ValidationError, match="risk group ids must be unique"):
        ExperimentDefinition.model_validate(payload)

    payload = _shipped_mapping()
    groups = payload["risk_groups"]
    assert isinstance(groups, list) and isinstance(groups[0], dict)
    groups[0]["symbols"] = ["AAOI", "CSCO", "HLIT", "SOXX"]
    with pytest.raises(ValidationError, match="partition active_tradable"):
        ExperimentDefinition.model_validate(payload)

    payload = _shipped_mapping()
    groups = payload["risk_groups"]
    assert isinstance(groups, list) and isinstance(groups[0], dict)
    groups[0]["max_gross_weight"] = "1.01"
    with pytest.raises(ValidationError):
        ExperimentDefinition.model_validate(payload)


def test_expected_hash_is_strict_and_mismatch_fails_closed() -> None:
    assert (
        load_experiment(
            SHIPPED_PATH,
            config_root=CONFIG_ROOT,
            expected_hash=EXPECTED_CONTENT_HASH,
        ).content_hash
        == EXPECTED_CONTENT_HASH
    )

    with pytest.raises(ExperimentHashMismatchError, match="does not match"):
        load_experiment(SHIPPED_PATH, config_root=CONFIG_ROOT, expected_hash="0" * 64)
    for invalid in ("", "A" * 64, "g" * 64, 1):
        with pytest.raises(ExperimentConfigError, match="expected_hash"):
            load_experiment(  # type: ignore[arg-type]
                SHIPPED_PATH,
                config_root=CONFIG_ROOT,
                expected_hash=invalid,
            )


@pytest.mark.parametrize("field", ["experiment_version", "risk_policy.version"])
@pytest.mark.parametrize("expected_hash", [None, EXPECTED_CONTENT_HASH])
def test_versions_cannot_escape_the_canonical_integer_domain(
    tmp_path: Path,
    field: str,
    expected_hash: str | None,
) -> None:
    payload = _shipped_mapping()
    if field == "experiment_version":
        payload[field] = 10**100
    else:
        risk_policy = payload["risk_policy"]
        assert isinstance(risk_policy, dict)
        risk_policy["version"] = 10**100
    path = _write_candidate(tmp_path, yaml.safe_dump(payload, sort_keys=False))

    with pytest.raises(ExperimentConfigError) as captured:
        load_experiment(path, config_root=tmp_path, expected_hash=expected_hash)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_loader_calculates_and_translates_hash_failures_without_a_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_candidate(tmp_path, (CONFIG_ROOT / SHIPPED_PATH).read_bytes())

    def fail_hash(_value: object) -> str:
        raise CanonicalizationError("internal structural failure")

    monkeypatch.setattr("adaptive_trader.platform.config.sha256_hex", fail_hash)
    with pytest.raises(ExperimentConfigError, match="could not be canonically hashed") as captured:
        load_experiment(path, config_root=tmp_path)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_yaml_comments_whitespace_and_mapping_order_do_not_change_hash(tmp_path: Path) -> None:
    payload = _shipped_mapping()
    reordered = yaml.safe_dump(payload, sort_keys=True, allow_unicode=False)
    candidate = "# semantic content is unchanged\n\n" + reordered

    assert _load_candidate(tmp_path, candidate).content_hash == EXPECTED_CONTENT_HASH


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        (("experiment_version",), 2),
        (("excluded",), ["AAPL", "WDC"]),
        (("market_data", "feed"), "sip"),
        (("session", "decision_ready_delay_seconds"), 61),
        (("risk_groups", 0, "max_gross_weight"), "0.59"),
        (("risk_policy", "edge_saturation_bps"), "26"),
    ],
)
def test_any_logical_section_change_breaks_a_pinned_hash(
    tmp_path: Path,
    mutation: tuple[str | int, ...],
    value: object,
) -> None:
    payload: object = _shipped_mapping()
    target = payload
    for component in mutation[:-1]:
        if isinstance(component, int):
            assert isinstance(target, list)
            target = target[component]
        else:
            assert isinstance(target, dict)
            target = target[component]
    final = mutation[-1]
    if isinstance(final, int):
        assert isinstance(target, list)
        target[final] = value
    else:
        assert isinstance(target, dict)
        target[final] = value
    candidate = yaml.safe_dump(payload, sort_keys=False)
    path = _write_candidate(tmp_path, candidate)

    with pytest.raises(ExperimentHashMismatchError):
        load_experiment(path, config_root=tmp_path, expected_hash=EXPECTED_CONTENT_HASH)


@pytest.mark.parametrize(
    ("old", "new"),
    [("regular_hours_only: true", "regular_hours_only: yes"), ('"0.60"', "0.60")],
)
def test_yaml_implicit_coercions_are_rejected(tmp_path: Path, old: str, new: str) -> None:
    contents = (CONFIG_ROOT / SHIPPED_PATH).read_text(encoding="utf-8").replace(old, new, 1)

    with pytest.raises(ExperimentConfigError):
        _load_candidate(tmp_path, contents)


@pytest.mark.parametrize("integer", ["1:00", "0x3c", "074", "0b111100", "+60"])
def test_yaml_noncanonical_integer_spellings_are_rejected(
    tmp_path: Path,
    integer: str,
) -> None:
    contents = (
        (CONFIG_ROOT / SHIPPED_PATH)
        .read_text(encoding="utf-8")
        .replace(
            "decision_ready_delay_seconds: 60",
            f"decision_ready_delay_seconds: {integer}",
        )
    )

    with pytest.raises(ExperimentConfigError):
        _load_candidate(tmp_path, contents)


@pytest.mark.parametrize(
    "malicious_yaml",
    [
        "schema_version: 1\nschema_version: 1\n",
        "market_data:\n  provider: alpaca\n  provider: alpaca\n",
        "defaults: &defaults\n  provider: alpaca\ncopy: *defaults\n",
        "market_data:\n  <<: {provider: alpaca}\n",
        "schema_version: !!int '1'\n",
        "schema_version: 1\n---\nschema_version: 1\n",
    ],
)
def test_yaml_duplicate_keys_anchors_merges_tags_and_documents_are_rejected(
    tmp_path: Path,
    malicious_yaml: str,
) -> None:
    with pytest.raises(ExperimentConfigError):
        _load_candidate(tmp_path, malicious_yaml)


@pytest.mark.parametrize(
    "contents",
    [
        b"\xff\xfe",
        b"\xef\xbb\xbfschema_version: 1\n",
        b"schema_version: 1\x00\n",
        b"x" * 65_537,
    ],
)
def test_invalid_text_and_oversized_files_are_rejected(tmp_path: Path, contents: bytes) -> None:
    with pytest.raises(ExperimentConfigError):
        _load_candidate(tmp_path, contents)


@pytest.mark.parametrize(
    "malicious_yaml",
    ["schema_version: 2023-02-30\n", "schema_version: " + ("9" * 5_000) + "\n"],
)
def test_yaml_constructor_failures_are_translated_without_context(
    tmp_path: Path,
    malicious_yaml: str,
) -> None:
    with pytest.raises(ExperimentConfigError) as captured:
        _load_candidate(tmp_path, malicious_yaml)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("value: " + ("[" * 33) + "0" + ("]" * 33) + "\n", "depth limit"),
        ("\n".join(f"key_{index}: value" for index in range(2_050)), "node limit"),
    ],
)
def test_yaml_structural_resource_limits_are_enforced(
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    with pytest.raises(ExperimentConfigError, match=message):
        _load_candidate(tmp_path, contents)


def test_loader_confines_reads_to_regular_nonsymlink_files(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-experiment.yaml"
    outside.write_text("schema_version: 1\n", encoding="utf-8")
    (tmp_path / "link.yaml").symlink_to(outside)
    (tmp_path / "subdir").mkdir()
    (tmp_path / "real-dir").mkdir()
    (tmp_path / "real-dir" / "nested.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (tmp_path / "linked-dir").symlink_to(tmp_path / "real-dir", target_is_directory=True)

    invalid_paths = (
        Path("missing.yaml"),
        Path("subdir"),
        Path("link.yaml"),
        Path("linked-dir/nested.yaml"),
        Path("../outside-experiment.yaml"),
        outside,
    )
    for path in invalid_paths:
        with pytest.raises(ExperimentConfigError):
            load_experiment(path, config_root=tmp_path)

    with pytest.raises(ExperimentConfigError, match="absolute pathlib Path"):
        load_experiment(Path("candidate.yaml"), config_root=Path("configs"))
    with pytest.raises(ExperimentConfigError, match="pathlib Path"):
        load_experiment("candidate.yaml", config_root=tmp_path)  # type: ignore[arg-type]

    (tmp_path / "candidate.yaml").write_bytes((CONFIG_ROOT / SHIPPED_PATH).read_bytes())
    root_link = tmp_path / "root-link"
    root_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ExperimentConfigError):
        load_experiment(Path("candidate.yaml"), config_root=root_link)


def test_path_subclass_cannot_override_the_validated_components(tmp_path: Path) -> None:
    concrete_path_type = type(Path())

    class HostilePath(concrete_path_type):  # type: ignore[valid-type,misc]
        @property
        def parts(self) -> tuple[str, ...]:
            return (str(CONFIG_ROOT / "experiments"), SHIPPED_PATH.name)

        def is_absolute(self) -> bool:
            return False

    with pytest.raises(ExperimentConfigError):
        load_experiment(HostilePath("harmless.yaml"), config_root=tmp_path)


def test_loader_errors_do_not_disclose_attacker_content_or_exception_context(
    tmp_path: Path,
) -> None:
    sentinel = "private-sentinel-must-not-escape"
    candidate = _write_candidate(tmp_path, f"{sentinel}: exposed\n")

    with pytest.raises(ExperimentConfigError) as captured:
        load_experiment(candidate, config_root=tmp_path)

    error = captured.value
    rendered = "\n".join(
        (
            str(error),
            repr(error),
            repr(error.args),
            "".join(traceback.format_exception(error)),
        )
    )
    assert sentinel not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None
