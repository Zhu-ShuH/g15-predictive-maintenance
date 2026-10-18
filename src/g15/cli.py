"""Command line entry point for the G15 sandbox.

Only ``validate`` is implemented in Issue #4; the other subcommands of
``docs/interface.md`` section 10 are registered with their frozen arguments so
that ``--help`` stays honest, and they exit non-zero with an explicit message
instead of pretending to succeed.

Exit codes (``docs/interface.md`` section 10):

* ``0`` success;
* ``2`` invalid arguments or invalid configuration;
* ``5`` unexpected error, used here for "subcommand not implemented yet".

The CLI performs argument parsing, command dispatch and exit-code mapping only;
it contains no simulation, scoring or alarm logic.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, load_config

#: Successful run.
EXIT_OK = 0
#: Invalid arguments or invalid configuration.
EXIT_INVALID = 2
#: Unexpected error; also used by the not-yet-implemented subcommands.
EXIT_UNEXPECTED = 5


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
        "consume", help="consume a run into SQLite (not implemented)"
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
        "benchmark", help="measure the 1M-point budget (not implemented)"
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

    if args.command == "validate":
        return _run_validate(Path(args.config))

    print(
        f"error: subcommand {args.command!r} is not implemented yet; "
        "only 'validate' is available in this issue",
        file=sys.stderr,
    )
    return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
