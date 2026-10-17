"""Thin CLI for config validation and small utilities.

Usage:
    python -m g15 --config configs/default.yaml

"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="g15")
    parser.add_argument("--config", "-c", required=True, help="Path to YAML configuration file")
    parser.add_argument("--show", action="store_true", help="Print the loaded configuration (JSON)")
    args = parser.parse_args(argv)

    path = Path(args.config)
    try:
        cfg = load_config(path)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.show:
        # Avoid depending on json of non-serializable objects
        import json

        print(json.dumps(cfg.raw, indent=2, ensure_ascii=False))
    else:
        print(f"Configuration OK: {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
