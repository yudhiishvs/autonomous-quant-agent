"""Real durable fixture commands, kept separate from the in-memory demo workflow."""

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from typer.testing import CliRunner

from adaptive_trader.platform.cli import app
from adaptive_trader.platform.data_cli import DataOperationError, _publish, run_data_operation
from adaptive_trader.platform.storage.tables import aqa_bar_identities, aqa_dataset_manifests

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def config_root(tmp_path):
    root = tmp_path / "configs"
    shutil.copytree(ROOT / "configs", root)
    return root


def test_commands_persist_separate_stages_and_freeze_real_parquet(config_root):
    first = run_data_operation(
        "ingest-fixture", config_root=config_root, output=Path("outputs/ingest.json")
    )
    assert first["row_count"] > 390
    engine = create_engine(
        f"sqlite:///{config_root.parent / 'runtime/aqa-offline.sqlite3'}"
    ).execution_options(schema_translate_map={"aqa": None})
    with engine.begin() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(aqa_bar_identities)
                .where(aqa_bar_identities.c.timeframe == "15Min")
            )
            == 0
        )
    retry = run_data_operation(
        "ingest-fixture", config_root=config_root, output=Path("outputs/ingest.json")
    )
    assert retry["work_units"] == 0
    assert retry["evidence_manifest_hash"] == first["evidence_manifest_hash"]
    aggregate = run_data_operation(
        "aggregate", config_root=config_root, output=Path("outputs/aggregate.json")
    )
    assert aggregate["row_count"] == 208
    routed = CliRunner().invoke(
        app,
        [
            "data",
            "aggregate",
            "--config-root",
            str(config_root),
            "--output",
            "outputs/aggregate.json",
            "--json",
        ],
    )
    assert routed.exit_code == 0, routed.output
    assert json.loads(routed.output)["row_count"] == 208
    frozen = run_data_operation(
        "freeze", config_root=config_root, output=Path("outputs/frozen.json")
    )
    repeated = run_data_operation(
        "freeze", config_root=config_root, output=Path("outputs/frozen.json")
    )
    assert frozen["work_units"] == 1
    assert repeated["work_units"] == 0
    manifest = json.loads((config_root.parent / "outputs/frozen.json").read_text())
    assert manifest["row_count"] == 208 and not manifest["promotable"]
    assert manifest["source_mode"] == "offline_fixture"
    parquet = list(config_root.parent.rglob("*.parquet"))
    assert len(parquet) == 1
    import pyarrow.parquet as pq

    assert pq.read_table(parquet[0]).num_rows == 208
    with engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_dataset_manifests)) == 1
    engine.dispose()


def test_aggregate_and_freeze_do_not_invent_missing_source(config_root):
    for operation in ("aggregate", "freeze"):
        with pytest.raises(DataOperationError):
            run_data_operation(
                operation, config_root=config_root, output=Path(f"outputs/{operation}.json")
            )
    assert not (config_root.parent / "outputs/aggregate.json").exists()
    result = CliRunner().invoke(
        app, ["data", "aggregate", "--config-root", str(config_root), "--json"]
    )
    assert result.exit_code == 2
    assert "offline data operation failed" in result.output


def test_receipt_publication_rejects_symlinks_and_preserves_existing_content(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "app"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        _publish({"value": 1}, Path("link/result.json"), application_root=root)
    assert not list(outside.iterdir())
    target = _publish({"value": 1}, Path("output/result.json"), application_root=root)
    with pytest.raises(DataOperationError):
        _publish({"value": 2}, Path("output/result.json"), application_root=root)
    assert json.loads(target.read_text()) == {"value": 1}


@pytest.mark.parametrize("revision", ["a" * 40, "unknown"])
def test_container_provenance_binds_revision_to_actual_lock_bytes(tmp_path, revision):
    import hashlib
    import subprocess
    import sys

    from adaptive_trader.platform.data_cli import _source_provenance

    lock = tmp_path / "uv.lock"
    lock.write_text("reviewed lock bytes\n")
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "docker/write_build_provenance.py"),
            "--root",
            str(tmp_path),
            "--revision",
            revision,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if revision == "unknown":
        with pytest.raises(DataOperationError, match="missing or invalid"):
            _source_provenance(tmp_path)
        return
    assert _source_provenance(tmp_path) == (
        revision,
        True,
        hashlib.sha256(lock.read_bytes()).hexdigest(),
    )
    lock.write_text("changed lock bytes\n")
    with pytest.raises(DataOperationError, match="missing or invalid"):
        _source_provenance(tmp_path)


def test_container_provenance_refuses_writable_metadata(tmp_path):
    from adaptive_trader.platform.data_cli import _source_provenance

    (tmp_path / "source-provenance.json").write_text("{}")
    (tmp_path / "source-provenance.json").chmod(0o666)
    with pytest.raises(DataOperationError, match="unsafe"):
        _source_provenance(tmp_path)


def test_container_ci_includes_execution_and_real_offline_data_smoke():
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/container.yml").read_text())
    job = workflow["jobs"]["container"]
    targets = {entry["target"] for entry in job["strategy"]["matrix"]["include"]}
    assert "execution" in targets
    smoke = next(
        step
        for step in job["steps"]
        if step["name"]
        == "Verify durable fixture ingest aggregate and Parquet freeze without network"
    )
    assert "--network none" in smoke["run"]
    assert '["aqa", "data", operation, "--json"]' in smoke["run"]
    assert "check=True" in smoke["run"] and "timeout=120" in smoke["run"]
    scanner = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("aquasecurity/trivy-action@")
    )
    assert scanner["uses"] == "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25"
    assert scanner["with"]["version"] == "v0.72.0"
