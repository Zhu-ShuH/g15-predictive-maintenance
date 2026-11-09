"""Integration tests for the ``g15`` command line (Issues #65, #51 and #110, C3).

Every test drives a real ``python -m g15`` subprocess against real files: a
small run produced by the producer, a consumer configuration, and the
artifacts the consumer writes. Nothing is mocked, because what these tests
pin down is the wiring -- argument parsing, configuration loading, the call
into C2's public consumer entry and the exit-code mapping of section 10.

The consumer itself (``consume_run`` and the SQLite schema) belongs to C2 and
is covered by ``tests/unit/test_consumer.py``; this file only proves that the
CLI hands it the right inputs and reports the outcome honestly.

The ``benchmark`` tests (Issue #51) additionally pin down that the production
window and the post-production verification cost stay two separate figures:
the CLI must never derive one from the other, and it must never present an
in-process number as the CLI total wall clock, which section 2 of
``docs/acceptance.md`` requires to be measured process-externally.
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
BENCHMARK_CONFIG = CONFIGS / "benchmark_1m.yaml"

#: A tiny scenario that keeps the benchmark tests fast.
#:
#: The shipped ``benchmark_1m.yaml`` is exercised once, on its own, to pin the
#: acceptance contour; every other benchmark test uses this one.
SMALL_BENCHMARK_CONFIG = """\
schema_version: '1.0'
scenario:
  id: bench_small
  description: 'small benchmark used by the CLI integration tests'
  seed: 42
  start_time_utc: '2026-10-14T00:00:00Z'
  device_id: 'DEV-005'
  sample_rate_hz: 100
  total_points: 10240
  chunk_points: 2560
signals:
  vibration:
    baseline: 0.5
    degradation_rate_per_sample: 0.000001
    noise_std: 0.05
    unit: 'g'
  temperature:
    baseline: 35.0
    degradation_rate_per_sample: 0.000003
    noise_std: 0.15
    unit: 'degC'
fault_generation:
  mode: manual
  stochastic_probability_per_window: 0.0
  events: []
"""


def run_cli(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
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
        cwd=cwd,
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


def text_field(stdout: str, field: str) -> str:
    """Read ``field=`` from the CLI's key=value block as text."""
    prefix = f"{field}="
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise AssertionError(f"{field!r} missing from stdout: {stdout!r}")


@pytest.fixture()
def small_benchmark_config(tmp_path: Path) -> Path:
    """Write the tiny benchmark scenario and return its path."""
    path = tmp_path / "bench_small.yaml"
    path.write_text(SMALL_BENCHMARK_CONFIG, encoding="utf-8")
    return path


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


def test_verify_help_describes_independent_shard_verification() -> None:
    """``verify`` help must not claim a comparison against ``private_truth/``.

    ``g15.reporting.verifier.verify_run`` reads only the manifest and the
    shards it names: it never opens the SQLite database and never reads
    ``private_truth/``.  The old wording ("match detections against fault
    truth") described the opposite of that public entry point (C4 P3-5 on
    #106).  The CLI subcommand is still deliberately unimplemented, so the
    ``(not implemented)`` marker stays honest and must not be dropped.
    """
    result = run_cli("--help")

    assert result.returncode == 0
    verify_lines = [
        line
        for line in result.stdout.splitlines()
        if line.strip().startswith("verify ")
    ]
    assert len(verify_lines) == 1
    help_line = verify_lines[0]
    assert "shards and manifest" in help_line
    assert "fault truth" not in help_line
    assert "not implemented" in help_line


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
# benchmark: the separated cost figures of acceptance.md section 2 (Issue #51)
# --------------------------------------------------------------------------- #


def test_benchmark_help_documents_its_argument() -> None:
    """The frozen invocation of section 10 must be discoverable."""
    result = run_cli("benchmark", "--help")

    assert result.returncode == 0
    assert "--config" in result.stdout
    assert "CLI total wall clock" in result.stdout
    assert "/usr/bin/time" in result.stdout


def test_benchmark_requires_its_config() -> None:
    """A missing required argument is an argument error, not a crash."""
    result = run_cli("benchmark")

    assert result.returncode == 2
    assert "required" in result.stderr


