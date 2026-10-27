"""Integration tests for the ``g15`` command line (Issue #65, owner C3).

Every test drives a real ``python -m g15`` subprocess against real files: a
small run produced by the producer, a consumer configuration, and the
artifacts the consumer writes. Nothing is mocked, because what these tests
pin down is the wiring -- argument parsing, configuration loading, the call
into C2's public consumer entry and the exit-code mapping of section 10.

The consumer itself (``consume_run`` and the SQLite schema) belongs to C2 and
is covered by ``tests/unit/test_consumer.py``; this file only proves that the
CLI hands it the right inputs and reports the outcome honestly.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from g15.config import load_config
from g15.pipeline.producer import produce_run

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "configs"
CONSUMER_CONFIG = CONFIGS / "consumer.yaml"


def run_cli(*argv: str) -> subprocess.CompletedProcess[str]:
    """Run ``python -m g15`` in a real subprocess and capture everything."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "g15", *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    """A real, complete run directory from the smallest shipped scenario.

    Function-scoped on purpose: some tests below mutate the run directory
    (manifest status), and a shared fixture would leak that mutation into
    the tests that run after them.
    """
    config = load_config(CONFIGS / "healthy.yaml")
    return produce_run(config, tmp_path / "runs")


def summary_field(stdout: str, field: str) -> int:
    """Read ``field=`` from the CLI's one-line consumption summary."""
    prefix = f"{field}="
    for token in stdout.replace("\n", " ").split():
        if token.startswith(prefix):
            return int(token[len(prefix) :].rstrip(","))
    raise AssertionError(f"{field!r} missing from summary: {stdout!r}")


# --------------------------------------------------------------------------- #
# Help and argument parsing
# --------------------------------------------------------------------------- #


def test_top_level_help_lists_every_command() -> None:
    """``--help`` must stay honest about the whole frozen command set."""
    result = run_cli("--help")

    assert result.returncode == 0
    for command in (
        "validate",
        "simulate",
        "consume",
        "report",
        "verify",
        "benchmark",
        "doctor",
    ):
        assert command in result.stdout


def test_consume_help_documents_its_arguments() -> None:
    """The frozen arguments of section 10 must be visible without a run."""
    result = run_cli("consume", "--help")

    assert result.returncode == 0
    assert "--run-dir" in result.stdout
    assert "--config" in result.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ("consume",),
        ("consume", "--run-dir", "runs/whatever"),
        ("consume", "--config", str(CONSUMER_CONFIG)),
    ],
)
def test_consume_requires_its_arguments(argv: tuple[str, ...]) -> None:
    """A missing required argument is an argument error, not a crash."""
    result = run_cli(*argv)

    assert result.returncode == 2
    assert "required" in result.stderr


# --------------------------------------------------------------------------- #
# Configuration errors (exit code 2)
# --------------------------------------------------------------------------- #


def test_consume_rejects_an_invalid_consumer_config(tmp_path: Path) -> None:
    """A bad consumer document exits 2 and names the offending field."""
    rules = (CONFIGS / "health_rules.yaml").resolve().as_posix()
    (tmp_path / "consumer.yaml").write_text(
        "schema_version: '1.0'\n"
        "window_points: 256\n"
        "incomplete_window: drop\n"
        f"health_rules_file: {rules}\n"
        "chunk_points: 102400\n",
        encoding="utf-8",
    )

    result = run_cli(
        "consume",
        "--run-dir",
        str(tmp_path / "no-such-run"),
        "--config",
        str(tmp_path / "consumer.yaml"),
    )

    assert result.returncode == 2
    assert "chunk_points" in result.stderr
    assert "unknown field" in result.stderr


def test_consume_reports_a_missing_config(tmp_path: Path) -> None:
    """A configuration path that does not exist is a user error."""
    result = run_cli(
        "consume",
        "--run-dir",
        str(tmp_path / "no-such-run"),
        "--config",
        str(tmp_path / "gone.yaml"),
    )

    assert result.returncode == 2
    assert "gone.yaml" in result.stderr


# --------------------------------------------------------------------------- #
# Run-level failures (exit code 3)
# --------------------------------------------------------------------------- #


def test_consume_reports_a_missing_manifest(tmp_path: Path) -> None:
    """A run directory without a manifest cannot be consumed."""
    (tmp_path / "empty-run").mkdir()

    result = run_cli(
        "consume",
        "--run-dir",
        str(tmp_path / "empty-run"),
        "--config",
        str(CONSUMER_CONFIG),
    )

    assert result.returncode == 3
    assert "manifest" in result.stderr


def test_consume_reports_a_malformed_manifest(tmp_path: Path) -> None:
    """Unparseable JSON is an I/O failure, never a silent success."""
    run = tmp_path / "broken-run"
    run.mkdir()
    (run / "manifest.json").write_text("{ not json", encoding="utf-8")

    result = run_cli("consume", "--run-dir", str(run), "--config", str(CONSUMER_CONFIG))

    assert result.returncode == 3
    assert "manifest" in result.stderr


