# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Statistics for a personal photo archive. `scan.py` reads EXIF and image data from
photo folders into one SQLite file; `app.py` serves a local web interface over it;
`launcher.py` is a Tk window that drives both for people who do not use a terminal.
Everything runs locally — see **Privacy** below, which is a design constraint, not
a disclaimer.

## Commands

`launcher.py` runs under whatever Python started it and bootstraps a repo-local
`.venv` on first run, then launches `scan.py` and `app.py` with that interpreter.
For development, use the venv directly — `.venv/Scripts/python.exe` on Windows,
`.venv/bin/python` elsewhere. `python` below means that interpreter.

```bash
python tools/smoke_test.py          # the whole test suite
python tools/check_license.py       # LICENSE must be the canonical AGPL text
python -m compileall -q scan.py app.py launcher.py version.py dupkey.py tools
python tools/make_icon.py           # redraw the icon files (they are committed)
```

Running the program:

```bash
python launcher.py                                   # the Tk window (photostats.bat is the user's entry point)
python scan.py "D:\Photos"                           # EXIF pass
python scan.py --colors-only                         # image analysis pass
python scan.py --hash-only                           # content signatures
python scan.py --check-dups "E:\Card"                # read-only folder check
python app.py --no-browser --port 5577               # the server alone
```

`tools/smoke_test.py` is one end-to-end script, not a test framework: it builds a
small generated archive in a temp folder, runs every pass, starts the server and
hits every endpoint. There is no flag to select a single check — to iterate on one,
call the relevant function (e.g. `check_dups_pass`) from your own script. It touches
nothing outside its temp folder and is what CI runs (Windows and Linux, 3.10 and 3.12).

## Architecture

**One flat table, no ORM.** `photos` holds a row per file, with EXIF columns, image
metrics and `path`. Panels in the interface are `GROUP BY` over that table, and
filters are a `WHERE` assembled in `app.py:build_where()` from repeated query
parameters (`month=9&month=10`, not comma-separated — camera names contain commas).
Each chart skips its own dimension when building the clause so it does not truncate
itself.

**Passes are separate and resumable.** EXIF, image analysis and signatures are three
independent passes over the same rows; each stores what it has done in batches, so
stopping one loses nothing. A pass and the server run at the same time — both open
SQLite with WAL and `busy_timeout` (`scan.py:tune()`, `app.py:db()`), which is what
makes concurrency work. Do not remove either.

**The launcher is a process supervisor, not a library.** It spawns `scan.py`/`app.py`
as subprocesses and does no work in-process, which is why new passes belong in
`scan.py` with a button in `launcher.py`. Two consequences:

- The progress bar is scraped from child stdout by the `PROGRESS` and `LEFT` regexes
  in `launcher.py`. A pass that prints progress in a different shape gets no bar.
- Stop sends `CTRL_BREAK_EVENT` (not `CTRL_C_EVENT`, which would hit the launcher's
  own process group). Python terminates on CTRL_BREAK rather than raising
  `KeyboardInterrupt`, so a pass that must print something on the way out installs a
  `SIGBREAK` handler — see `scan.py:stop_on_break()`. Do not install one globally:
  passes using `with ThreadPoolExecutor` would then block on shutdown waiting for
  every queued job.

**The interface is a single file.** `web/index.html` is markup, CSS and JS in one
file with no build step and no package manager. Leaflet and map tiles are the only
external fetch, loaded on demand behind a button.

## Invariants that span files

**The duplicate rule lives in `dupkey.py`.** `app.py` (the duplicates panel) and
`scan.py --check-dups` both import it. The rule is a signature when every row has
one, otherwise name + exact size + capture time. Two traps:

- `name_key()` is also the text of the `ix_dupkey` index built in `app.py` at
  startup. Changing the expression without rebuilding the index makes duplicate
  queries table-scan.
- Build candidate keys with SQLite, not Python. SQLite's `lower()` folds only ASCII;
  `str.lower()` folds Cyrillic too, so the two disagree on every non-Latin filename.
  `scan.py:candidate_keys()` evaluates the key through the same connection for this
  reason.

**The migration column list is duplicated.** `scan.py:migrate()` and the startup
block in `app.py:main()` both `ALTER TABLE ... ADD COLUMN` the same list, because
either program may meet an old database first. Adding a column means editing both.

**Two version counters, different meanings.** `ALGO_VERSION` (`scan.py`) bumps when
the image-analysis maths changes and triggers a re-analysis of the archive.
`EXIF_VERSION` bumps when the EXIF field set changes and triggers a metadata re-read
that keeps the image analysis. Thresholds in `app.py` apply in the query and need
neither — that is deliberate, so they can be tuned without touching the archive.

**Colour is a vote over 48×48 pixels, not an average**, with warm tones split by
purity and brightness rather than hue. The thresholds at the top of `scan.py` are
commented individually; the README explains why each one exists. Changing them
without bumping `ALGO_VERSION` leaves the archive inconsistent.

## Conventions

- Comments, docstrings and user-facing output are **English everywhere**. The
  Russian comments the program was first written with were translated in full; do
  not reintroduce any.
- Three pieces of Cyrillic are *data* and must stay: the `COLOR_RENAME` keys in
  `app.py`, which have to match the colour names stored in old databases, and the
  `rstrip("sс")` in `app.py` and `scan.py`, which strips a Cyrillic seconds suffix
  from a shutter value. `README.ru.md` and the link to it are Russian by design.
- Comments explain *why*, usually naming the failure that motivated the code. Keep
  that register; do not add comments that restate the line below them.
- `README.md` and `README.ru.md` are parallel documents — a user-visible change
  belongs in both. `CHANGELOG.md` follows Keep a Changelog and semver; `version.py`
  is the single place the version lives.

## Privacy

These are requirements, not defaults:

- `photos.db` and `cache/` hold file paths, coordinates and pieces of the photos.
  `.gitignore` keeps them out of git.
- The server binds `127.0.0.1`.
- The map is the **only** outbound request the program ever makes, it is behind a
  button, and the place list works offline. Do not add a fetch that runs without
  the user asking.
