"""Command line entry point for the G15 sandbox.

``validate`` has been implemented since Issue #4, ``consume`` since Issue #65
and ``benchmark`` since Issue #51; the remaining subcommands of
``docs/interface.md`` section 10 are registered with their frozen arguments so
that ``--help`` stays honest, and they exit non-zero with an explicit message
instead of pretending to succeed.

Exit codes (``docs/interface.md`` section 10):

* ``0`` success;
* ``2`` invalid arguments or invalid configuration;
* ``3`` I/O or schema failure;
* ``4`` an acceptance gate that actually ran did not pass;
* ``5`` unexpected error, also used for "subcommand not implemented yet".

The CLI performs argument parsing, command dispatch and exit-code mapping only;
it contains no simulation, scoring, alarm or SQL logic.  ``benchmark`` prints
the producer's own measurement window and the independent verifier's own
elapsed time; it never re-derives either number from wall-clock arithmetic.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .config import ConfigError, load_config, load_consumer_config
from .pipeline.consumer import ConsumptionError, consume_run
from .pipeline.producer import produce_run_detailed
from .pipeline.schema import DataContractError
from .reporting.verifier import VerificationError, verify_run
from .storage.database import DatabaseError
from .storage.repository import StorageError

#: Successful run.
EXIT_OK = 0
#: Invalid arguments or invalid configuration.
EXIT_INVALID = 2
#: I/O or schema failure.
EXIT_IO = 3
#: An acceptance gate that actually ran did not pass.
EXIT_ACCEPTANCE = 4
#: Unexpected error; also used by the not-yet-implemented subcommands.
EXIT_UNEXPECTED = 5

#: Location of the consumer database inside a run directory (section 11).
DATABASE_RELATIVE_PATH = Path("artifacts") / "results.sqlite"

#: Output root used by ``benchmark``.
#:
#: Section 10 freezes the benchmark invocation as
#: ``python -m g15 benchmark --config configs/benchmark_1m.yaml``, so there is
#: no ``--output-root`` option to pass.  The documented layout of section 11 is
#: ``runs/<run_id>`` relative to the working directory, and every invocation
#: creates a fresh ``<run_id>``, which keeps the runs independent without an
#: extra flag (a new public option would be a section 13 change).
BENCHMARK_OUTPUT_ROOT = Path("runs")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with every frozen subcommand.

    Returns:
        A parser exposing ``validate`` plus the six subcommands that later
        issues will implement.
    """
    parser = argparse.ArgumentParser(
        prog="g15",
        description="G15 predictive-maintenance sandbox (configuration gate)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    validate = subparsers.add_parser(
        "validate", help="validate a scenario YAML against the config contract"
    )
    validate.add_argument(
        "--config", required=True, help="path to the scenario YAML file"
    )

    simulate = subparsers.add_parser(
        "simulate", help="generate NPZ shards for a scenario (not implemented)"
    )
    simulate.add_argument("--config", required=True, help="path to the scenario YAML")
    simulate.add_argument(
        "--output-root", required=True, help="directory that receives runs/<run_id>"
    )

    consume = subparsers.add_parser(
        "consume", help="consume a run into its SQLite database (Issue #65)"
    )
    consume.add_argument("--run-dir", required=True, help="path to runs/<run_id>")
    consume.add_argument("--config", required=True, help="path to consumer.yaml")

    report = subparsers.add_parser(
        "report", help="build JSON and Markdown reports (not implemented)"
    )
    report.add_argument("--run-dir", required=True, help="path to runs/<run_id>")

    verify = subparsers.add_parser(
        "verify", help="match detections against fault truth (not implemented)"
    )
    verify.add_argument("--run-dir", required=True, help="path to runs/<run_id>")

    benchmark = subparsers.add_parser(
        "benchmark",
        help="publish one benchmark run and report the separated cost figures",
        description=(
            "Generate one run inside the producer's official measurement "
            "window, then verify it independently. The production window and "
            "the verification cost are printed as separate figures. The CLI "
            "total wall clock is measured outside this process (for example "
            "with /usr/bin/time -v), because a process cannot observe its own "
            "interpreter startup; peak RSS is measured the same way."
        ),
    )
    benchmark.add_argument("--config", required=True, help="path to benchmark_1m.yaml")

    subparsers.add_parser("doctor", help="check the environment (not implemented)")

    return parser


