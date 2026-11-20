#!/usr/bin/env python3
"""Build a deterministic, secret-free zero2xLab skill archive."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable, Optional, Sequence
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ALLOWED_ROOTS = {
    "SKILL.md",
    "agents",
    "assets",
    "references",
    "requirements.txt",
    "scripts",
}
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", "tests", "output", "outputs"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".env"}
SECRET_ASSIGNMENT = re.compile(
    rb"(?:DEEPSEEK_API_KEY|DASHSCOPE_API_KEY)\s*=\s*['\"]?[A-Za-z0-9_-]{8,}"
)
DANGEROUS_PATHS = (
    b"F:" + b"\\work\\",
    b"/" + b"mnt/",
    ("申请_" + "王宵冰").encode("utf-8"),
)


def iter_packaged_files(root: Path) -> Iterable[Path]:
    root = Path(root).resolve()
    for entry in sorted(root.rglob("*"), key=lambda path: path.as_posix()):
        if not entry.is_file():
            continue
        relative = entry.relative_to(root)
        if relative.parts[0] not in ALLOWED_ROOTS:
            continue
        if any(part in EXCLUDED_PARTS or part.startswith(".") for part in relative.parts):
            continue
        if entry.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        yield entry


def _validate_payload(relative: Path, payload: bytes) -> None:
    if SECRET_ASSIGNMENT.search(payload):
        raise ValueError(f"Potential API key assignment found in {relative.as_posix()}")
    for marker in DANGEROUS_PATHS:
        if marker in payload:
            raise ValueError(
                f"Host-specific path marker found in {relative.as_posix()}: "
                f"{marker.decode('utf-8', errors='replace')}"
            )


def build_archive(root: Path, output: Path) -> Path:
    root = Path(root).resolve()
    output = Path(output).resolve()
    files = list(iter_packaged_files(root))
    if root / "SKILL.md" not in files:
        raise FileNotFoundError(f"SKILL.md is missing from skill root: {root}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(root)
            payload = path.read_bytes()
            _validate_payload(relative, payload)
            info = ZipInfo(relative.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
    return output


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Package the zero2xLab skill.")
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    archive = build_archive(args.root, args.output)
    print(archive)
    print(f"bytes={archive.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

