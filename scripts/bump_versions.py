#!/usr/bin/env python3
"""
bump_versions.py
----------------

Give every local image in README.md a content addressed cache buster.

GitHub proxies and caches images by URL, so a static URL keeps serving
the first version it ever saw. The previous fix was a timestamp, but a
timestamp changes on every single run, which produces an empty commit
every few minutes and inflates the contribution graph with commits that
changed nothing.

A content hash is strictly better: the URL only changes when the file
content changes, so the cache is invalidated exactly when it has to be
and stays stable otherwise.

Usage:
    python scripts/bump_versions.py [--check]

    --check   report which images are out of date and exit non zero
              instead of writing, for use in a verification workflow.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

README_PATH = Path("README.md")

HASH_LENGTH = 12

IMG_SRC = re.compile(
    r'(?P<head><img\b[^>]*?\ssrc=")'
    r'(?P<path>[^"?#]+)'
    r'(?P<query>\?[^"]*)?'
    r'(?P<tail>")',
    re.IGNORECASE,
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()[:HASH_LENGTH]


def collect_targets(content: str) -> list[tuple[str, str]]:
    """(path, expected version) for every local image in the README."""

    targets: list[tuple[str, str]] = []

    for match in IMG_SRC.finditer(content):
        path = match.group("path")

        if path.startswith(("http://", "https://", "//", "data:")):
            continue

        if not Path(path).is_file():
            continue

        targets.append((path, file_hash(Path(path))))

    return targets


def rewrite(content: str) -> tuple[str, list[str]]:
    """Return the new content and the list of images that were updated."""

    changed: list[str] = []
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        path = match.group("path")

        if path.startswith(("http://", "https://", "//", "data:")):
            return match.group(0)

        target = Path(path)

        if not target.is_file():
            return match.group(0)

        version = file_hash(target)
        key = str(target)

        if key in seen:
            return match.group(0)

        seen.add(key)

        new_tag = (
            f"{match.group('head')}"
            f"{path}?v={version}"
            f"{match.group('tail')}"
        )

        if new_tag != match.group(0):
            changed.append(f"{path} -> ?v={version}")

        return new_tag

    return IMG_SRC.sub(replace, content), changed


def main() -> int:

    if not README_PATH.exists():
        print("README.md not found", file=sys.stderr)
        return 1

    original = README_PATH.read_text(encoding="utf-8")

    check_only = "--check" in sys.argv

    updated, changed = rewrite(original)

    if check_only:
        if updated != original:
            print("README image versions are out of date:")
            for entry in changed:
                print(f"  {entry}")
            return 1
        print("README image versions are up to date")
        return 0

    if updated == original:
        print("README image versions already up to date")
        return 0

    README_PATH.write_text(updated, encoding="utf-8")

    for entry in changed:
        print(f"  {entry}")

    print(f"updated {len(changed)} image version(s)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
