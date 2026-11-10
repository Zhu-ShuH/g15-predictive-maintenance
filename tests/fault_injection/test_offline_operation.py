"""Offline runtime acceptance tests for Issue #144.

The guard lives outside ``src/`` and is activated only in child processes via
``PYTHONPATH`` plus ``G15_DENY_NETWORK=1``. Production imports and ordinary
CLI invocations therefore retain their normal behaviour.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "configs"
OFFLINE_GUARD = ROOT / "tools" / "offline_guard"
GUARD_ACTIVE_MARKER = "G15 offline guard active"

SMALL_SCENARIO = """\
schema_version: '1.0'
scenario:
  id: offline_acceptance_small
  description: 'small runtime-offline acceptance chain'
  kind: normal
  seed: 144
  start_time_utc: '2026-10-14T00:00:00Z'
  device_id: 'DEV-OFFLINE'
  sample_rate_hz: 100
  total_points: 1024
  chunk_points: 512
signals:
  vibration:
    baseline: 0.5
    degradation_rate_per_sample: 0.0
    noise_std: 0.05
    unit: 'g'
  temperature:
    baseline: 35.0
    degradation_rate_per_sample: 0.0
    noise_std: 0.15
    unit: 'degC'
fault_generation:
  mode: manual
  stochastic_probability_per_window: 0.0
  events: []
"""


def _guarded_environment() -> dict[str, str]:
    """Return an environment that activates the test-only startup guard."""
    environment = dict(os.environ)
    python_paths = [str(OFFLINE_GUARD), str(ROOT / "src")]
    existing = environment.get("PYTHONPATH")
    if existing:
        python_paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    environment["G15_DENY_NETWORK"] = "1"
    environment["G15_OFFLINE_GUARD_VERBOSE"] = "1"
    return environment


def _run_guarded(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one Python child and prove the startup guard was activated."""
    result = subprocess.run(
        [sys.executable, *arguments],
        cwd=cwd,
        env=_guarded_environment(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert GUARD_ACTIVE_MARKER in result.stderr, (
        "the child did not confirm that the test-only offline guard was active: "
        f"{result.stderr!r}"
    )
    return result


def _stdout_value(stdout: str, name: str) -> str:
    """Extract a ``name=value`` line from CLI output."""
    prefix = f"{name}="
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise AssertionError(f"{name!r} missing from stdout: {stdout!r}")


def test_offline_guard_rejects_dns_and_socket_creation(tmp_path: Path) -> None:
    """The guard self-check must fail closed before the pipeline is trusted."""
    probe = """
import socket
from sitecustomize import OfflineNetworkAccessError

blocked = []
for name, operation in (
    ("dns", lambda: socket.getaddrinfo("example.invalid", 443)),
    ("socket", lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM)),
):
    try:
        operation()
    except OfflineNetworkAccessError as exc:
        if "G15 offline guard blocked" not in str(exc):
            raise
        blocked.append(name)

if blocked != ["dns", "socket"]:
    raise SystemExit(f"guard did not reject both probes: {blocked!r}")
print("offline_guard_self_check=blocked:dns,socket")
"""

    result = _run_guarded("-c", probe, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "offline_guard_self_check=blocked:dns,socket" in result.stdout


def test_small_cli_chain_completes_with_python_network_denied(
    tmp_path: Path,
) -> None:
    """Run real validate -> simulate -> consume -> report under the guard."""
    scenario = tmp_path / "offline-small.yaml"
    scenario.write_text(SMALL_SCENARIO, encoding="utf-8")
    output_root = tmp_path / "runs"

    validated = _run_guarded(
        "-m", "g15", "validate", "--config", str(scenario), cwd=tmp_path
    )
    assert validated.returncode == 0, validated.stderr
    assert "Configuration OK" in validated.stdout

    simulated = _run_guarded(
        "-m",
        "g15",
        "simulate",
        "--config",
        str(scenario),
        "--output-root",
        str(output_root),
        cwd=tmp_path,
    )
    assert simulated.returncode == 0, simulated.stderr
    run_dir = Path(_stdout_value(simulated.stdout, "run_dir"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["total_points"] == 1024

    consumed = _run_guarded(
        "-m",
        "g15",
        "consume",
        "--run-dir",
        str(run_dir),
        "--config",
        str(CONFIGS / "consumer.yaml"),
        cwd=ROOT,
    )
    assert consumed.returncode == 0, consumed.stderr
    assert "windows_written=4" in consumed.stdout

    reported = _run_guarded("-m", "g15", "report", "--run-dir", str(run_dir), cwd=ROOT)
    assert reported.returncode == 0, reported.stderr

    summary_json = run_dir / "reports" / "summary.json"
    summary_markdown = run_dir / "reports" / "summary.md"
    database = run_dir / "artifacts" / "results.sqlite"
    assert summary_json.is_file()
    assert summary_markdown.is_file()
    assert database.is_file()
    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    assert summary["total_points"] == 1024
    assert summary["processed_points"] == 1024
