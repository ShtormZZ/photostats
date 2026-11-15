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
python tools/test_database.py       # migrations, concurrency and planner policy
python tools/check_license.py       # LICENSE must be the canonical AGPL text
python -m compileall -q scan.py app.py launcher.py version.py dupkey.py tags.py jobs.py db_schema.py tools
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
metrics and `path`. Tags are the one exception, in a table of their own — see the
invariants below. Panels in the interface are `GROUP BY` over that table, and
filters are a `WHERE` assembled in `app.py:build_where()` from repeated query
parameters (`month=9&month=10`, not comma-separated — camera names contain commas).
Each chart skips its own dimension when building the clause so it does not truncate
itself.

**Passes are separate and resumable.** EXIF, image analysis and signatures are three
independent passes over the same rows; each stores what it has done in batches, so
stopping one loses nothing. A pass and the server run at the same time — both open
SQLite with WAL and `busy_timeout` (`db_schema.py:tune()`, `app.py:db()`), which is what
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

**Pass status goes through the database, `jobs.py` holds it.** Each writing pass in
`scan.py` runs inside `with jobs.Job(db, kind) as job:` and only sets
`job.total`/`job.done`/`job.phase`; a heartbeat thread with its own connection
writes them, so the work loop never waits on its own status. A pass killed by
CTRL_BREAK cannot write an end state, which is why `jobs.latest()` reads a
`running` row whose heartbeat is older than `STALE` as stopped — do not
"fix" that by adding a SIGBREAK handler (see above). A new pass needs a
`KINDS` entry and a `Job`; `--check-dups` deliberately has none, as it never
writes. `db_schema.py` creates the job table along with the rest of the schema.

**The interface is a single file.** `web/index.html` is markup, CSS and JS in one
file with no build step and no package manager. Leaflet and map tiles are the only
external fetch, loaded on demand behind a button.

**Timeline owns its filters.** `photoTimeline` holds match-any tags, rating mode
and the unrated toggle in memory only; do not copy Statistics filters into it or
persist these options. `app.py:timeline_filter()` serves date discovery, counts,
highlights, expanded pages and cards. Keep tag matching correlated with the photo
so date queries retain `ix_taken`. The independent 15 per day toggle limits
results after all filters, including Include unrated; Show all expands every
matching photo when the filtered count exceeds 15. With the limit off, matching
days expand automatically whether unrated photos are included or not.
Day-wide Add tags uses the same predicate without a display limit. Preview and
write count all matching photos, warn about excluded photos, and preserve existing
tags. The write freezes targets in one transaction and rejects the whole batch
on a three-tag capacity conflict or a stale preview revision.
Remove tags shares that endpoint with `operation=remove` and repeated `remove_tag`
fields. Its preview lists every tag on matching photos with counts, never archive
suggestions. Removal has no three-tag selection limit; freeze targets before
deleting so removing a filter tag cannot skip later requested tags. Preserve
unselected tags, reject stale revisions and refresh the day's membership after save.
The client sorts highlights and full days by group (great first, other ratings
and unrated together, bad last), then `(taken, id)`, for display and card
navigation. Keep highlight selection separate from display order. Expanded days
collect lightweight metadata in batches before revealing their globally sorted
photos; loading pauses for unmounted/collapsed days. Do not sort only each batch
or put the original highlights ahead of earlier photos in a full day.
Thumbnail records include tooltip fields (camera/lens, exposure settings, tags,
comment), but no paths, coordinates or image-analysis metrics. Tooltips must not
fetch per hover. Keep annotation badges/tooltips in sync in highlights, full-day
caches and mounted nodes after saves; comment edits need no day reload.
One optional extra dimension/value (year, camera, lens, focal or aperture range)
lives in the same state and predicate. `/api/timeline/options` returns bounded
searchable choices under tag/rating filters, excluding its own current selection.
Reuse Statistics' bucket constants; focal lengths are actual mm. Keep scalar
metadata checks correlated too, to retain date-ordered scans for broad ranges.
Analyse day always opens the whole date in Statistics. Run
`tools/test_timeline.py` and `tools/timeline_browser_test.py` for changes here.

## Invariants that span files

**The duplicate rule lives in `dupkey.py`.** `app.py` (the duplicates panel) and
`scan.py --check-dups` both import it. The rule is a signature when every row has
one, otherwise name + exact size + capture time. Two traps:

- `name_key()` is also the text of the `ix_dupkey` index built in `db_schema.py` at
  startup. Changing the expression without rebuilding the index makes duplicate
  queries table-scan.
- Build candidate keys with SQLite, not Python. SQLite's `lower()` folds only ASCII;
  `str.lower()` folds Cyrillic too, so the two disagree on every non-Latin filename.
  `scan.py:candidate_keys()` evaluates the key through the same connection for this
  reason.