def test_benchmark_separates_the_production_and_verification_figures(
    small_benchmark_config: Path, tmp_path: Path
) -> None:
    """Figures 1 and 3 are printed apart; figure 2 is left to the caller."""
    result = run_cli("benchmark", "--config", str(small_benchmark_config), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""

    window = float(text_field(result.stdout, "production_window_seconds"))
    verification = float(text_field(result.stdout, "verification_seconds"))
    assert window > 0.0
    assert verification > 0.0

    # Figure 2 is the CLI total wall clock, which includes interpreter startup.
    # A process cannot observe its own startup, so the CLI must not claim it
    # measured that figure -- and must not pass an in-process number off as it.
    assert text_field(result.stdout, "cli_wallclock_seconds") == "unmeasured"

    assert text_field(result.stdout, "status") == "complete"
    assert text_field(result.stdout, "verification_passed") == "true"
    assert text_field(result.stdout, "parts") == "4"
    assert text_field(result.stdout, "total_points") == "10240"
    assert text_field(result.stdout, "scalar_value_count") == "20480"

    run_dir = Path(text_field(result.stdout, "run_dir"))
    if not run_dir.is_absolute():
        run_dir = tmp_path / run_dir
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["run_id"] == text_field(result.stdout, "run_id")


def test_benchmark_matches_the_shipped_acceptance_contour(tmp_path: Path) -> None:
    """``benchmark_1m.yaml`` must keep the 10 x (9x102400 + 78400) contour."""
    result = run_cli("benchmark", "--config", str(BENCHMARK_CONFIG), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert text_field(result.stdout, "parts") == "10"
    assert text_field(result.stdout, "total_points") == "1000000"
    assert text_field(result.stdout, "scalar_value_count") == "2000000"
    assert text_field(result.stdout, "shard_shape") == "9x102400+78400"
    assert text_field(result.stdout, "status") == "complete"
    assert text_field(result.stdout, "verification_passed") == "true"


def test_benchmark_rejects_an_invalid_config(tmp_path: Path) -> None:
    """A bad scenario document exits 2 and names the offending field."""
    config = tmp_path / "bad.yaml"
    config.write_text(
        SMALL_BENCHMARK_CONFIG + "unexpected_field: 1\n", encoding="utf-8"
    )

    result = run_cli("benchmark", "--config", str(config), cwd=tmp_path)

    assert result.returncode == 2
    assert "unexpected_field" in result.stderr
    assert "unknown field" in result.stderr


def test_benchmark_reports_a_missing_config(tmp_path: Path) -> None:
    """A missing configuration file is a configuration error, not a crash."""
    result = run_cli(
        "benchmark", "--config", str(tmp_path / "no-such.yaml"), cwd=tmp_path
    )

    assert result.returncode == 2
    assert "error:" in result.stderr
    assert not (tmp_path / "runs").exists()


def _run_injected_benchmark(
    tmp_path: Path, config: Path, body: str
) -> subprocess.CompletedProcess[str]:
    """Run ``benchmark`` through a launcher that swaps ``cli.verify_run``.

    The verifier is C3's dependency, not C3's code, so the only way to reach
    its failure branches without corrupting a real run is to replace the name
    the CLI calls.  This is still a real subprocess with a real produced run.
    """
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import sys\n"
        "import g15.cli as cli\n"
        "\n"
        "\n" + body + "\n\nsys.exit(cli.main(sys.argv[1:]))\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(launcher),
            "benchmark",
            "--config",
            str(config),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
        cwd=tmp_path,
    )


def test_benchmark_exits_four_when_verification_fails(
    small_benchmark_config: Path, tmp_path: Path
) -> None:
    """A gate that ran and failed is exit 4 -- never 0, never 5."""
    result = _run_injected_benchmark(
        tmp_path,
        small_benchmark_config,
        "class _Failed:\n"
        "    run_id = 'injected'\n"
        "    status = 'complete'\n"
        "    passed = False\n"
        "    failures = ('shard.hash_mismatch',)\n"
        "    elapsed_seconds = 0.01\n"
        "    shard_shape = '1x16'\n"
        "\n"
        "\n"
        "cli.verify_run = lambda *_args, **_kwargs: _Failed()",
    )

    assert result.returncode == 4
    assert "did not pass" in result.stderr
    assert "shard.hash_mismatch" in result.stderr
    # The figures are still printed: the operator needs to see what was
    # produced before the gate rejected it.
    assert "production_window_seconds=" in result.stdout


def test_benchmark_exits_three_when_verification_cannot_run(
    small_benchmark_config: Path, tmp_path: Path
) -> None:
    """A run the verifier cannot read at all is an I/O or schema failure."""
    result = _run_injected_benchmark(
        tmp_path,
        small_benchmark_config,
        "from g15.reporting.verifier import VerificationError\n"
        "\n"
        "\n"
        "def _boom(*_args, **_kwargs):\n"
        "    raise VerificationError('injected verification failure')\n"
        "\n"
        "\n"
        "cli.verify_run = _boom",
    )

    assert result.returncode == 3
    assert "cannot verify" in result.stderr


def test_benchmark_exits_five_on_an_unexpected_error(
    small_benchmark_config: Path, tmp_path: Path
) -> None:
    """Anything the mapping does not name stays an unexpected error."""
    result = _run_injected_benchmark(
        tmp_path,
        small_benchmark_config,
        "def _boom(*_args, **_kwargs):\n"
        "    raise RuntimeError('injected fault for test')\n"
        "\n"
        "\n"
        "cli.verify_run = _boom",
    )

    assert result.returncode == 5
    assert "unexpected" in result.stderr
    assert "RuntimeError" in result.stderr


# --------------------------------------------------------------------------- #
# simulate / report: the Issue #110 CLI wiring
# --------------------------------------------------------------------------- #

#: The smallest shipped scenario, used by both the producer fixture and the
#: end-to-end chain below.
SCENARIO_CONFIG = CONFIGS / "healthy.yaml"


def _run_injected_cli(
    tmp_path: Path, argv: tuple[str, ...], body: str
) -> subprocess.CompletedProcess[str]:
    """Run a real ``g15`` subprocess with one CLI dependency replaced.

    ``produce_run`` and ``build_report`` belong to C4/B1, not to C3.  Swapping
    the name the CLI calls is the only way to reach an unexpected failure
    branch without corrupting a real run; the child still imports ``g15.cli``
    and executes ``main`` in a separate process, with no mock framework.
    """
    launcher = tmp_path / "launcher_110.py"
    launcher.write_text(
        "import sys\n"
        "import g15.cli as cli\n"
        "\n"
        "\n" + body + "\n\nsys.exit(cli.main(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, str(launcher), *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
        cwd=tmp_path,
    )


def test_simulate_help_documents_its_arguments() -> None:
    """The frozen arguments of section 10 stay visible without a run."""
    result = run_cli("simulate", "--help")

    assert result.returncode == 0
    assert "--config" in result.stdout
    assert "--output-root" in result.stdout
    assert "not implemented" not in result.stdout


def test_report_help_documents_its_argument() -> None:
    """The report command must no longer present itself as unimplemented."""
    result = run_cli("report", "--help")

    assert result.returncode == 0
    assert "--run-dir" in result.stdout
    assert "not implemented" not in result.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ("simulate",),
        ("simulate", "--config", str(SCENARIO_CONFIG)),
        ("simulate", "--output-root", "runs"),
        ("report",),
    ],
)
def test_new_commands_require_their_arguments(argv: tuple[str, ...]) -> None:
    """A missing required argument is an argument error, not a crash."""
    result = run_cli(*argv)

    assert result.returncode == 2
    assert "required" in result.stderr


