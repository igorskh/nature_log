"""Mel spectrograms of audio recordings, decoded with FFmpeg. Independent of Qt.

The recording is decoded to mono float PCM by ``ffmpeg`` (originals are only
read) and processed in blocks, so long recordings never sit in memory as raw
samples. Consecutive STFT frames are max-pooled so the result has at most
``max_frames`` columns while short calls stay visible.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import numpy as np

#: Highest sample rate the recording is decoded at (Nyquist = 24 kHz).
MAX_SAMPLE_RATE = 48_000
#: Sample rate used when the source rate is unknown.
DEFAULT_SAMPLE_RATE = 32_000
N_MELS = 128
#: Dynamic range shown, dB below the loudest bin.
DYNAMIC_RANGE_DB = 80.0
_BLOCK_SAMPLES = 1 << 18
_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class SpectrogramError(RuntimeError):
    """The recording could not be decoded."""


@dataclass(frozen=True, slots=True)
class MelSpectrogram:
    """Mel power in dB, shape ``(n_mels, frames)``; row 0 is the lowest band."""

    db: np.ndarray
    sample_rate: int
    duration: float  # seconds covered by the columns
    fmin: float
    fmax: float

    @property
    def n_mels(self) -> int:
        return int(self.db.shape[0])


def hz_to_mel(hz: float | np.ndarray) -> float | np.ndarray:
    """HTK mel scale."""
    return 2595.0 * np.log10(1.0 + np.asarray(hz) / 700.0)


def mel_to_hz(mel: float | np.ndarray) -> float | np.ndarray:
    return 700.0 * (10.0 ** (np.asarray(mel) / 2595.0) - 1.0)


@cache
def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


@cache
def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def _probe(path: Path) -> tuple[int | None, float | None]:
    """Sample rate and duration of the first audio stream, when ffprobe knows them."""
    exe = ffprobe_path()
    if exe is None:
        return None, None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate:format=duration", "-of", "json", str(path)],
            capture_output=True, timeout=30, creationflags=_CREATIONFLAGS,
        ).stdout
        info = json.loads(out or b"{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None
    streams = info.get("streams") or [{}]
    try:
        rate = int(streams[0].get("sample_rate", 0)) or None
    except (TypeError, ValueError):
        rate = None
    try:
        duration = float(info.get("format", {}).get("duration", 0)) or None
    except (TypeError, ValueError):
        duration = None
    return rate, duration


def _mel_filterbank(sample_rate: int, n_fft: int, n_mels: int, fmin: float, fmax: float) -> np.ndarray:
    """Triangular filters, shape ``(n_mels, n_fft // 2 + 1)``, area-normalised (Slaney)."""
    fft_hz = np.linspace(0.0, sample_rate / 2, n_fft // 2 + 1)
    edges = mel_to_hz(np.linspace(hz_to_mel(fmin), hz_to_mel(fmax), n_mels + 2))
    lower, center, upper = edges[:-2, None], edges[1:-1, None], edges[2:, None]
    rising = (fft_hz - lower) / (center - lower)
    falling = (upper - fft_hz) / (upper - center)
    weights = np.maximum(0.0, np.minimum(rising, falling))
    weights *= (2.0 / (upper - lower))
    return weights.astype(np.float32)


def compute_mel_spectrogram(path: Path, max_frames: int = 4000, n_mels: int = N_MELS) -> MelSpectrogram:
    """Mel spectrogram of ``path`` with at most ``max_frames`` columns.

    Raises :class:`SpectrogramError` if FFmpeg is missing or decodes nothing.
    """
    exe = ffmpeg_path()
    if exe is None:
        raise SpectrogramError("FFmpeg is not installed")
    source_rate, duration = _probe(path)
    sample_rate = min(source_rate or DEFAULT_SAMPLE_RATE, MAX_SAMPLE_RATE)
    n_fft = 1 << max(8, round(math.log2(sample_rate * 0.032)))  # ~32 ms window
    hop = n_fft // 2
    fmin, fmax = 0.0, sample_rate / 2
    filters = _mel_filterbank(sample_rate, n_fft, n_mels, fmin, fmax)
    window = np.hanning(n_fft).astype(np.float32)
    pool = max(1, math.ceil(duration * sample_rate / hop / max_frames)) if duration else 1

    process = subprocess.Popen(
        [exe, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-ac", "1",
         "-ar", str(sample_rate), "-f", "f32le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_CREATIONFLAGS,
    )
    assert process.stdout is not None
    columns: list[np.ndarray] = []
    pending = np.zeros(0, dtype=np.float32)  # samples not yet covered by a full frame
    pool_buffer = np.zeros((n_mels, 0), dtype=np.float32)  # frames not yet pooled
    total_samples = 0
    try:
        while True:
            raw = process.stdout.read(_BLOCK_SAMPLES * 4)
            if not raw:
                break
            raw = raw[: len(raw) // 4 * 4]
            block = np.frombuffer(raw, dtype="<f4")
            total_samples += block.size
            pending = np.concatenate((pending, block))
            count = (pending.size - n_fft) // hop + 1 if pending.size >= n_fft else 0
            if count <= 0:
                continue
            frames = np.lib.stride_tricks.sliding_window_view(pending, n_fft)[::hop][:count]
            power = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
            mel = filters @ power.T.astype(np.float32)
            pending = pending[count * hop:]
            pool_buffer = np.concatenate((pool_buffer, mel), axis=1)
            usable = pool_buffer.shape[1] // pool * pool
            if usable:
                head = pool_buffer[:, :usable].reshape(n_mels, -1, pool).max(axis=2)
                columns.append(head)
                pool_buffer = pool_buffer[:, usable:]
    finally:
        process.stdout.close()
        stderr = process.stderr.read() if process.stderr else b""
        process.wait()
        if process.stderr:
            process.stderr.close()
    if pool_buffer.shape[1]:
        columns.append(pool_buffer.max(axis=1, keepdims=True))
    if not columns:
        message = stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise SpectrogramError(message[-1] if message else "No audio could be decoded")

    mel = np.concatenate(columns, axis=1)
    if mel.shape[1] > max_frames:  # duration unknown or wrong: pool again
        extra = math.ceil(mel.shape[1] / max_frames)
        width = mel.shape[1] // extra * extra
        mel = mel[:, :width].reshape(n_mels, -1, extra).max(axis=2)
    db = 10.0 * np.log10(np.maximum(mel, 1e-12))
    db = np.maximum(db, db.max() - DYNAMIC_RANGE_DB)
    return MelSpectrogram(
        db=db.astype(np.float32),
        sample_rate=sample_rate,
        duration=total_samples / sample_rate,
        fmin=fmin,
        fmax=fmax,
    )
