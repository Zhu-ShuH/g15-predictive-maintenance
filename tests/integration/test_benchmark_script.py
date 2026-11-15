"""Exercise the actual three-round shell orchestration with controlled processes."""

from __future__ import annotations

import csv
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _bash() -> str:
    candidates = []
    git = shutil.which("git")
    if os.name == "nt" and git:
        candidates.append(Path(git).resolve().parent.parent / "bin/bash.exe")
    discovered = shutil.which("bash")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    pytest.skip("Bash is required for the real shell orchestration test")


def _script(path: Path, body: str) -> None:
    path.write_text(
        "#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8", newline="\n"
    )
    path.chmod(0o755)


@pytest.mark.parametrize(
    "codes,tee_code,expected",
    [("0,0,0", 0, 0), ("4,0,0", 0, 3), ("4,4,4", 0, 3), ("0,0,0", 9, 3)],
)
def test_benchmark_runs_all_three_rounds_and_preserves_each_exit(
    tmp_path: Path, codes: str, tee_code: int, expected: int
) -> None:
    repo = tmp_path / "repository"
    (repo / "scripts").mkdir(parents=True)
    (repo / "configs").mkdir()
    (repo / "configs/benchmark_1m.yaml").write_text("fixture only\n", encoding="utf-8")
    benchmark = repo / "scripts/benchmark.sh"
    benchmark.write_bytes(
        (ROOT / "scripts/benchmark.sh").read_bytes().replace(b"\r\n", b"\n")
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    conda = fake_bin / "conda"
    _script(
        conda,
        r"""
if [[ "$*" == *"platform.python_version"* ]]; then
  echo 3.12.16
  exit 0
fi
expected='run --no-capture-output -n pp03 python -m g15 benchmark --config configs/benchmark_1m.yaml --max-production-seconds 60'
[ "$*" = "$expected" ] || { echo "wrong invocation: $*" >&2; exit 23; }
number=0
[ ! -f stub-count ] || number="$(cat stub-count)"
number=$((number+1))
printf '%s\n' "$number" > stub-count
printf '%s\n' "$*" >> stub-calls
echo "round=$number stdout"
echo "round=$number stderr" >&2
IFS=',' read -r -a codes <<< "$STUB_CODES"
exit "${codes[$((number-1))]}"
""",
    )
    if tee_code:
        _script(
            fake_bin / "tee",
            r"""
cat > "$1"
cat "$1"
exit "$STUB_TEE_EXIT"
""",
        )
    # Git Bash/Cygwin accepts native Windows argv but its PATH needs POSIX paths.
    launcher = r"""
test_bin="$1"; test_conda="$2"; test_script="$3"
if command -v cygpath >/dev/null 2>&1; then
  test_bin="$(cygpath -u "$test_bin")"
  test_conda="$(cygpath -u "$test_conda")"
  test_script="$(cygpath -u "$test_script")"
fi
export PATH="$test_bin:$PATH" CONDA_EXE="$test_conda"
exec bash "$test_script"
"""
    result = subprocess.run(
        [
            _bash(),
            "-c",
            launcher,
            "benchmark-test",
            str(fake_bin),
            str(conda),
            str(benchmark),
        ],
        cwd=repo,
        env=dict(os.environ, STUB_CODES=codes, STUB_TEE_EXIT=str(tee_code)),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert (repo / "stub-count").read_text(encoding="utf-8").strip() == "3"
    assert len((repo / "stub-calls").read_text(encoding="utf-8").splitlines()) == 3
    batches = list((repo / "runs/evidence").glob("benchmark_*"))
    assert len(batches) == 1
    with (batches[0] / "steps.tsv").open(encoding="utf-8", newline="") as stream:
        steps = list(csv.DictReader(stream, delimiter="\t"))
    assert [row["command_exit"] for row in steps] == codes.split(",")
    assert [int(row["tee_exit"]) for row in steps] == [tee_code] * 3
    for number in range(1, 4):
        log = (batches[0] / f"run{number}.log").read_text(encoding="utf-8")
        assert f"round={number} stdout" in log
        assert f"round={number} stderr" in log
    assert all("--max-production-seconds 60" in row["command"] for row in steps)
    if expected:
        assert "3 次均严格小于60秒" not in result.stdout