**`mark` and `comment` are the columns the scanner must not own.** Every column in
`scan.py:COLUMNS` is rewritten from the file by the upsert at the end of the EXIF
pass, so a rating a user set by hand would be destroyed the next time
`EXIF_VERSION` changed. `rating` (from EXIF) is in COLUMNS; `mark` (set in the
interface) is deliberately not, and `app.py:rating_key()` folds the two into the
one rating shown, filtered and sorted. `comment` (the user's note, at most 256
characters, rule in `app.py:clean_comment()`) stays out of COLUMNS for the same
reason. Smoke-test checks re-scan with `--full` and fail if either does not
survive.

**Tags are the one thing not in `photos`, and `tags.py` holds the rule.** A photo
takes up to three, which is not the shape of a column, so they live in
`photo_tags` (photo_id, tag) — the only table the panels join to. Three things
to know:

- `db_schema.py` owns the tag table, index and cleanup trigger. The compatibility
  `tags.ensure_schema()` entry point delegates to its versioned migrations.
- The trigger is what keeps the table honest — the scanner deletes rows for files
  that are gone, and without it their tags stay behind and are counted. A foreign
  key would not do: `PRAGMA foreign_keys` is off by default and per connection.
- Fold case in Python (`casefold`, and NFC before it), never in SQL — the same
  trap as `dupkey`, from the other side. Because the stored form is already
  folded, every comparison is a plain `=`. `app.py:tag_counts()` skips the join
  entirely when nothing is filtered: measured on 105k photos, the join cost
  317 ms against 9 for the same count straight off `ix_tag`.

**The schema has one owner.** `db_schema.py` creates tables, backfills legacy
columns, translates old colour names and reconciles index expressions. The app,
scanner and job writer all use it. Migrations and success records run under one
`BEGIN IMMEDIATE`; do not commit inside a migration or use `executescript`, which
can commit implicitly. A verified backup precedes changes to a nonempty catalogue.
See `docs/database-maintenance.md` for recovery and the planner policy.

**Data revision is transactional.** Database triggers update `db_state.revision`
for changes to photos, tags and algorithm/signature metadata. Job heartbeats and
maintenance do not invalidate data. Keep new persisted photo/annotation fields
covered by the revision triggers when adding a migration.

**Statistics maintenance belongs at writer boundaries.** Startup and completed
passes call `maintain_statistics`, never a GET. Index changes, missing statistics
or substantial photo changes trigger analysis. A range index is not always
faster: measure complete requests on a disposable copy with
`tools/db_benchmark.py`, not just one COUNT query. The diagnostic endpoint only
reads metadata. Server request connections must be closed explicitly.

**Three version counters, different meanings.** `ALGO_VERSION` (`scan.py`) bumps when
the colour/light maths changes and triggers a re-analysis of the archive.
`EXIF_VERSION` bumps when the EXIF field set changes and triggers a metadata re-read
that keeps the image analysis. `SHARP_VERSION` bumps when the sharpness maths
changes; it is stored per row (`sharp_v`), so only sharpness is re-measured, and a
photo that could not be measured is still marked as tried. Thresholds in `app.py`
(including the sharpness bands and `FOCUS_RATIO`) apply in the query and need
none of them — that is deliberate, so they can be tuned without touching the archive.

**Sharpness decodes separately from colour.** `load_gray()` decodes at 1500 px for
`sharpness()`; `load_small()` keeps its 1/8 draft for colour. Sharing one decode
would move the 48×48 colour grid onto different pixels and shift colours without
an `ALGO_VERSION` bump. `stats_of()` computes only what a row is missing. The
arithmetic needs `ImageMath.lambda_eval` (Pillow ≥ 10.3); Pillow's 8-bit filters
clip the negative half of the Laplacian, so do not swap it for `ImageFilter.Kernel`.

**Colour is a vote over 48×48 pixels, not an average**, with warm tones split by
purity and brightness rather than hue. The thresholds at the top of `scan.py` are
commented individually; the README explains why each one exists. Changing them
without bumping `ALGO_VERSION` leaves the archive inconsistent.

## Conventions

- Comments, docstrings and user-facing output are **English everywhere**. The
  Russian comments the program was first written with were translated in full; do
  not reintroduce any.
- Three pieces of Cyrillic are *data* and must stay: the `COLOR_RENAME` keys in
  `db_schema.py`, which have to match the colour names stored in old databases, and the
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
- The server binds `127.0.0.1`. The one way off it is the launcher's network
  box (or an explicit `--host`): off at every start, never saved, red warning,
  a private LAN address only (`launcher.py:lan_address()`), never `0.0.0.0`.
- `/api/reveal` and `/api/open` start programs on the server's machine, so
  `app.py:files_here()` allows them only to a loopback client of a
  loopback-bound server. Any new endpoint that acts on the machine goes behind it.
- The map is the **only** outbound request the program ever makes, it is behind a
  button, and the place list works offline. Do not add a fetch that runs without
  the user asking.
