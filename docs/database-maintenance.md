# Database migration and recovery

`db_schema.py` is the schema owner for `app.py`, `scan.py`, `jobs.py` and the
compatibility entry points in `tags.py`. It serialises startup with
`BEGIN IMMEDIATE`, then commits DDL, bounded-memory exposure backfill, colour
normalisation, index changes, migration history and `PRAGMA user_version` together.
Failed transactions leave the previous version intact. Backfill resumes by
repeating the rolled-back migration; it has no separately committed partial state.
The logical column definitions converge; physical column order in legacy SQLite
tables is retained to avoid rebuilding photographs and changing their IDs.

The migration versions are:

1. Adopt actual legacy tables, add missing columns and backfill exposure in batches.
2. Reconcile all managed photo indexes, including duplicate/rating expressions.
3. Install the persistent revision and photo-change counter with database triggers.
4. Translate legacy colour names without rereading photographs.

Migration history records only committed successes. A failed migration has no
success record and is retried. Newer unsupported schemas or a version/history
mismatch stop startup. Index definitions are also checked on repeat startup,
so a changed expression cannot silently reuse an old index with the same name.
`db_state.revision` is an invalidation counter, not a count of user actions: deleting
one photo with tags may increment it several times. It rolls back with the data.
Job heartbeats and planner maintenance do not increment it.

## Backups and recovery

Before changing a nonempty persistent database, startup holds the writer lock
and copies the committed snapshot through a separate read-only connection and
SQLite's backup API. This includes committed WAL data. The copy must pass
`PRAGMA quick_check` before any migration statements execute. Copies are retained
under `<database filename>.backups/schema-<old version>-<unique id>.db`.
`meta.schema_backup` identifies the most recent successful migration's backup;
the pre-migration copy itself retains its original schema version.

To recover:

1. Stop the launcher, server and all scanners; close any tools using the catalogue.
2. Keep the current database and any matching `-wal`/`-shm` files together in a
   separate recovery folder. Do not reuse those sidecars with the restored database.
3. Copy the chosen checked backup to the database's original filename. The backup
   is a standalone SQLite file and has no required WAL/SHM sidecars.
4. Check it with `sqlite3 <database> "PRAGMA integrity_check;"` (expect `ok`), or
   Python's built-in `sqlite3` module if the SQLite CLI is unavailable.
5. Start the compatible application version. The current application will retry
   its migrations and make a new pre-migration backup.

The regression suite restores backups into a separate destination with SQLite's
backup API and verifies the complete legacy photo row. It also verifies backup
failure, interrupted migration, ID/annotation preservation and concurrent startup.
These are catalogue backups; original photograph backups remain separate.
General backup scheduling, checkpoint policy and a restore UI belong to DB-05.

## Planner policy and diagnostics

The maintenance boundary is application/scanner startup and completion of EXIF,
image or signature passes, including deletion-only passes. An empty catalogue
defers analysis until it contains photographs. A pending index change or missing
`sqlite_stat1` for photos triggers analysis; subsequent work triggers it after
`max(1000, ceil(previous_photo_count * 0.1))` changed photo rows. Updates count as
well as inserts/deletes, since rating distributions also affect plans. Tags alone
change revision without forcing expensive photo analysis.

Use full, table-scoped `ANALYZE photos` and `ANALYZE photo_tags`, temporarily
setting `analysis_limit=0` and restoring its previous value afterward. SQLite
3.46+ additionally runs `PRAGMA optimize=0x10002` for other eligible tables.
This policy deliberately does not rely on optimize's heuristics to notice a
10% change or rating-only updates. Older SQLite lacks the newer bounded optimize
behaviour. See the [SQLite ANALYZE documentation](https://www.sqlite.org/lang_analyze.html).
No query uses `INDEXED BY`.

Statistics and the success metadata commit together. Failure rolls them back,
preserves the last success and records a separate error for diagnostics. Startup
and subsequent writer boundaries retry when statistics are still missing, an
index change is pending or the change threshold remains exceeded.
`GET /api/db/diagnostics` only reads: SQLite/schema versions, migration results,
revision, photo-change count, pending work, last maintenance and last error.
Dates are UTC ISO 8601, with the offset included.

## Verification

Run `python tools/test_database.py`, `python tools/smoke_test.py` and
`python tools/check_license.py`. CI runs these on Windows/Linux and Python
3.10/3.12. Local execution validates the current host only.

`tools/db_benchmark.py` uses a temporary fixture or a read-only backup of
`--source`, measures the same database before and after migration/analysis, and
emits JSON without personal paths or coordinates. Default sampling is 5 warmups
and 30 measured requests per endpoint. It checks seeded Timeline responses and
other deterministic API results by checksum, records SQL and connection counts,
and flags median increases above 10% for investigation. The random endpoint's
response checksum is intentionally excluded. It measures server work through
the Flask test client, excluding browser/network/thumbnail decoding. OS file
caches are not flushed. Synthetic distributions cover unrated/bad-heavy archives,
many great ratings, sparse dates and one large day.

## Measurements on 13 November 2026

Windows, Python 3.12.7, SQLite 3.45.3; 5 warmups and 30 measured requests per
scenario on the same temporary database before/after analysis. The source
catalogue remained at schema version 0, without migration/statistics tables.
[Full catalogue measurements](db-benchmark-2026-11-13.json) and
[five synthetic distributions](db-benchmark-synthetic-2026-11-13.json) contain
the machine-readable results.

For the catalogue copy with 132,761 photographs:

| Scenario | Median before, ms | Median after, ms | p95 after, ms |
|---|---:|---:|---:|
| Timeline, 8 days | 1388.051 | 3.589 | 3.871 |
| Timeline, largest day highlights | 126.451 | 8.437 | 8.818 |
| Expanded day, 200 photos | 2.447 | 2.621 | 2.848 |
| Statistics, archive | 2193.596 | 2190.265 | 2216.504 |
| Statistics, largest day | 734.632 | 738.113 | 769.487 |
| Directories | 163.094 | 162.752 | 164.736 |
| Photo list, 120 photos | 13.199 | 13.530 | 14.268 |
| Random strip | 65.304 | 65.425 | 68.668 |

The Timeline date search changed from `MULTI-INDEX OR` over `ix_rating` with
temporary sorting to a range search over `ix_taken`. Deterministic responses,
including seeded highlights, matched exactly. No catalogue scenario's median
increased more than 10%. Analysis took about 225 ms. Statistics remains expensive
and is still work for DB-03; these changes do not implement a statistics cache.

Synthetic runs use 10,000 rows per distribution. Every deterministic response
matched, and all date searches used `ix_taken` after analysis. One explicit
tradeoff exceeds the relative 10% threshold: with 99% bad marks, Timeline median
rose from 3.814 to 4.633 ms (+0.819 ms, about 21.5%). In this small, extremely
selective fixture, the old rating-index search plus sorting is cheaper than
walking dates past excluded photos. The date plan yields large gains for unrated,
great-heavy, sparse and large-day fixtures and the control catalogue. This
sub-millisecond synthetic regression is accepted here without forced indexes;
it is recorded rather than treated as a universal performance improvement.
Windows was exercised locally; Linux and other bundled SQLite versions are
covered by the CI matrix when it runs.
