"""Scan repository files for accidental credentials without printing matched values."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_SECRET_PATTERNS = (
    ("github-token", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    (
        "authorization-bearer",
        re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    ),
    (
        "credential-assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)"
            r"\s*[:=]\s*['\"]?([^\s'\"<>]{12,})"
        ),
    ),
)
_SAFE_ENV_REFERENCE = re.compile(
    r"(?i)^\s*(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)"
    r"\s*[:=]\s*['\"]?os\.environ/[A-Z_][A-Z0-9_]*['\"]?\s*$"
)
_SENSITIVE_BASENAMES = {".netrc", ".npmrc", "id_rsa", "id_ed25519"}
_EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "_working_docs",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "target",
}
_MAX_SCAN_BYTES = 10_000_000


@dataclass(frozen=True, slots=True)
class ScanIssue:
    """A secret-scan finding that deliberately contains no matched secret value."""

    path: str
    line: int
    rule: str


def scan_text(text: str, *, path: str = "<memory>") -> list[ScanIssue]:
    """Return line-level credential findings without exposing the match itself."""

    issues: list[ScanIssue] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in _SECRET_PATTERNS:
            if not pattern.search(line):
                continue
            if rule == "credential-assignment" and _SAFE_ENV_REFERENCE.fullmatch(line):
                continue
            issues.append(ScanIssue(path, line_number, rule))
    return issues


def _sensitive_path(path: Path) -> bool:
    name = path.name
    if name in _SENSITIVE_BASENAMES or path.suffix.lower() in {".pem", ".key"}:
        return True
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def scan_paths(root: Path, relative_paths: Iterable[Path]) -> list[ScanIssue]:
    """Scan supplied repository-relative paths and reject sensitive credential files."""

    issues: list[ScanIssue] = []
    for relative_path in relative_paths:
        path = root / relative_path
        if _sensitive_path(path):
            issues.append(ScanIssue(relative_path.as_posix(), 0, "sensitive-file-path"))
            continue
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_SCAN_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            issues.append(ScanIssue(relative_path.as_posix(), 0, "unreadable-file"))
            continue
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        issues.extend(scan_text(text, path=relative_path.as_posix()))
    return issues


def scan_repository(root: Path) -> list[ScanIssue]:
    """Scan the repository tree, excluding Git internals and private working files."""

    paths: list[Path] = []
    for path in root.rglob("*"):
        if any(part in _EXCLUDED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            paths.append(path.relative_to(root))
    return scan_paths(root, paths)


def main() -> int:
    """Print sanitized findings and return nonzero when a possible secret is found."""

    repository_root = Path(__file__).resolve().parents[1]
    issues = scan_repository(repository_root)
    if issues:
        for issue in issues:
            print(f"{issue.path}:{issue.line}: possible credential ({issue.rule})", file=sys.stderr)
        return 1
    print("Secret scan passed: no credential patterns or sensitive credential files found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
