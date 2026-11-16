# Changelog

The changes that matter to whoever uses PDF Cropper. The versions up to
1.1.6 are described in their GitHub releases.

## [Unreleased]

## [1.1.7] - 2026-11-16

### Changed

- **Licence**: from this version PDF Cropper is under the GNU General
  Public License, version 3 or later (GPL-3.0-or-later). It now carries
  copies of modules shared with the author's other apps, which are under
  that licence. The versions up to 1.1.6 were released under the MIT
  licence and stay so. The licence's text comes with the program
  (`_internal\licenses` in the installed or unpacked program, with the
  texts of the libraries it carries; `share/licenses` in the Flatpak).
- Windows: the check for a newer version is made when the window opens and
  when it comes back from the taskbar, every 4 hours at most (before: once
  a day, at start). The Preferences say so.
- The preferences are written whole or not at all, and saving tries again
  for a moment when Windows holds the file (an antivirus, the indexer);
  a save that fails is written in the errors file.
- The message of an installer refused for its signature says "not signed
  by the project" (in Italian "non è firmato dal progetto").

### Added

- The window's errors are kept in `errors.log` in the app's state folder
  (`%LOCALAPPDATA%\pdf-cropper\state` on Windows,
  `~/.local/state/pdf-cropper` on Linux), rotated past 1 MB.
- *About → Troubleshooting*: version, folders, the errors file and the
  preferences, to copy into a report of a problem.
