"""Copy file-system dates from source files to derived target files.

A target belongs to a source when its file name matches a pattern such as
``{basename}_*``, where ``{basename}`` is the source file name without its
extension (``250614_0712.wav`` -> ``250614_0712_bird.wav``). The target gets the
source's modification and creation dates, shifted by an offset written in the
target name (``+1m30s``), as chirppipe's ``SyncDatetime`` does.

Uses chirppipe's ``SyncDatetime`` (optional dependency) for the offset parsing
and for setting the modification date; the creation date is set here, since
``os.utime`` cannot change it. Only file-system metadata changes, never content.
"""

from __future__ import annotations

import ctypes
import fnmatch
import glob
import os
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATTERN = "{basename}_*"
BASENAME = "{basename}"


class FileDatetimeError(Exception):
    """Raised when dates cannot be mapped or applied."""


@dataclass(frozen=True)
class DatetimeMapping:
    """One target file and the source whose dates it receives."""

    source: Path
    target: Path
    #: Seconds added to the source dates (from ``+1m30s`` in the target name).
    offset: int
    modified: float
    created: float


def _sync_datetime():
    try:
        from chirps.file.sync_datetime import SyncDatetime
    except ImportError as exc:  # optional dependency
        raise FileDatetimeError(
            "chirppipe is not installed (install NatureLog with the 'species' extra)"
        ) from exc
    return SyncDatetime


def list_files(paths: list[Path]) -> list[Path]:
    """Expand folders to the visible files directly inside them."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(
                sorted(p for p in path.iterdir() if p.is_file() and not p.name.startswith("."))
            )
        elif path.is_file():
            files.append(path)
    return files


def creation_time(path: Path) -> float:
    """Creation (birth) time, or the modification time where none is recorded."""
    stat = path.stat()
    return getattr(stat, "st_birthtime", None) or stat.st_mtime


def map_files(sources: list[Path], targets: list[Path], pattern: str) -> list[DatetimeMapping]:
    """Pair each target with the source whose ``{basename}`` pattern matches its name.

    When several sources match, the longest basename wins (``A_1`` over ``A``).
    A file is never mapped to itself; unmatched targets are left out.
    """
    if BASENAME not in pattern:
        raise FileDatetimeError(f"The pattern must contain {BASENAME}")
    sync = _sync_datetime()
    by_length = sorted(set(sources), key=lambda p: len(p.stem), reverse=True)
    globs = [(src, pattern.replace(BASENAME, glob.escape(src.stem))) for src in by_length]

    mappings = []
    for target in dict.fromkeys(targets):
        for source, mask in globs:
            if fnmatch.fnmatch(target.name, mask) and target.resolve() != source.resolve():
                offset = sync.extract_time_offset_from_filename(target.name) or 0
                mappings.append(
                    DatetimeMapping(
                        source=source,
                        target=target,
                        offset=offset,
                        modified=source.stat().st_mtime + offset,
                        created=creation_time(source) + offset,
                    )
                )
                break
    return mappings


def apply_mapping(mapping: DatetimeMapping) -> None:
    """Set the target's modification and creation dates."""
    _sync_datetime().set_file_datetime(str(mapping.target), mapping.modified)
    set_creation_time(mapping.target, mapping.created)


# --- creation time ---------------------------------------------------------


def set_creation_time(path: Path, timestamp: float) -> None:
    """Set the file-system creation (birth) date; a no-op on Linux."""
    if sys.platform == "darwin":
        _set_creation_time_macos(path, timestamp)
    elif sys.platform == "win32":
        _set_creation_time_windows(path, timestamp)
    # Linux file systems do not allow setting the birth time.


class _AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


def _set_creation_time_macos(path: Path, timestamp: float) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    attrs = _AttrList(bitmapcount=5, commonattr=0x00000200)  # ATTR_CMN_CRTIME
    seconds = int(timestamp // 1)
    value = _Timespec(seconds, int((timestamp - seconds) * 1e9))
    result = libc.setattrlist(
        os.fsencode(path), ctypes.byref(attrs), ctypes.byref(value), ctypes.sizeof(value), 0
    )
    if result != 0:
        errno = ctypes.get_errno()
        raise FileDatetimeError(f"Cannot set creation date of {path.name}: {os.strerror(errno)}")


def _set_creation_time_windows(path: Path, timestamp: float) -> None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        str(path), 0x0100, 0x7, None, 3, 0x02000000, None  # FILE_WRITE_ATTRIBUTES, OPEN_EXISTING
    )
    if handle == wintypes.HANDLE(-1).value:
        raise FileDatetimeError(f"Cannot open {path.name}: {ctypes.WinError(ctypes.get_last_error())}")
    try:
        ticks = int((timestamp + 11644473600) * 10_000_000)  # 100 ns since 1601
        created = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
        if not kernel32.SetFileTime(handle, ctypes.byref(created), None, None):
            raise FileDatetimeError(
                f"Cannot set creation date of {path.name}: {ctypes.WinError(ctypes.get_last_error())}"
            )
    finally:
        kernel32.CloseHandle(handle)
