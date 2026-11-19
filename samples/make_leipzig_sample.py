"""Generate the Leipzig sample excursion: a GPX log, 3 photos and 2 recordings.

The walk loops through Clara-Zetkin-Park, Leipzig, on 2026-05-10 from 07:00 to
07:50 local time (Europe/Berlin, UTC+02:00). Every media file is captured
during the walk, so all of them can be placed on the track.

Output: ``leipzig/gps/`` holds the track, ``leipzig/media/`` the photos and
recordings (add that folder with "Add folder…").

- Photos store their capture time in EXIF ``DateTimeOriginal`` (local time,
  no GPS tags), like a camera would.
- Recordings have no capture time inside: NatureLog uses the file creation
  time. A git checkout resets it, so run this script again after cloning to
  set it (on macOS and Linux; on Windows use "Edit date and time…" instead).

Run from the project root::

    uv run python samples/make_leipzig_sample.py
"""

from __future__ import annotations

import math
import os
import random
import struct
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "leipzig"
TZ_OFFSET = timedelta(hours=2)  # CEST
START_LOCAL = datetime(2026, 5, 10, 7, 0, 0)
DURATION = timedelta(minutes=50)
FIX_INTERVAL = timedelta(seconds=5)

#: Loop through Clara-Zetkin-Park (lat, lon), back to the start.
ROUTE = [
    (51.32920, 12.35310),
    (51.32700, 12.34900),
    (51.32450, 12.35050),
    (51.32300, 12.35550),
    (51.32500, 12.36050),
    (51.32800, 12.36100),
    (51.33000, 12.35750),
    (51.32920, 12.35310),
]

#: (file name, minutes after the start, colours, caption)
PHOTOS = [
    ("P5100008.jpg", 8.5, ((70, 110, 50), (160, 190, 90)), "Sample photo 1"),
    ("P5100021.jpg", 22.25, ((40, 80, 120), (150, 190, 220)), "Sample photo 2"),
    ("P5100037.jpg", 37.75, ((110, 80, 40), (210, 170, 110)), "Sample photo 3"),
]
#: (file name, minutes after the start, chirp frequencies in Hz)
RECORDINGS = [
    ("260510_0715.wav", 15.0, (3800, 5200)),
    ("260510_0744.wav", 44.0, (2500, 4100)),
]
SAMPLE_RATE = 22050
RECORDING_SECONDS = 6


def _route_point(fraction: float) -> tuple[float, float]:
    """Position at ``fraction`` (0–1) of the route length, linear between corners."""
    legs = [
        math.dist(a, b) for a, b in zip(ROUTE, ROUTE[1:], strict=False)
    ]
    target = fraction * sum(legs)
    for (a, b), length in zip(zip(ROUTE, ROUTE[1:], strict=False), legs, strict=True):
        if target <= length:
            t = target / length
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        target -= length
    return ROUTE[-1]


def write_gpx(path: Path) -> None:
    rng = random.Random(51)
    steps = int(DURATION / FIX_INTERVAL)
    points = []
    for i in range(steps + 1):
        lat, lon = _route_point(i / steps)
        # A few metres of GPS noise.
        lat += rng.gauss(0, 0.00003)
        lon += rng.gauss(0, 0.00005)
        ele = 110 + 3 * math.sin(i / 40) + rng.gauss(0, 0.4)
        when = (START_LOCAL - TZ_OFFSET + i * FIX_INTERVAL).replace(tzinfo=UTC)
        points.append(
            f'      <trkpt lat="{lat:.7f}" lon="{lon:.7f}"><ele>{ele:.1f}</ele>'
            f"<time>{when.strftime('%Y-%m-%dT%H:%M:%SZ')}</time></trkpt>"
        )
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="NatureLog sample" xmlns="http://www.topografix.com/GPX/1/1">\n'
        "  <trk>\n    <name>Clara-Zetkin-Park, Leipzig</name>\n    <trkseg>\n"
        + "\n".join(points)
        + "\n    </trkseg>\n  </trk>\n</gpx>\n",
        encoding="utf-8",
    )


def write_photo(path: Path, when: datetime, colors, caption: str) -> None:
    width, height = 960, 640
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    (r1, g1, b1), (r2, g2, b2) = colors
    for y in range(height):
        t = y / (height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=(round(r2 + (r1 - r2) * t), round(g2 + (g1 - g2) * t), round(b2 + (b1 - b2) * t)),
        )
    draw.text((40, 40), f"{caption}\n{when:%Y-%m-%d %H:%M:%S}", fill="white", font_size=48)
    exif = Image.Exif()
    exif[0x010F] = "NatureLog"  # Make
    exif[0x0110] = "Sample camera"  # Model
    exif_ifd = exif.get_ifd(0x8769)
    exif_ifd[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")  # DateTimeOriginal
    exif_ifd[0x9011] = "+02:00"  # OffsetTimeOriginal
    image.save(path, quality=85, exif=exif)


def write_recording(path: Path, frequencies: tuple[int, int]) -> None:
    """Quiet background noise with a few bird-like down-chirps."""
    rng = random.Random(path.name)
    low, high = frequencies
    samples = []
    for n in range(SAMPLE_RATE * RECORDING_SECONDS):
        t = n / SAMPLE_RATE
        value = rng.gauss(0, 0.01)
        phase = t % 1.2  # a 0.25 s chirp every 1.2 s
        if phase < 0.25:
            freq = high - (high - low) * phase / 0.25
            envelope = math.sin(math.pi * phase / 0.25)
            value += 0.4 * envelope * math.sin(2 * math.pi * freq * phase)
        samples.append(max(-1.0, min(1.0, value)))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(b"".join(struct.pack("<h", round(s * 32767)) for s in samples))


def set_file_time(path: Path, when: datetime) -> None:
    """Set the modification time; on macOS an earlier time also moves the creation time."""
    stamp = (when - TZ_OFFSET).replace(tzinfo=UTC).timestamp()
    os.utime(path, (stamp, stamp))


def main() -> None:
    gps, media = OUT / "gps", OUT / "media"
    gps.mkdir(parents=True, exist_ok=True)
    media.mkdir(parents=True, exist_ok=True)
    write_gpx(gps / "20260510_clara_zetkin_park.gpx")
    for name, minutes, colors, caption in PHOTOS:
        write_photo(media / name, START_LOCAL + timedelta(minutes=minutes), colors, caption)
    for name, minutes, frequencies in RECORDINGS:
        path = media / name
        if not path.exists():
            write_recording(path, frequencies)
        set_file_time(path, START_LOCAL + timedelta(minutes=minutes))
    print(f"Sample data written to {OUT}")


if __name__ == "__main__":
    main()
