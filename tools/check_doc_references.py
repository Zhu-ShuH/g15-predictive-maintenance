"""Read-only, local Markdown file-reference checks (no third-party dependencies).

Supports single-line inline links/images and reference definitions, with angle
destinations or one nested pair of parentheses. Skips code, external schemes,
and anchors; does not check headings, HTML, multiline links, or remote sites.
Three explicitly registered project dependencies are checked in inline code,
HTML comments and task-card code fences too; these required paths are not
examples. HTML comments are not parsed or masked. Unreadable files produce
diagnostics without discarding other findings; any read error still exits 2.
Generated pytest/cache directories and nested Git checkouts are excluded.
Exit codes: 0 clean, 1 missing/escaping target, 2 scan error.
"""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

DEPENDENCIES = ("docs/acceptance.md", "docs/environment.md", "environment.yml")
EXPLICIT = re.compile(
    r"(?<![\w./\\-])(?:" + "|".join(map(re.escape, DEPENDENCIES)) + r")(?![\w./\\-])"
)
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "runs",
    "__pycache__",
    ".pytest_cache",
    ".black-cache",
}
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
DEST = r"(<[^>\n]+>|(?:\\.|[^\s()\\]|\((?:\\.|[^()\\])*\))+?)"
INLINE = re.compile(r"!?\[(?:\\.|[^\]\\])*\]\(\s*" + DEST + r"(?:\s+[^\n]*?)?\s*\)")
DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s*" + DEST + r"(?:\s|$)")
INLINE_CODE = re.compile(r"(`+)(?!`)(.*?)(?<!`)\1(?!`)")


@dataclass(frozen=True)
class Finding:
    """A missing or escaping destination with an actionable source location."""

    file: str
    line: int
    target: str
    kind: str


def local_target(raw: str) -> str | None:
    """Decode a Markdown destination, excluding remote URLs and pure anchors."""
    value = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
    value = re.sub(r"\\([\s!\"#$%&'()*+,\-./:;<=>?@\[\]^_`{|}~\\])", r"\1", value)
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    return unquote(parsed.path)


def findings_for_file(root: Path, path: Path) -> list[Finding]:
    """Scan one UTF-8 Markdown file; propagate I/O and decoding failures."""
    findings = []
    fence_char = ""
    fence_length = 0
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        targets = {
            (match.group(), "required reference") for match in EXPLICIT.finditer(line)
        }
        fence = FENCE.match(line)
        if fence:
            marks, rest = fence.groups()
            if not fence_char:
                fence_char, fence_length = marks[0], len(marks)
            elif (
                marks[0] == fence_char
                and len(marks) >= fence_length
                and not rest.strip()
            ):
                fence_char = ""
        elif not fence_char:
            visible = INLINE_CODE.sub(lambda m: " " * len(m.group()), line)
            for match in INLINE.finditer(visible):
                target = local_target(match.group(1))
                if target is not None:
                    targets.add((target, "link"))
            definition = DEFINITION.match(visible)
            if definition:
                target = local_target(definition.group(1))
                if target is not None:
                    targets.add((target, "link"))
        reported = set()
        for target, kind in sorted(targets):
            parent = (
                root
                if kind == "required reference" or target.startswith("/")
                else path.parent
            )
            destination = (parent / target.lstrip("/")).resolve()
            try:
                destination.relative_to(root)
                escapes = False
            except ValueError:
                escapes = True
            if (escapes or not destination.exists()) and target not in reported:
                findings.append(
                    Finding(
                        path.relative_to(root).as_posix(), line_number, target, kind
                    )
                )
                reported.add(target)
    return findings


def scan(root: Path) -> list[Finding]:
    """Scan Markdown below root without following directory or file symlinks."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    findings = []

    def walk_error(error: OSError) -> None:
        path = Path(error.filename) if error.filename else root
        findings.append(
            Finding(path.relative_to(root).as_posix(), 1, str(error), "scan error")
        )

    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in SKIP_DIRS
            and not d.startswith("pytest-of-")
            and not d.endswith(".egg-info")
            and not (Path(current) / d).is_symlink()
            and not (Path(current) / d / ".git").exists()
        )
        for name in sorted(files):
            path = Path(current) / name
            if path.suffix.lower() == ".md" and not path.is_symlink():
                try:
                    findings.extend(findings_for_file(root, path))
                except (OSError, UnicodeError, ValueError) as error:
                    findings.append(
                        Finding(
                            path.relative_to(root).as_posix(),
                            1,
                            str(error),
                            "scan error",
                        )
                    )
    return findings


def annotation_escape(value: str, *, property_value: bool = False) -> str:
    """Escape GitHub workflow commands, including untrusted paths and targets."""
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        value = value.replace(":", "%3A").replace(",", "%2C")
    return value


def main(argv: list[str] | None = None) -> int:
    """Print diagnostics; CI may explicitly treat this checker as advisory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--github-actions", action="store_true")
    args = parser.parse_args(argv)
    try:
        findings = scan(args.root)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Document reference scan failed: {error}", file=sys.stderr)
        return 2
    for finding in findings:
        if finding.kind == "scan error":
            message = f"Document reference scan failed for this file: {finding.target}"
            print(f"{finding.file}:{finding.line}: {message}", file=sys.stderr)
        else:
            message = f"Missing or escaping {finding.kind}: {finding.target}"
        if args.github_actions:
            source = args.root.resolve() / finding.file
            annotation_file = os.path.relpath(source, Path.cwd()).replace(os.sep, "/")
            print(
                f"::warning file={annotation_escape(annotation_file, property_value=True)},"
                f"line={finding.line}::{annotation_escape(message)}"
            )
        else:
            print(f"{finding.file}:{finding.line}: {message}")
    print(f"Document reference scan: {len(findings)} issue(s).")
    if any(finding.kind == "scan error" for finding in findings):
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