def test_simulate_rejects_an_invalid_config(tmp_path: Path) -> None:
    """A bad scenario document exits 2 and names the offending field."""
    config = tmp_path / "bad.yaml"
    config.write_text(
        SCENARIO_CONFIG.read_text(encoding="utf-8") + "unexpected_field: 1\n",
        encoding="utf-8",
    )

    result = run_cli(
        "simulate",
        "--config",
        str(config),
        "--output-root",
        str(tmp_path / "runs"),
    )

    assert result.returncode == 2
    assert "unexpected_field" in result.stderr


def test_simulate_reports_a_missing_config(tmp_path: Path) -> None:
    """A missing configuration file is a configuration error, not a crash."""
    result = run_cli(
        "simulate",
        "--config",
        str(tmp_path / "no-such.yaml"),
        "--output-root",
        str(tmp_path / "runs"),
    )

    assert result.returncode == 2
    assert "error:" in result.stderr
    assert not (tmp_path / "runs").exists()


def test_simulate_succeeds_on_a_real_run(tmp_path: Path) -> None:
    """The CLI hands a loaded config to the frozen producer entry."""
    output_root = tmp_path / "runs"
    result = run_cli(
        "simulate",
        "--config",
        str(SCENARIO_CONFIG),
        "--output-root",
        str(output_root),
    )

    assert result.returncode == 0, result.stderr
    run_dir = Path(text_field(result.stdout, "run_dir"))
    assert run_dir.is_dir()
    assert output_root in run_dir.parents
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"