def _run_validate(path: Path) -> int:
    """Validate one scenario configuration file.

    Args:
        path: Scenario YAML to validate.

    Returns:
        ``0`` when the document is valid, ``2`` when it is not.
    """
    try:
        config = load_config(path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return EXIT_INVALID

    scenario = config.scenario
    print(
        f"Configuration OK: {path} "
        f"(scenario={scenario.scenario_id}, total_points={scenario.total_points}, "
        f"chunk_points={scenario.chunk_points}, "
        f"events={len(config.fault_generation.events)})"
    )
    return EXIT_OK


def _run_consume(run_dir: Path, config_path: Path) -> int:
    """Consume one run into its SQLite database.

    The database lives at ``run_dir/artifacts/results.sqlite`` as laid out by
    ``docs/interface.md`` section 11, so no extra option is needed and the
    report command can find the same file.

    Args:
        run_dir: Directory holding ``manifest.json``.
        config_path: Consumer YAML to load.

    Returns:
        ``0`` on success, ``2`` for a bad configuration, ``3`` when the run
        or the database cannot be consumed.
    """
    try:
        config = load_consumer_config(config_path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except OSError as exc:
        print(f"error: cannot read {config_path}: {exc}", file=sys.stderr)
        return EXIT_INVALID

    database_path = run_dir / DATABASE_RELATIVE_PATH
    try:
        summary = consume_run(run_dir, database_path, config)
    except ConsumptionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_IO
    except (DatabaseError, StorageError, sqlite3.Error, OSError) as exc:
        print(f"error: cannot consume {run_dir}: {exc}", file=sys.stderr)
        return EXIT_IO

    print(
        f"Consumed {summary.run_id}: device={summary.device_id}, "
        f"shards_processed={summary.shards_processed}, "
        f"shards_skipped={summary.shards_skipped}, "
        f"windows_written={summary.windows_written}, "
        f"windows_dropped={summary.windows_dropped}, "
        f"alarms_written={summary.alarms_written}"
    )
    print(f"database: {database_path}")

    # A T76/T78: when this pass processed new shards and still dropped
    # samples, the operator must see it on stderr -- a zero-exit success
    # with silent drops is misleading.  An idempotent re-consume that
    # processes nothing and drops nothing must NOT trigger the warning.
    if summary.shards_processed > 0 and summary.windows_dropped > 0:
        print(
            f"warning: {summary.windows_dropped} sample(s) dropped "
            "(incomplete final window under incomplete_window=drop)",
            file=sys.stderr,
        )

    return EXIT_OK


def _run_benchmark(config_path: Path) -> int:
    """Publish one benchmark run and report the separated cost figures.

    ``docs/acceptance.md`` section 2 keeps the four numbers apart, and this
    command is the CLI half of that contract:

    * the producer's own ``perf_counter`` window (figure 1) comes from
      :class:`~g15.pipeline.producer.ProductionSummary`;
    * the independent post-production verification (figure 3) comes from
      :func:`~g15.reporting.verifier.verify_run`, which times itself and never
      overlaps the production window;
    * the CLI total wall clock (figure 2) and the peak RSS (figure 4) are
      measured *outside* this process -- a process cannot observe its own
      interpreter startup, so figure 2 is printed as ``unmeasured`` instead of
      being replaced by a smaller in-process number.

    Args:
        config_path: Benchmark scenario YAML.

    Returns:
        ``0`` on success, ``2`` for an invalid configuration, ``3`` for an I/O
        or schema failure, ``4`` when the independent verification ran and did
        not pass, ``5`` for an unexpected error.
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except OSError as exc:
        print(f"error: cannot read {config_path}: {exc}", file=sys.stderr)
        return EXIT_INVALID

    try:
        summary = produce_run_detailed(config, BENCHMARK_OUTPUT_ROOT)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except (DataContractError, OSError) as exc:
        print(f"error: cannot publish the benchmark run: {exc}", file=sys.stderr)
        return EXIT_IO

    try:
        verification = verify_run(summary.run_dir)
    except VerificationError as exc:
        print(f"error: cannot verify {summary.run_dir}: {exc}", file=sys.stderr)
        return EXIT_IO

    print(
        f"Benchmarked {verification.run_id}: {summary.part_count} parts, "
        f"{summary.total_points} points, status={verification.status}, "
        f"verification={'passed' if verification.passed else 'failed'}"
    )
    print(f"production_window_seconds={summary.elapsed_seconds:.4f}")
    print(f"verification_seconds={verification.elapsed_seconds:.4f}")
    print("cli_wallclock_seconds=unmeasured")
    print(f"parts={summary.part_count}")
    print(f"total_points={summary.total_points}")
    print(f"scalar_value_count={summary.scalar_value_count}")
    print(f"shard_shape={verification.shard_shape}")
    print(f"status={verification.status}")
    print(f"verification_passed={'true' if verification.passed else 'false'}")
    print(f"run_id={verification.run_id}")
    print(f"run_dir={summary.run_dir}")

    if not verification.passed:
        # The gate ran and failed: exit 4, never 0 and never 5.  A corrupt run
        # must not be reported as a successful benchmark measurement.
        print(
            "error: independent verification of the produced run did not pass: "
            + ", ".join(verification.failures),
            file=sys.stderr,
        )
        return EXIT_ACCEPTANCE

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Argument vector without the program name; ``None`` uses
            :data:`sys.argv`.

    Returns:
        The process exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            return _run_validate(Path(args.config))
        if args.command == "consume":
            return _run_consume(Path(args.run_dir), Path(args.config))
        if args.command == "benchmark":
            return _run_benchmark(Path(args.config))
    except Exception as exc:  # pylint: disable=broad-except
        # Section 10 maps an unexpected failure to 5; a traceback would only
        # help the developer, not whoever ran the command.
        print(f"error: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_UNEXPECTED

    print(
        f"error: subcommand {args.command!r} is not implemented yet; "
        "'validate', 'consume' and 'benchmark' are available so far",
        file=sys.stderr,
    )
    return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
