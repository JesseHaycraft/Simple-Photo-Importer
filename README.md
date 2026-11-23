# Simple Photo Importer

A small Windows app that copies new photos from a camera card into a photo library organised by date and event.

- Finds the photos on the card that are not already in the library and shows only those.
- Groups them by capture date, with thumbnails. For back-to-back days it asks whether they are the same event or trip; a single day can also be split into several events.
- You name each event, and everything is imported in one go into folders such as:

  ```
  Photos life\
    2026-11-04 Week in Mexico\RAW\
    2026-11-19 Tom's birthday\RAW\
    2026-11-22 Car race\RAW\
  ```

  A multi-day event uses its first day as the date. The `RAW` subfolder name can be changed or left empty.

**The card is never modified.** Files are only read from it and copied. Nothing on the card is deleted, moved or renamed, and no existing file in the library is overwritten.

## Requirements

- Windows 10 or 11 with Microsoft Edge (or Chrome)
- Python 3.9 or newer with Pillow: `python -m pip install pillow`

## Running it

Double-click `Photo Importer.bat`. The first time, click the library path in the top bar to choose your library folder.

The card must show up as a drive letter (a card reader, or the camera in USB mass-storage mode).

## How "already imported" is decided

A file on the card counts as already imported when the library contains:

- a file with the same name and size, or
- a file with the same name taken at the same moment (for example a copy that was edited afterwards), or
- for a JPG, its RAW twin taken at the same moment.

A library folder can be left out of this check (for example a folder of unsorted photos) from the "where?" list in the app.

## Files the app creates

Next to the app: `settings.json` (library location and options), `ignored.json` (photos marked "Never") and `logs\` (a list of every file copied by each import). Thumbnails are cached under `%LOCALAPPDATA%\PhotoImporter`.
