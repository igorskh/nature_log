"""Thin read-only wrapper around the ExifTool command-line program."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import cache
from pathlib import Path
from typing import Any

_BATCH = 200
_MIN_BATCH = 10
_WORKERS = 4  # parallel ExifTool processes for metadata reads
# Hide the console window ExifTool would otherwise open on Windows.
_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


@cache
def exiftool_path() -> str | None:
    """Path of the ``exiftool`` executable, or ``None`` if it is not installed."""
    return shutil.which("exiftool")


def _run(args: Sequence[str], timeout: float) -> bytes:
    exe = exiftool_path()
    if exe is None:
        raise FileNotFoundError("exiftool is not installed")
    # Arguments go through stdin (-@ -) so long file lists and non-ASCII paths are safe.
    argfile = "\n".join(["-charset", "filename=utf8", *args]).encode("utf-8")
    result = subprocess.run(
        [exe, "-@", "-"],
        input=argfile,
        capture_output=True,
        timeout=timeout,
        creationflags=_CREATIONFLAGS,
    )
    return result.stdout


def _read_batch(batch: Sequence[Path], tags: Sequence[str]) -> dict[Path, dict[str, Any]]:
    args = ["-json", "-n", "-q", *(f"-{tag}" for tag in tags), *map(str, batch)]
    output = _run(args, timeout=60 + 2 * len(batch))
    found: dict[Path, dict[str, Any]] = {}
    if not output.strip():
        return found
    by_name = {str(p): p for p in batch}
    for record in json.loads(output.decode("utf-8", errors="replace")):
        source = record.pop("SourceFile", None)
        path = by_name.get(source) or (Path(source) if source else None)
        if path is not None:
            found[path] = record
    return found


def read_tags(
    paths: Sequence[Path],
    tags: Sequence[str],
    progress: Callable[[int], None] | None = None,
) -> dict[Path, dict[str, Any]]:
    """Read ``tags`` from each file. Values are numeric where ExifTool allows (``-n``).

    Files are split into batches read by parallel ExifTool processes.
    ``progress`` receives the number of files processed so far after each batch.
    """
    if not paths:
        return {}
    # Enough batches for parallelism and smooth progress, few enough to keep
    # ExifTool start-up overhead small.
    size = max(_MIN_BATCH, min(_BATCH, math.ceil(len(paths) / (_WORKERS * 4))))
    batches = [paths[i : i + size] for i in range(0, len(paths), size)]
    found: dict[Path, dict[str, Any]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(batches))) as pool:
        futures = {pool.submit(_read_batch, batch, tags): len(batch) for batch in batches}
        for future in as_completed(futures):
            found.update(future.result())
            done += futures[future]
            if progress is not None:
                progress(done)
    return found


def read_binary_tag(path: Path, tag: str) -> bytes:
    """The raw bytes of a binary tag (e.g. an embedded preview), or empty bytes."""
    return _run(["-b", f"-{tag}", str(path)], timeout=30)
