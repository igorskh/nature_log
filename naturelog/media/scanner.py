"""Expand user-selected files and folders into a list of files."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path


def _hidden(name: str) -> bool:
    return name.startswith(".")


def expand_paths(paths: Iterable[str | Path]) -> list[Path]:
    """Files given directly plus all files found recursively in given folders.

    Hidden files and folders inside scanned folders are skipped. The result is
    de-duplicated and keeps discovery order.
    """
    seen: set[Path] = set()
    result: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved not in seen and resolved.is_file():
            seen.add(resolved)
            result.append(resolved)

    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not _hidden(d))
                for name in sorted(files):
                    if not _hidden(name):
                        add(Path(root) / name)
        else:
            add(path)
    return result
