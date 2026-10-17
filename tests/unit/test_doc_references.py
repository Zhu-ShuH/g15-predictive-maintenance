"""Verify local-reference diagnostics independently of the project's documents."""

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "check_doc_references.py"
SPEC = importlib.util.spec_from_file_location("g15_doc_reference_tool", SCRIPT)
CHECKER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECKER
SPEC.loader.exec_module(CHECKER)


def write(root, name, text=""):
    """Create an independent fixture, including any parent directories."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_existing_relative_image_definition_and_root_links(tmp_path):
    write(tmp_path, "中文 空格.md")
    write(tmp_path, "plot(1).png")
    write(
        tmp_path,
        "docs/guide.md",
        '[x](<../中文 空格.md>)\n![plot](../plot(1).png)\n[id]: /中文%20空格.md "title"\n[x](../中文%20空格.md?view=1#section)\n',
    )
    assert CHECKER.scan(tmp_path) == []


def test_bad_links_report_source_line_and_target(tmp_path):
    write(tmp_path, "docs/a.md", "heading\n[x](missing.md)\n[id]: /gone.png\n")
    assert [(x.file, x.line, x.target) for x in CHECKER.scan(tmp_path)] == [
        ("docs/a.md", 2, "missing.md"),
        ("docs/a.md", 3, "/gone.png"),
    ]


def test_external_anchors_and_code_are_ignored(tmp_path):
    write(
        tmp_path,
        "a.md",
        "[x](https://example.invalid/x)\n[x](mailto:a@example.invalid)\n[x](#heading)\n[x](//example.invalid/x)\n`[example](fake.md)`\n````python\n[x](fake.md)\n```\n[x](also-fake.md)\n````\n~~~\n[x](fake.md)\n~~~\nordinary `src/future.py`\n",
    )
    assert CHECKER.scan(tmp_path) == []


def test_registered_dependencies_inside_code_fence(tmp_path):
    write(
        tmp_path,
        "docs/task.md",
        "```text\nRead docs/acceptance.md and environment.yml\n```\n",
    )
    found = CHECKER.scan(tmp_path)
    assert {(x.line, x.target, x.kind) for x in found} == {
        (2, "docs/acceptance.md", "required reference"),
        (2, "environment.yml", "required reference"),
    }
    write(tmp_path, "docs/acceptance.md")
    write(tmp_path, "environment.yml")
    assert CHECKER.scan(tmp_path) == []


def test_required_reference_matches_exact_project_paths(tmp_path):
    write(tmp_path, "a.md", "archive/docs/acceptance.md environment.yml.example\n")
    assert CHECKER.scan(tmp_path) == []


def test_escape_from_repository_is_reported_even_when_file_exists(tmp_path):
    root = tmp_path / "repo"
    write(tmp_path, "outside.md")
    write(root, "a.md", "[x](../outside.md)\n")
    assert [(x.line, x.target) for x in CHECKER.scan(root)] == [(1, "../outside.md")]


def test_ignored_directories_are_not_scanned(tmp_path):
    write(tmp_path, ".git/a.md", "[x](missing.md)")
    write(tmp_path, "runs/a.md", "[x](missing.md)")
    assert CHECKER.scan(tmp_path) == []


def test_link_target_cannot_escape_through_symlink(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = write(tmp_path, "outside.md")
    try:
        (root / "alias.md").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation unavailable on this OS/account")
    write(root, "a.md", "[x](alias.md)")
    assert [x.target for x in CHECKER.scan(root)] == ["alias.md"]


def test_cli_exit_codes_and_line_diagnostics(tmp_path):
    write(tmp_path, "a.md", "heading\n[x](missing.md)")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "a.md:2:" in proc.stdout and "missing.md" in proc.stdout
    write(tmp_path, "missing.md")
    assert CHECKER.main(["--root", str(tmp_path)]) == 0
    assert CHECKER.main(["--root", str(tmp_path / "absent")]) == 2


def test_invalid_utf8_is_scan_error(tmp_path, capsys):
    (tmp_path / "bad.md").write_bytes(b"\xff")
    assert CHECKER.main(["--root", str(tmp_path)]) == 2
    assert "scan failed" in capsys.readouterr().err


def test_github_warning_and_escaping(tmp_path, capsys):
    write(tmp_path, "a,b.md", "[x](bad%25name.md)")
    assert CHECKER.main(["--root", str(tmp_path), "--github-actions"]) == 1
    output = capsys.readouterr().out
    assert "::warning file=a%2Cb.md,line=1::" in output
    assert "bad%25name.md" in output
    assert CHECKER.annotation_escape("x\n::error::oops%") == "x%0A::error::oops%25"
