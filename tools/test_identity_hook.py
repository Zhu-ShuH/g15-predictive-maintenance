"""Exercise the real pre-push entry point against an isolated local bare remote."""

import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
env = os.environ.copy()
for key in list(env):
    if key.startswith("GIT_"):
        env.pop(key)
env.update(
    GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0"
)
results = []


def run(repo, *args, ok=True, extra=None):
    e = env.copy()
    e.update(extra or {})
    p = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=e,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if ok and p.returncode:
        raise RuntimeError(f"{args}: {p.stderr}")
    return p


def expect(repo, name, args, allowed, extra=None, reason=None):
    p = run(repo, *args, ok=False, extra=extra)
    assert (p.returncode == 0) == allowed, (name, p.returncode, p.stderr)
    if reason:
        assert reason in p.stderr, (name, p.stderr)
    results.append(
        {
            "case": name,
            "passed": True,
            "allowed": allowed,
            "exit_code": p.returncode,
            "output": p.stderr,
        }
    )


with tempfile.TemporaryDirectory(prefix="g15-hook-") as temp:
    base = Path(temp)
    bare, repo = base / "origin.git", base / "work"
    run(base, "init", "--bare", str(bare))
    run(base, "init", "-b", "main", str(repo))
    run(repo, "config", "user.name", "Main maintainer")
    run(repo, "config", "user.email", "main@example.test")
    run(repo, "commit", "--allow-empty", "-m", "main baseline")
    baseline = run(repo, "rev-parse", "HEAD").stdout.strip()
    run(repo, "remote", "add", "origin", str(bare))
    run(repo, "push", "origin", "main")
    run(repo, "fetch", "origin")
    run(repo, "config", "user.name", "C4")
    run(repo, "config", "user.email", "c4@example.test")
    run(repo, "config", "user.useConfigOnly", "true")
    hooks = repo / ".githooks"
    hooks.mkdir()
    run(repo, "config", "core.hooksPath", ".githooks")
    (hooks / "pre-push").write_bytes((ROOT / ".githooks" / "pre-push").read_bytes())
    (hooks / "pre-push").chmod(0o755)
    run(repo, "switch", "-c", "feature")
    run(repo, "commit", "--allow-empty", "-m", "C4 work")
    expect(
        repo,
        "first push excludes other authors already in main",
        ["push", "origin", "feature"],
        True,
    )
    run(repo, "config", "--unset", "user.email")
    expect(
        repo,
        "missing local identity",
        ["push", "origin", "feature:missing"],
        False,
        reason="Set local user.email",
    )
    run(repo, "config", "user.email", "c4@example.test")
    run(repo, "config", "--unset", "user.useConfigOnly")
    expect(
        repo,
        "missing local useConfigOnly",
        ["push", "origin", "feature:missing-only"],
        False,
        reason="Set local user.useConfigOnly=true",
    )
    run(repo, "config", "user.useConfigOnly", "false")
    expect(
        repo,
        "false local useConfigOnly",
        ["push", "origin", "feature:false-only"],
        False,
        reason="Set local user.useConfigOnly=true",
    )
    run(repo, "config", "user.useConfigOnly", "true")
    expect(
        repo,
        "effective environment override",
        ["push", "origin", "feature:override"],
        False,
        extra={"GIT_AUTHOR_EMAIL": "b2@example.test"},
        reason="Effective author",
    )
    run(
        repo,
        "commit",
        "--allow-empty",
        "-m",
        "wrong author",
        "--author=B2 <b2@example.test>",
    )
    expect(
        repo,
        "wrong new author",
        ["push", "origin", "feature"],
        False,
        reason="Stop and ask A",
    )
    run(
        repo,
        "commit",
        "--amend",
        "--allow-empty",
        "--no-edit",
        "--author=C4 <c4@example.test>",
    )
    expect(repo, "corrected author", ["push", "origin", "feature"], True)
    run(
        repo,
        "commit",
        "--allow-empty",
        "-m",
        "legitimate A contribution",
        "--author=A <a@example.test>",
    )
    a_sha = run(repo, "rev-parse", "HEAD").stdout.strip()
    expect(repo, "unreviewed other author", ["push", "origin", "feature"], False)
    run(repo, "config", "--add", "g15.identityApprovedCommit", a_sha)
    expect(
        repo,
        "reviewed exact A SHA",
        ["push", "origin", "feature"],
        True,
        reason="Reviewed exception",
    )
    old = run(repo, "rev-parse", "HEAD").stdout.strip()
    run(
        repo,
        "commit",
        "--amend",
        "--allow-empty",
        "--no-edit",
        "--author=C4 <c4@example.test>",
    )
    expect(
        repo,
        "explicit lease history correction",
        ["push", f"--force-with-lease=refs/heads/feature:{old}", "origin", "feature"],
        True,
    )
    # Peer advances main; new main ancestry must not be mistaken for C4 authorship.
    peer = base / "peer"
    run(base, "clone", "--branch", "main", str(bare), str(peer))
    run(peer, "config", "user.name", "Other maintainer")
    run(peer, "config", "user.email", "other@example.test")
    run(peer, "commit", "--allow-empty", "-m", "main advanced")
    run(peer, "push", "origin", "main")
    run(repo, "fetch", "origin")
    run(repo, "merge", "--no-edit", "origin/main")
    expect(repo, "sync main preserves others", ["push", "origin", "feature"], True)
    run(repo, "update-ref", "-d", "refs/remotes/origin/main")
    expect(
        repo,
        "missing baseline fails closed",
        ["push", "origin", "feature:no-base"],
        False,
        reason="Fetch origin/main",
    )
    run(repo, "fetch", "origin")
    # A remote tip unknown locally must fail even though the remote has it.
    run(peer, "switch", "-c", "unknown", "origin/main")
    run(peer, "commit", "--allow-empty", "-m", "unknown remote object")
    run(peer, "push", "origin", "unknown")
    expect(
        repo,
        "unknown remote object fails closed",
        ["push", "--force", "origin", "feature:unknown"],
        False,
        reason="Missing remote tip",
    )
    expect(
        repo,
        "multiple refs checks all",
        ["push", "origin", "feature:multi-good", "feature:unknown"],
        False,
        reason="Missing remote tip",
    )
    expect(repo, "branch deletion", ["push", "origin", ":unknown"], True)
    # Valid commit object with an unavailable parent: cat-file succeeds, rev-list must fail.
    tree = run(repo, "rev-parse", baseline + "^{tree}").stdout.strip()
    raw_commit = (
        f"tree {tree}\nparent {'2' * 40}\n"
        "author C4 <c4@example.test> 1700000000 +0000\n"
        "committer C4 <c4@example.test> 1700000000 +0000\n\nmissing parent\n"
    )
    obj = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=repo,
        env=env,
        input=raw_commit.encode(),
        capture_output=True,
        check=True,
    )
    missing_parent_sha = obj.stdout.decode().strip()
    sh = (
        Path(shutil.which("git")).parent.parent / "bin" / "sh.exe"
        if os.name == "nt"
        else shutil.which("sh")
    )
    if sh is None:
        raise RuntimeError("A POSIX shell is required")
    p = subprocess.run(
        [str(sh), str(hooks / "pre-push"), "origin", str(bare)],
        cwd=repo,
        env=env,
        capture_output=True,
        input=f"refs/heads/broken {missing_parent_sha} refs/heads/broken {'0' * 40}\n".encode(),
    )
    error = p.stderr.decode(errors="replace")
    assert p.returncode != 0 and "Cannot calculate new-branch range" in error, error
    results.append(
        {
            "case": "rev-list traversal failure refuses push",
            "passed": True,
            "allowed": False,
            "exit_code": p.returncode,
            "output": error,
        }
    )

report = {"passed": len(results), "failed": 0, "cases": results}
print(
    json.dumps(
        {"passed": len(results), "failed": 0, "cases": [x["case"] for x in results]},
        indent=2,
    )
)