def test_simulate_exits_three_on_an_unwritable_output_root(tmp_path: Path) -> None:
    """An output root below a regular file cannot receive a run directory."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    result = run_cli(
        "simulate",
        "--config",
        str(SCENARIO_CONFIG),
        "--output-root",
        str(blocked / "runs"),
    )

    assert result.returncode == 3
    assert "error:" in result.stderr


def test_simulate_exits_five_on_an_unexpected_error(tmp_path: Path) -> None:
    """Anything the producer mapping does not name stays an unexpected error."""
    result = _run_injected_cli(
        tmp_path,
        (
            "simulate",
            "--config",
            str(SCENARIO_CONFIG),
            "--output-root",
            str(tmp_path / "runs"),
        ),
        "def _boom(*_args, **_kwargs):\n"
        "    raise RuntimeError('injected producer fault')\n"
        "\n"
        "\n"
        "cli.produce_run = _boom",
    )

    assert result.returncode == 5
    assert "unexpected" in result.stderr
    assert "RuntimeError" in result.stderr


def test_report_reports_a_missing_run_dir(tmp_path: Path) -> None:
    """A run directory that does not exist is an I/O failure, not a crash."""
    result = run_cli("report", "--run-dir", str(tmp_path / "no-such-run"))

    assert result.returncode == 3
    assert "error:" in result.stderr


def test_report_requires_a_consumed_run(run_dir: Path) -> None:
    """A complete but unconsumed run has no database to report from."""
    result = run_cli("report", "--run-dir", str(run_dir))

    assert result.returncode == 3
    assert "database" in result.stderr


def _consume_for_report(run_dir: Path) -> None:
    """Consume a real run so the report builder has its database."""
    result = run_cli(
        "consume", "--run-dir", str(run_dir), "--config", str(CONSUMER_CONFIG)
    )
    assert result.returncode == 0, result.stderr


def test_report_succeeds_on_a_consumed_run(run_dir: Path) -> None:
    """Both artefacts are written and their paths reach stdout."""
    _consume_for_report(run_dir)

    result = run_cli("report", "--run-dir", str(run_dir))

    assert result.returncode == 0, result.stderr
    json_path = Path(text_field(result.stdout, "report_json"))
    markdown_path = Path(text_field(result.stdout, "report_markdown"))
    assert json_path.is_file()
    assert markdown_path.is_file()
    assert "passed" in json.loads(json_path.read_text(encoding="utf-8"))
    assert "# " in markdown_path.read_text(encoding="utf-8")


def test_report_exits_zero_for_a_diagnostic_report(run_dir: Path) -> None:
    """Issue #110: generation success is 0 even when the verdict is unusable.

    ``build_report`` legitimately writes a diagnostic report whose ``passed``
    is ``False`` or ``None`` when the evidence is incomplete.  That is not an
    acceptance gate: exit code 4 belongs to ``verify`` or a dedicated
    acceptance entry and must never leak out of the ordinary ``report``.
    """
    _consume_for_report(run_dir)

    result = run_cli("report", "--run-dir", str(run_dir))

    assert result.returncode == 0, result.stderr
    assert result.returncode != 4
    json_path = Path(text_field(result.stdout, "report_json"))
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["passed"] is None or payload["passed"] is False


def test_report_exits_five_on_an_unexpected_error(
    tmp_path: Path, run_dir: Path
) -> None:
    """Anything the builder mapping does not name stays an unexpected error."""
    result = _run_injected_cli(
        tmp_path,
        ("report", "--run-dir", str(run_dir)),
        "def _boom(*_args, **_kwargs):\n"
        "    raise RuntimeError('injected builder fault')\n"
        "\n"
        "\n"
        "cli.build_report = _boom",
    )

    assert result.returncode == 5
    assert "unexpected" in result.stderr
    assert "RuntimeError" in result.stderr


def test_simulate_consume_report_chain_from_an_empty_directory(tmp_path: Path) -> None:
    """Issue #110 acceptance: the whole chain in one fresh, empty directory."""
    empty = tmp_path / "empty"
    empty.mkdir()
    output_root = empty / "runs"

    simulated = run_cli(
        "simulate",
        "--config",
        str(SCENARIO_CONFIG),
        "--output-root",
        str(output_root),
        cwd=empty,
    )
    assert simulated.returncode == 0, simulated.stderr
    run_dir = Path(text_field(simulated.stdout, "run_dir"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"

    consumed = run_cli(
        "consume",
        "--run-dir",
        str(run_dir),
        "--config",
        str(CONSUMER_CONFIG),
        cwd=empty,
    )
    assert consumed.returncode == 0, consumed.stderr

    reported = run_cli("report", "--run-dir", str(run_dir), cwd=empty)
    assert reported.returncode == 0, reported.stderr

    assert (run_dir / "reports" / "summary.json").is_file()
    assert (run_dir / "reports" / "summary.md").is_file()
    assert (run_dir / "artifacts" / "results.sqlite").is_file()


# --------------------------------------------------------------------------- #
# Unimplemented subcommands stay explicitly unsuccessful
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        ("doctor",),
        ("verify", "--run-dir", "runs/whatever"),
    ],
)
def test_unimplemented_subcommands_fail_explicitly(argv: tuple[str, ...]) -> None:
    """Only the still-unimplemented commands must say so explicitly.

    ``simulate`` and ``report`` were wired in Issue #110 and have their own
    tests above; leaving them here would assert the opposite of the wiring.
    """
    result = run_cli(*argv)

    assert result.returncode != 0
    assert "not implemented" in result.stderr