def test_consume_refuses_an_incomplete_run(run_dir: Path) -> None:
    """Only a complete run may be consumed (section 5.2)."""
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "failed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = run_cli(
        "consume", "--run-dir", str(run_dir), "--config", str(CONSUMER_CONFIG)
    )

    assert result.returncode == 3
    assert "failed" in result.stderr
    assert "complete" in result.stderr


def test_consume_refuses_a_window_grid_mismatch(tmp_path: Path, run_dir: Path) -> None:
    """``chunk_points % window_points`` is checked before anything is written.

    The shipped ``healthy.yaml`` shards are 102400 samples long, which no
    whole number of 300-sample windows can fill.
    """
    rules = (CONFIGS / "health_rules.yaml").resolve().as_posix()
    config_text = tmp_path / "consumer.yaml"
    config_text.write_text(
        "schema_version: '1.0'\n"
        "window_points: 300\n"
        "incomplete_window: drop\n"
        f"health_rules_file: {rules}\n",
        encoding="utf-8",
    )

    result = run_cli(
        "consume",
        "--run-dir",
        str(run_dir),
        "--config",
        str(config_text),
    )

    assert result.returncode == 3
    assert "window_points" in result.stderr
    # The refusal happens before any write, so the database -- which the
    # consumer may have created and initialised -- holds no run row.
    database = run_dir / "artifacts" / "results.sqlite"
    if database.exists():
        with sqlite3.connect(database) as connection:
            stored = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert stored == 0


# --------------------------------------------------------------------------- #
# Success and idempotency (exit code 0)
# --------------------------------------------------------------------------- #


