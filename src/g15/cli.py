"""Command line entry point for the G15 sandbox.

``validate`` has been implemented since Issue #4, ``simulate`` and ``report``
since Issue #110, ``consume`` since Issue #65, ``benchmark`` since Issue #51
and ``reset-alarms`` since Issue #127; the remaining subcommands of
``docs/interface.md`` section 10 (``verify`` and ``doctor``) are registered
with their frozen arguments so that ``--help`` stays honest, and they exit
non-zero with an explicit message instead of pretending to succeed.

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
from .pipeline.consumer import (
    AlarmResetResult,
    ConsumptionError,
    consume_run,
    reset_alarm_state,
)
from .pipeline.producer import produce_run, produce_run_detailed
from .pipeline.schema import DataContractError
from .reporting.builder import SUMMARY_MD, ReportError, build_report
from .reporting.verifier import VerificationError, verify_run
from .storage.database import DatabaseError
from .storage.repository import StateCompatibilityError, StorageError

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
        "simulate",
        help="generate a complete run for a scenario (Issue #110)",
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
        "report",
        help="build JSON and Markdown reports for a run (Issue #110)",
    )
    report.add_argument("--run-dir", required=True, help="path to runs/<run_id>")

    reset_alarms = subparsers.add_parser(
        "reset-alarms",
        help="explicitly reset one alarm streak (Issue #127)",
        description=(
            "Archive the stored alarm state for one (run, device, rule) and "
            "start a fresh streak. Only the target alarm_states row changes: "
            "processed_chunks, feature_windows, alarms and runs are left "
            "untouched. Historical alarms are not deleted and the run is not "
            "recomputed. The last_end_seq_exclusive anchor is kept, so the "
            "next window may raise an alarm again. --yes is the only "
            "confirmation; a failed consume never triggers a reset."
        ),
    )
    reset_alarms.add_argument("--run-dir", required=True, help="path to runs/<run_id>")
    reset_alarms.add_argument(
        "--device-id", required=True, help="device id declared by the manifest"
    )
    reset_alarms.add_argument(
        "--rule-id", required=True, help="rule whose stored state is reset"
    )
    reset_alarms.add_argument(
        "--reason", required=True, help="non-empty operator reason (free text)"
    )
    reset_alarms.add_argument(
        "--yes",
        action="store_true",
        help="confirm the reset non-interactively (required)",
    )

    verify = subparsers.add_parser(
        "verify",
        # Kept short: ``reset-alarms`` widened the subcommand column, and a
        # longer line would fold ``(not implemented)`` onto a second line.
        help="verify shards and manifest (not implemented)",
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


def _run_simulate(config_path: Path, output_root: Path) -> int:
    """Publish one complete simulated run.

    The CLI only loads the configuration and hands it to the frozen producer
    entry :func:`~g15.pipeline.producer.produce_run`; simulation, sharding and
    manifest publication stay in the producer (Issue #110).

    Args:
        config_path: Scenario YAML to load.
        output_root: Directory receiving the new ``<run_id>`` sub-directory.

    Returns:
        ``0`` on success, ``2`` for an invalid configuration, ``3`` for an I/O
        or schema failure.  Unexpected errors propagate to :func:`main`, which
        maps them to ``5``.
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
        run_dir = produce_run(config, output_root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except (DataContractError, OSError) as exc:
        print(f"error: cannot publish the simulated run: {exc}", file=sys.stderr)
        return EXIT_IO

    print(f"run_dir={run_dir}")
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


def _run_report(run_dir: Path) -> int:
    """Write the JSON and Markdown reports for one consumed run.

    Issue #110 freezes success as "the report artefacts were produced".
    :func:`~g15.reporting.builder.build_report` may legitimately emit a
    diagnostic report whose ``passed`` is ``False`` or ``None``; that is still
    a successful generation and returns ``0``.  Exit code ``4`` belongs to
    ``verify`` or another explicit acceptance entry, never to this command.

    Args:
        run_dir: Directory holding the manifest and ``artifacts/results.sqlite``.

    Returns:
        ``0`` when both artefacts were written, ``3`` when the run, manifest,
        database, truth or output could not be used.  Unexpected errors
        propagate to :func:`main`, which maps them to ``5``.
    """
    database_path = run_dir / DATABASE_RELATIVE_PATH
    try:
        summary_path = build_report(run_dir, database_path)
    except (ReportError, DataContractError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_IO
    except OSError as exc:
        print(f"error: cannot build the report for {run_dir}: {exc}", file=sys.stderr)
        return EXIT_IO

    print(f"report_json={summary_path}")
    print(f"report_markdown={summary_path.parent / SUMMARY_MD}")
    return EXIT_OK


def _print_reset_audit(result: AlarmResetResult, database_path: Path) -> None:
    """Print one reset's audit as stable key=value lines.

    The four untouched tables are named explicitly: section 8.5 requires the
    operator to see that a reset did not delete historical alarms or recompute
    the run.
    """
    print(
        f"reset run_id={result.run_id} device_id={result.device_id} "
        f"rule_id={result.rule_id}"
    )
    print(f"reason={result.reason}")
    print(f"reset_at_utc={result.reset_at_utc}")
    print(f"previous_state_schema_version={result.previous_state_schema_version}")
    print(
        "previous_state_decodable="
        f"{'true' if result.previous_state_decodable else 'false'}"
    )
    print(
        "previous_state_json_bytes="
        f"{len(result.previous_state_json.encode('utf-8'))}"
    )
    print(f"archived_reset_count={result.archived_reset_count}")
    print(f"audit_sequence={result.audit_sequence}")
    print(f"audit_previous_hash={result.audit_previous_hash or 'null'}")
    print(f"audit_event_hash={result.audit_event_hash}")
    print(
        "new_state_consecutive_breaches="
        f"{result.new_state.consecutive_breaches} "
        f"new_state_active_level={result.new_state.active_level} "
        f"new_state_alarm_windows={result.new_state.alarm_windows}"
    )
    print("tables_untouched=processed_chunks,feature_windows,alarms,runs")
    print("audit_table=audit_events append_only=true")
    print(f"database={database_path}")


def _run_reset_alarms(  # pylint: disable=R0911
    run_dir: Path,
    device_id: str,
    rule_id: str,
    reason: str,
    *,
    confirmed: bool,
) -> int:
    """Explicitly reset one alarm streak.

    The database path is derived from ``run_dir`` exactly as ``consume`` does;
    there is deliberately no ``--database`` option and no ``allow_legacy``
    option. Exit codes follow section 8.5: empty reason or a missing required
    argument is 2, data/I/O/state failures are 3, success is 0, and anything
    unanticipated is left to :func:`main` to map to 5.

    Args:
        run_dir: Directory holding the manifest and the database.
        device_id: Device whose streak is reset.
        rule_id: Rule whose streak is reset.
        reason: Operator's non-empty free text.
        confirmed: Whether ``--yes`` was supplied.

    Returns:
        ``0``, ``2`` or ``3``.
    """
    if not reason.strip():
        print("error: --reason must not be empty", file=sys.stderr)
        return EXIT_INVALID
    if not confirmed:
        print(
            "error: reset-alarms requires --yes; a reset is never implicit",
            file=sys.stderr,
        )
        return EXIT_INVALID

    database_path = run_dir / DATABASE_RELATIVE_PATH
    try:
        result = reset_alarm_state(
            run_dir, database_path, device_id, rule_id, reason=reason
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except (ConsumptionError, DatabaseError, sqlite3.Error, OSError) as exc:
        print(f"error: cannot reset alarm state: {exc}", file=sys.stderr)
        return EXIT_IO
    except StateCompatibilityError as exc:
        # A StorageError subclass: keep its specific diagnostic first, or the
        # message would degrade into a generic storage failure.
        print(f"error: incompatible stored state: {exc}", file=sys.stderr)
        return EXIT_IO
    except StorageError as exc:
        print(f"error: cannot reset alarm state: {exc}", file=sys.stderr)
        return EXIT_IO

    _print_reset_audit(result, database_path)
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
        if args.command == "simulate":
            return _run_simulate(Path(args.config), Path(args.output_root))
        if args.command == "consume":
            return _run_consume(Path(args.run_dir), Path(args.config))
        if args.command == "report":
            return _run_report(Path(args.run_dir))
        if args.command == "reset-alarms":
            return _run_reset_alarms(
                Path(args.run_dir),
                args.device_id,
                args.rule_id,
                args.reason,
                confirmed=args.yes,
            )
        if args.command == "benchmark":
            return _run_benchmark(Path(args.config))
    except Exception as exc:  # pylint: disable=broad-except
        # Section 10 maps an unexpected failure to 5; a traceback would only
        # help the developer, not whoever ran the command.
        print(f"error: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_UNEXPECTED

    print(
        f"error: subcommand {args.command!r} is not implemented yet; "
        "'validate', 'simulate', 'consume', 'report', 'reset-alarms' and "
        "'benchmark' are available so far",
        file=sys.stderr,
    )
    return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
