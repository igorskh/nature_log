from datetime import datetime, timedelta, timezone

import pytest

from naturelog.gps import load_track, position_at
from naturelog.gps.nmea import NmeaError, parse_nmea

UTC = timezone.utc

NMEA = """\
$GPGGA,235959.00,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*65
$GPRMC,235959.00,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*48
$GPGGA,000001.00,4807.138,N,01131.100,E,1,08,0.9,547.4,M,46.9,M,,*67
$GPRMC,000001.00,V,4807.138,N,01131.100,E,022.4,084.4,240394,003.1,W*58
$GPRMC,000003.00,A,4807.238,N,01131.200,E,022.4,084.4,240394,003.1,W*4D
$GPRMC,000005.00,A,4807.238,N,01131.200,E,022.4,084.4,240394,003.1,W*00
"""


def test_nmea_merges_sentences_and_rolls_over_midnight():
    fixes = parse_nmea(NMEA)
    assert [f[0] for f in fixes] == [
        datetime(1994, 3, 23, 23, 59, 59, tzinfo=UTC),
        datetime(1994, 3, 24, 0, 0, 1, tzinfo=UTC),
        datetime(1994, 3, 24, 0, 0, 3, tzinfo=UTC),
    ]  # last line has a bad checksum
    assert fixes[0][1:] == pytest.approx((48.1173, 11.516667, 545.4), abs=1e-6)
    assert fixes[2][3] is None


def test_nmea_without_date_is_rejected():
    with pytest.raises(NmeaError, match="no date"):
        parse_nmea("$GPGGA,000001.00,4807.138,N,01131.100,E,1,08,0.9,547.4,M,46.9,M,,*67\n")


def test_load_nmea_and_interpolate_without_extrapolation(tmp_path):
    path = tmp_path / "walk.nmea"
    path.write_text(NMEA)
    track = load_track(path)
    assert track.source_format == "nmea" and track.point_count == 3

    mid = position_at(track, track.start + timedelta(seconds=1))
    assert mid.interpolated
    assert mid.latitude == pytest.approx((48.1173 + 48.118967) / 2, abs=1e-5)
    assert mid.elevation == pytest.approx(546.4)
    assert not position_at(track, track.start).interpolated
    assert position_at(track, track.start - timedelta(seconds=1)) is None
    assert position_at(track, track.end + timedelta(seconds=1)) is None


def test_gpx_gap_between_segments_has_no_position(tmp_path):
    path = tmp_path / "t.gpx"
    path.write_text(
        '<gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1"><trk>'
        '<trkseg><trkpt lat="1" lon="1"><time>2026-01-01T10:00:00Z</time></trkpt>'
        '<trkpt lat="1" lon="2"><time>2026-01-01T10:01:00Z</time></trkpt></trkseg>'
        '<trkseg><trkpt lat="2" lon="2"><time>2026-01-01T11:00:00Z</time></trkpt>'
        '<trkpt lat="2" lon="3"><time>2026-01-01T11:01:00Z</time></trkpt></trkseg>'
        "</trk></gpx>"
    )
    track = load_track(path)
    assert len(track.segments) == 2
    assert position_at(track, datetime(2026, 1, 1, 10, 30, tzinfo=UTC)) is None
    assert position_at(track, datetime(2026, 1, 1, 11, 0, 30, tzinfo=UTC)).longitude == pytest.approx(2.5, abs=1e-3)
