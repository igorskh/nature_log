# Sample data

## Leipzig – Clara-Zetkin-Park

A 50-minute walk on 2026-05-10, 07:00–07:50 CEST (UTC+02:00), with media taken
along the way:

| File | Captured (local time) | Time source |
| --- | --- | --- |
| `leipzig/media/P5100008.jpg` | 07:08:30 | EXIF `DateTimeOriginal` |
| `leipzig/media/260510_0715.wav` | 07:15:00 | file creation time |
| `leipzig/media/P5100021.jpg` | 07:22:15 | EXIF `DateTimeOriginal` |
| `leipzig/media/P5100037.jpg` | 07:37:45 | EXIF `DateTimeOriginal` |
| `leipzig/media/260510_0744.wav` | 07:44:00 | file creation time |

Track: `leipzig/gps/20260510_clara_zetkin_park.gpx` (601 fixes, every 5 s).
Photos are plain generated images without GPS tags; recordings are synthetic
chirps, not real bird song.

### Try it

1. Set the timezone to **Europe/Berlin**.
2. Drop the GPX file onto the map (or add it on the **Tracks** tab).
3. **Add folder…** → `samples/leipzig/media`.

### After cloning

Git does not keep file times, so the recordings get the checkout time and
fall outside the track. Restore their times (macOS and Linux):

```bash
uv run python samples/make_leipzig_sample.py
```

The times are set in the computer's local timezone; on a computer outside
Central Europe, or on Windows, set them with **Edit date and time…** instead.
