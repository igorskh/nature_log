# NatureLog - GeoTag and Upload Diverse Nature Observations

## Project Overview

NatureLog is a cross-platform, local-first desktop application for managing georeferenced biodiversity observations.

It imports GPX tracks, photographs and audio recordings from multiple devices, synchronizes media with GPS positions, manages processed media, groups files into observations and prepares data for iNaturalist.

## Core principles

- Preserve original media unchanged.
- Support multiple devices and GPS tracks per excursion.
- Require user approval for observation grouping.
- Keep projects portable and usable offline.
- Prefer open formats and interoperable data.

## Technology Stack

Use the following technologies unless a documented reason justifies alternatives:

Package and environment management:
- uv

- Python
- PySide6 / Qt 6
- SQLite
- GeoPackage
- gpxpy and pyproj
- ExifTool
- FFmpeg / FFprobe
- Pillow
- Leaflet
- pytest

Keep dependencies minimal and verify compatibility across Windows, macOS and Linux.

## Architecture

Use a modular architecture with clear separation between:
- Core domain logic
- Database and repositories
- GPS synchronization
- Photo and audio processing
- External integrations
- User interface

###  Rules
- Keep business logic independent of the UI.
- Keep GPS calculations independent of Qt.
- Use typed interfaces and documented models.
- Avoid circular dependencies and unnecessary abstractions.
- Keep long-running tasks off the UI thread.
- Make processing operations traceable and recoverable.

## Timestamp and GPS Synchronization

GPS synchronization is a critical component of NatureLog.

- Support multiple GPS tracks and recording devices.
- Preserve original timestamps.
- Use timezone-aware timestamp handling. Let user choose timestamp offset for GPS tracks.
- Support device-specific clock corrections.
- Match media to timestamped GPX positions.
- Use appropriate geographic interpolation.
- Never silently extrapolate or invent coordinates.

## General rules

- Propose implementation plan before generating code
- Avoid unnecessary tests and check loops
- Inspect the existing implementation.
- Identify affected modules and dependencies.
- Propose a concise implementation plan.
- Implement incrementally.