def test_consume_succeeds_on_a_real_run(run_dir: Path) -> None:
    """The acceptance path: parse, load, call the entry, report the summary."""
    result = run_cli(
        "consume", "--run-dir", str(run_dir), "--config", str(CONSUMER_CONFIG)
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(f"Consumed {run_dir.name}: ")
    assert summary_field(result.stdout, "shards_processed") == 2
    assert summary_field(result.stdout, "windows_written") > 0

    database = run_dir / "artifacts" / "results.sqlite"
    assert database.is_file()
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "runs" in tables


def test_repeat_consume_is_idempotent(run_dir: Path) -> None:
    """Consuming the same run twice must not double-count anything."""
    first = run_cli(
        "consume", "--run-dir", str(run_dir), "--config", str(CONSUMER_CONFIG)
    )
    second = run_cli(
        "consume", "--run-dir", str(run_dir), "--config", str(CONSUMER_CONFIG)
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert summary_field(first.stdout, "windows_written") > 0
    # The second pass recognises the shards and writes nothing new.
    assert summary_field(second.stdout, "shards_processed") == 0
    assert summary_field(second.stdout, "shards_skipped") == 2
    assert summary_field(second.stdout, "windows_written") == 0

    database = run_dir / "artifacts" / "results.sqlite"
    with sqlite3.connect(database) as connection:
        stored_windows = connection.execute(
            "SELECT COUNT(*) FROM feature_windows"
        ).fetchone()[0]
    assert stored_windows == summary_field(first.stdout, "windows_written")


# --------------------------------------------------------------------------- #
# Drop reporting (A T76/T78: stderr visibility for dropped samples)
# --------------------------------------------------------------------------- #


def _write_scenario(
    tmp_path: Path,
    *,
    total_points: int,
    chunk_points: int,
    seed: int = 42,
) -> Path:
    """Write a minimal scenario YAML and return the loaded run directory.

    The scenario uses zero degradation so the only variable is the point
    count and the chunk size -- exactly what the drop tests need.
    """
    scenario_text = (
        "schema_version: '1.0'\n"
        "scenario:\n"
        f"  id: drop_test_{total_points}_{chunk_points}\n"
        f"  description: 'drop-reporting fixture'\n"
        f"  seed: {seed}\n"
        "  start_time_utc: '2026-10-14T00:00:00Z'\n"
        "  device_id: 'DEV-DROP'\n"
        "  sample_rate_hz: 100\n"
        f"  total_points: {total_points}\n"
        f"  chunk_points: {chunk_points}\n"
        "signals:\n"
        "  vibration:\n"
        "    baseline: 0.5\n"
        "    degradation_rate_per_sample: 0.0\n"
        "    noise_std: 0.05\n"
        "    unit: 'g'\n"
        "  temperature:\n"
        "    baseline: 35.0\n"
        "    degradation_rate_per_sample: 0.0\n"
        "    noise_std: 0.15\n"
        "    unit: 'degC'\n"
        "fault_generation:\n"
        "  mode: manual\n"
        "  stochastic_probability_per_window: 0.0\n"
        "  events: []\n"
    )
    scenario_file = tmp_path / "scenario.yaml"
    scenario_file.write_text(scenario_text, encoding="utf-8")
    config = load_config(scenario_file)
    return produce_run(config, tmp_path / "runs")


def _write_consumer(tmp_path: Path, *, window_points: int) -> Path:
    """Write a consumer YAML with the given window size."""
    rules = (CONFIGS / "health_rules.yaml").resolve().as_posix()
    consumer_file = tmp_path / "consumer.yaml"
    consumer_file.write_text(
        "schema_version: '1.0'\n"
        f"window_points: {window_points}\n"
        "incomplete_window: drop\n"
        f"health_rules_file: {rules}\n",
        encoding="utf-8",
    )
    return consumer_file


def test_zero_window_drop_warns_on_stderr(tmp_path: Path) -> None:
    """100 samples cut on a 256-window grid: zero windows, 100 dropped.

    A T78: ``shards_processed > 0`` and ``windows_dropped > 0`` must produce
    a stderr warning even though the exit code is 0 (section 8.1 allows the
    tail to be dropped).
    """
    run = _write_scenario(tmp_path, total_points=100, chunk_points=100)
    consumer = _write_consumer(tmp_path, window_points=256)

    result = run_cli("consume", "--run-dir", str(run), "--config", str(consumer))

    assert result.returncode == 0, result.stderr
    assert summary_field(result.stdout, "shards_processed") == 1
    assert summary_field(result.stdout, "windows_written") == 0
    assert summary_field(result.stdout, "windows_dropped") == 100
    assert "warning" in result.stderr
    assert "100" in result.stderr
    assert "dropped" in result.stderr


def test_partial_drop_with_windows_warns_on_stderr(tmp_path: Path) -> None:
    """10240 samples on a 7000-window grid: 1 window, 3240 dropped.

    A T78: a run that produces windows AND drops samples must still warn.
    """
    run = _write_scenario(tmp_path, total_points=10240, chunk_points=10240)
    consumer = _write_consumer(tmp_path, window_points=7000)

    result = run_cli("consume", "--run-dir", str(run), "--config", str(consumer))

    assert result.returncode == 0, result.stderr
    assert summary_field(result.stdout, "shards_processed") == 1
    assert summary_field(result.stdout, "windows_written") == 1
    assert summary_field(result.stdout, "windows_dropped") == 3240
    assert "warning" in result.stderr
    assert "3240" in result.stderr


def test_idempotent_reconsume_does_not_false_warn(run_dir: Path) -> None:
    """A second consume that skips every shard must not emit a drop warning.

    A T76: ``shards_processed == 0`` and ``windows_dropped == 0`` -- the
    idempotent path -- must stay silent on stderr even if the first pass
    dropped samples.
    """
    first = run_cli(
        "consume", "--run-dir", str(run_dir), "--config", str(CONSUMER_CONFIG)
    )
    assert first.returncode == 0, first.stderr

    second = run_cli(
        "consume", "--run-dir", str(run_dir), "--config", str(CONSUMER_CONFIG)
    )
    assert second.returncode == 0, second.stderr
    assert summary_field(second.stdout, "shards_processed") == 0
    assert summary_field(second.stdout, "windows_dropped") == 0
    # No warning when nothing was processed and nothing was dropped.
    assert "warning" not in second.stderr


def test_unexpected_exception_exits_five(tmp_path: Path, run_dir: Path) -> None:
    """A non-ConumptionError raised inside ``main`` maps to exit 5 (section 10).

    A T80: the ``except Exception`` branch in ``main()`` is not dead code --
    it must catch a genuinely unexpected failure (not a ``ConsumptionError``,
    which already maps to 3) and report it on stderr with the exit code 5.

    The injection is done via a launcher script so the subprocess stays
    real: no mock framework, no in-process ``capsys`` -- the child process
    actually imports ``g15.cli``, patches ``consume_run`` to raise
    ``RuntimeError``, calls ``main`` and exits with its return value.
    """
    launcher = tmp_path / "inject_fault.py"
    launcher.write_text(
        "import sys\n"
        "from g15 import cli\n"
        "\n"
        "\n"
        "def _boom(*_args, **_kwargs):\n"
        "    raise RuntimeError('injected fault for test')\n"
        "\n"
        "\n"
        "cli.consume_run = _boom\n"
        "sys.exit(cli.main(sys.argv[1:]))\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            str(launcher),
            "consume",
            "--run-dir",
            str(run_dir),
            "--config",
            str(CONSUMER_CONFIG),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )

    assert result.returncode == 5
    assert "unexpected" in result.stderr
    assert "RuntimeError" in result.stderr


# --------------------------------------------------------------------------- #
# Unimplemented subcommands stay explicitly unsuccessful
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        ("doctor",),
        (
            "simulate",
            "--config",
            str(CONFIGS / "healthy.yaml"),
            "--output-root",
            "runs",
        ),
        ("report", "--run-dir", "runs/whatever"),
        ("verify", "--run-dir", "runs/whatever"),
        ("benchmark", "--config", str(CONFIGS / "benchmark_1m.yaml")),
    ],
)
def test_unimplemented_subcommands_fail_explicitly(argv: tuple[str, ...]) -> None:
    """Registered but unimplemented commands must never look successful."""
    result = run_cli(*argv)

    assert result.returncode != 0
    assert "not implemented" in result.stderr
