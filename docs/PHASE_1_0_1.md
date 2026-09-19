# Phase 1.0.1 — Stability and Year/Location Fix

This build addresses the Phase 1.0 crash observed during **Review & Process → Year → Location**.

## Main changes

- Replaced GUI worker `QObject + QThread` lifecycle with dedicated `QThread` subclasses.
- GUI no longer releases a worker QObject while its thread is stopping.
- Added explicit unhandled-exception logging.
- Copy reconciliation always compares the destination against the **complete expected source population**.
- A failed copy therefore cannot make the lightweight destination check incorrectly pass.
- Year → Location organization remains available.
- Destination-only duplicate deletion now runs in a background thread.
- Delete review rows are captured before deletion; reports no longer stat deleted files.
- All regular files are processed by default. OS metadata exclusion is an explicit option.

## Year → Location behavior

The folder structure is:

`<Creation Year>/<Creation Location>/<generated filename>`

For images, the year prefers EXIF capture time and location uses EXIF GPS coordinates. No online reverse-geocoding is performed.

For files without usable image EXIF, the year falls back to filesystem creation time (or modification time where creation time is unavailable), and location is `Unknown Location`.

## macOS / Windows

Build the application on the target operating system because PyInstaller does not cross-compile macOS applications from Windows or Windows applications from macOS.
