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


def find_posix_shell(git=None, which=None, is_file=None, is_windows=None):
    """Locate a POSIX shell for the hook entry point.

    The preferred location is the Git for Windows layout
    (<git>/../../bin/sh.exe). A sh executable on PATH is accepted as a
    fallback so the tool also runs on Linux, in CI and in Git Bash style
    environments. Path strings are never trusted blindly: a candidate is used
    only after an explicit file-existence check.

    Args:
        git: Injected git executable path, defaults to shutil.which.
        which: Injected which lookup, defaults to shutil.which.
        is_file: Injected existence predicate, defaults to Path.is_file.
        is_windows: Injected platform flag, defaults to os.name == "nt".

    Returns:
        A (path, source) pair, where source is "git-for-windows" or "path".

    Raises:
        RuntimeError: when no POSIX shell is found anywhere.
    """
    which = shutil.which if which is None else which
    if is_file is None:
        is_file = lambda candidate: Path(candidate).is_file()
    if is_windows is None:
        is_windows = os.name == "nt"
    git = which("git") if git is None else git
    if is_windows and git:
        candidate = Path(git).parent.parent / "bin" / "sh.exe"
        if is_file(candidate):
            return str(candidate), "git-for-windows"
    found = which("sh")
    if found:
        return found, "path"
    raise RuntimeError(
        "A POSIX shell is required: no sh.exe in the Git for Windows layout "
        "and no sh on PATH"
    )


def check_shell_resolution():
    """Cover the preferred layout, the PATH fallback and the total-miss case.

    The platform flag is injected so all three branches run identically on
    Windows and on Linux CI instead of only exercising the host's own layout.
    """
    present = lambda candidate: True
    absent = lambda candidate: False
    git_path = "C:\\Program Files\\Git\\cmd\\git.exe"

    path, source = find_posix_shell(
        git=git_path, which=lambda name: None, is_file=present, is_windows=True
    )
    assert source == "git-for-windows", source
    assert path.replace("\\", "/").endswith("Git/bin/sh.exe"), path
    results.append(
        {
            "case": "shell from the Git for Windows layout",
            "passed": True,
            "allowed": True,
            "exit_code": 0,
            "output": path,
        }
    )

    def which_without_sh(name):
        return git_path if name == "git" else None

    sh_on_path = "/usr/bin/sh"
    path, source = find_posix_shell(
        git=git_path,
        which=lambda name: sh_on_path if name == "sh" else None,
        is_file=absent,
        is_windows=True,
    )
    assert source == "path" and path == sh_on_path, (path, source)
    results.append(
        {
            "case": "Windows layout missing falls back to sh on PATH",
            "passed": True,
            "allowed": True,
            "exit_code": 0,
            "output": path,
        }
    )

    path, source = find_posix_shell(
        git=None,
        which=lambda name: sh_on_path if name == "sh" else None,
        is_file=absent,
        is_windows=False,
    )
    assert source == "path" and path == sh_on_path, (path, source)
    results.append(
        {
            "case": "POSIX host resolves sh on PATH",
            "passed": True,
            "allowed": True,
            "exit_code": 0,
            "output": path,
        }
    )

    try:
        find_posix_shell(
            git=git_path, which=which_without_sh, is_file=absent, is_windows=True
        )
    except RuntimeError as exc:
        assert "POSIX shell is required" in str(exc), exc
    else:
        raise AssertionError("a missing POSIX shell must fail loudly")
    results.append(
        {
            "case": "missing POSIX shell fails loudly",
            "passed": True,
            "allowed": False,
            "exit_code": 1,
            "output": "RuntimeError: A POSIX shell is required",
        }
    )


check_shell_resolution()


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
    sh, shell_source = find_posix_shell()
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

print(
    json.dumps(
        {
            "passed": len(results),
            "failed": 0,
            "shell": str(sh),
            "shell_source": shell_source,
            "cases": [x["case"] for x in results],
        },
        indent=2,
    )
)
