"""Prepare the Leipzig sample excursion: write the GPX log, set recording times.

The walk loops through Clara-Zetkin-Park, Leipzig, on 2026-05-10 from 07:00 to
07:50 local time (Europe/Berlin, UTC+02:00). Every media file is captured
during the walk, so all of them can be placed on the track.

Output: ``leipzig/gps/`` holds the track, ``leipzig/media/`` the photos and
recordings (add that folder with "Add folder…").

- Photos store their capture time in EXIF ``DateTimeOriginal`` (local time,
  no GPS tags); they are committed as they are.
- Recordings have no capture time inside: NatureLog uses the file creation
  time. A git checkout resets it, so run this script after cloning to set it
  (on macOS and Linux; on Windows use "Edit date and time…" instead).

Run from the project root::

    uv run python samples/make_leipzig_sample.py
"""

from __future__ import annotations

import math
import os
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

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

#: Recordings and their start time (minutes after the walk starts). The photos
#: keep their capture time in EXIF, so they need no fixing after a checkout.
RECORDINGS = [
    ("2034488.wav", 14.0),
    ("XC1073921.wav", 31.0),
]


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


def set_file_time(path: Path, when: datetime) -> None:
    """Set the modification time; on macOS an earlier time also moves the creation time."""
    stamp = (when - TZ_OFFSET).replace(tzinfo=UTC).timestamp()
    os.utime(path, (stamp, stamp))


def main() -> None:
    gps, media = OUT / "gps", OUT / "media"
    gps.mkdir(parents=True, exist_ok=True)
    write_gpx(gps / "20260510_clara_zetkin_park.gpx")
    for name, minutes in RECORDINGS:
        path = media / name
        if path.is_file():
            set_file_time(path, START_LOCAL + timedelta(minutes=minutes))
        else:
            print(f"Missing recording: {path}")
    print(f"Sample track written and recording times set in {OUT}")


if __name__ == "__main__":
    main()
