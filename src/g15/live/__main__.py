"""Run a local ten-device wall-clock simulator and its read-only console."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import platform
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading

from g15.console.server import create_console_server
from g15.live import LiveConfig, LiveRuntime
from g15.live.reporting import render_live_report
from g15.query.application import HealthQueryApplication
from g15.query.auth import AuthorizationPolicy, TokenVerifier, issue_token
from g15.query.cursor import CursorCodec
from g15.service.http import create_query_server


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _source_identity() -> dict:
    try:
        root = Path(__file__).resolve().parents[3]
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
        )
        return {"git_sha": revision, "git_dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"git_sha": None, "git_dirty": None}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--health-rules", type=Path, required=True)
    parser.add_argument("--devices", type=int, default=10)
    parser.add_argument("--sample-rate-hz", type=int, default=256)
    parser.add_argument("--window-points", type=int, default=256)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--queue-capacity", type=int, default=20)
    parser.add_argument("--duration-seconds", type=float, default=3600.0)
    parser.add_argument("--max-disk-mib", type=int, default=512)
    parser.add_argument("--seed", type=int, default=160)
    parser.add_argument("--console-port", type=int, default=18468)
    parser.add_argument("--query-port", type=int, default=18469)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--exit-after-stop", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Start one bounded capture session; retain readable state after it stops."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.console_port == args.query_port and args.console_port != 0:
        parser.error("console and query ports must be distinct")
    stopped = threading.Event()
    servers, threads = [], []
    runtime = None
    prior_handlers = {}
    result_code = 0

    def request_stop(_number, _frame):
        stopped.set()

    try:
        config = LiveConfig(
            devices=args.devices,
            sample_rate_hz=args.sample_rate_hz,
            window_points=args.window_points,
            interval_seconds=args.interval_seconds,
            queue_capacity=args.queue_capacity,
            max_duration_seconds=args.duration_seconds,
            max_disk_bytes=args.max_disk_mib * 1024 * 1024,
            seed=args.seed,
        )
        runtime = LiveRuntime(
            args.output_root, config=config, health_rules_path=args.health_rules
        )
        catalog = runtime.catalog()
        bearer_key, cursor_key = secrets.token_bytes(32), secrets.token_bytes(32)
        issuer, audience, subject = "g15-live", "g15-live-local-query", "local-operator"
        policy = AuthorizationPolicy.from_yaml(
            json.dumps(
                {
                    "authz_schema_version": "1",
                    "policy_version": runtime.session_id,
                    "subjects": {
                        subject: {
                            "actor_ref": "live-local-operator",
                            "permission_domain": runtime.session_id,
                            "allow": [
                                {"run_id": row["run_id"], "device_id": row["device_id"]}
                                for row in catalog
                            ],
                        }
                    },
                }
            )
        )
        application = HealthQueryApplication(
            runs_root=runtime.runs_root,
            token_verifier=TokenVerifier(
                key=bearer_key, issuer=issuer, audience=audience
            ),
            authorization_policy=policy,
            cursor_codec=CursorCodec(key=cursor_key),
            allow_live_runs=True,
        )
        query = create_query_server(application, host="127.0.0.1", port=args.query_port)
        servers.append(query)
        query_url = f"http://127.0.0.1:{query.server_address[1]}"

        def credential() -> str:
            return issue_token(
                key=bearer_key,
                issuer=issuer,
                audience=audience,
                subject=subject,
                ttl_seconds=600,
            )

        def live_report(pair: tuple[str, str], extension: str) -> bytes:
            return render_live_report(
                runtime.runs_root,
                *pair,
                extension,
                live_status=runtime.status_snapshot()["status"],
            )

        console = create_console_server(
            runtime.catalog_path,
            runtime.runs_root,
            query_url,
            None,
            port=args.console_port,
            live_status_provider=runtime.status_snapshot,
            credential_provider=credential,
            live_report_provider=live_report,
        )
        servers.append(console)
        for server in servers:
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        runtime.start()
        stop_file = args.stop_file or runtime.session_root / "stop.request"
        ready = {
            "schema_version": "1",
            "process_id": os.getpid(),
            "session_id": runtime.session_id,
            "session_root": str(runtime.session_root),
            "console_url": f"http://127.0.0.1:{console.server_address[1]}/",
            "query_url": query_url,
            "stop_file": str(stop_file),
            "started_at_utc": _utc_now(),
            "config": asdict(config),
            "source": _source_identity(),
            "environment": {
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(),
            },
        }
        _write_json(runtime.session_root / "service-ready.json", ready)
        _write_json(args.output_root / "active.json", ready)
        print(
            json.dumps(
                {
                    "console_url": ready["console_url"],
                    "devices": len(catalog),
                    "session_id": runtime.session_id,
                    "duration_limit_seconds": args.duration_seconds,
                }
            ),
            flush=True,
        )
        for number in (signal.SIGINT, signal.SIGTERM):
            prior_handlers[number] = signal.signal(number, request_stop)
        while not stopped.is_set():
            snapshot = runtime.status_snapshot()
            snapshot["observed_at_utc"] = _utc_now()
            _write_json(runtime.session_root / "service-status.json", snapshot)
            if stop_file.exists():
                stopped.set()
                break
            finished = runtime.wait(timeout=0.5)
            if finished and args.exit_after_stop:
                result_code = 1 if snapshot["status"] == "error" else 0
                break
            if finished:
                stopped.wait(0.5)
    except (OSError, ValueError, RuntimeError) as exc:
        logging.exception("local live service failed: %s", type(exc).__name__)
        result_code = 1
    finally:
        if runtime is not None:
            try:
                final = runtime.stop(timeout=30)
                final["observed_at_utc"] = _utc_now()
                _write_json(runtime.session_root / "service-status.json", final)
                if final["status"] in ("error", "stopping"):
                    result_code = 1
            except Exception:  # pylint: disable=broad-exception-caught
                logging.exception("live session failed to stop cleanly")
                result_code = 1
        for index, server in enumerate(servers):
            if index < len(threads):
                server.shutdown()
                threads[index].join(timeout=5)
                if threads[index].is_alive():
                    result_code = 1
            server.server_close()
        for number, handler in prior_handlers.items():
            signal.signal(number, handler)
    return result_code


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
