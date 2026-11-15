# -*- coding: utf-8 -*-
# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License, version 3, as published by
# the Free Software Foundation. See LICENSE for the full text.
# Commercial licensing is available — see COMMERCIAL.md.
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared, transactional schema migrations and planner maintenance."""

import json
import logging
import math
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import dupkey

BUSY_MS = 15000
SCHEMA_VERSION = 4


PHOTOS_SQL = """CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    folder      TEXT,
    filename    TEXT,
    ext         TEXT,
    size        INTEGER,
    mtime       REAL,
    taken       TEXT,      -- 'YYYY-MM-DD HH:MM:SS'
    date_src    TEXT,      -- exif | file
    year        INTEGER,
    month       INTEGER,
    ym          TEXT,      -- 'YYYY-MM'
    day         INTEGER,
    hour        INTEGER,
    weekday     INTEGER,   -- 0 = Monday
    brand       TEXT,
    camera      TEXT,
    lens        TEXT,
    focal       REAL,
    focal35     REAL,
    iso         INTEGER,
    fnumber     REAL,
    shutter     TEXT,      -- for display: '1/125'
    exposure    REAL,      -- the same in seconds, for the distribution
    width       INTEGER,
    height      INTEGER,
    lat         REAL,      -- coordinates from EXIF, signed degrees
    lon         REAL,
    rating      INTEGER,   -- stars from EXIF, 1…5; NULL = the file has none
    -- Your own mark, set in the interface: '1'…'5', 'bad' or 'great'. Deliberately
    -- not in COLUMNS, so that re-reading EXIF cannot overwrite it — the scanner
    -- owns every other column here, and this is the one column it must not touch.
    mark        TEXT,
    -- Your own note on the photo, up to 256 characters, set in the interface.
    -- Kept out of COLUMNS for the reason `mark` is.
    comment     TEXT,
    sig         TEXT,      -- content signature; NULL = the pass was never run
    color       TEXT,      -- dominant colour group; NULL = not analysed yet
    color_hex   TEXT,      -- the average shade of that group, for the swatch
    color_share REAL,      -- what share of the frame it holds
    color_center       TEXT,   -- the same over the middle half of the frame area
    color_center_hex   TEXT,
    color_center_share REAL,
    brightness  REAL,      -- average brightness, 0…1
    contrast    REAL,      -- the spread of brightness
    chroma      REAL,      -- share of coloured pixels; near zero means a black and white frame
    clip_hi     REAL,      -- share of pixels blown out to white
    clip_lo     REAL,      -- share of pixels crushed to black
    -- Sharpness of the centre square and of the rest of the frame: the RMS of the
    -- Laplacian at 1500 px, in the sharpest tenth of the tiles (see sharpness()).
    sharp_center REAL,
    sharp_edge   REAL,
    sharp_v      INTEGER   -- the SHARP_VERSION it was measured under; NULL = never
);"""

PHOTO_COLUMNS = (
    ('id', 'INTEGER'), ('path', 'TEXT'), ('folder', 'TEXT'),
    ('filename', 'TEXT'), ('ext', 'TEXT'), ('size', 'INTEGER'),
    ('mtime', 'REAL'), ('taken', 'TEXT'), ('date_src', 'TEXT'),
    ('year', 'INTEGER'), ('month', 'INTEGER'), ('ym', 'TEXT'),
    ('day', 'INTEGER'), ('hour', 'INTEGER'), ('weekday', 'INTEGER'),
    ('brand', 'TEXT'), ('camera', 'TEXT'), ('lens', 'TEXT'),
    ('focal', 'REAL'), ('focal35', 'REAL'), ('iso', 'INTEGER'),
    ('fnumber', 'REAL'), ('shutter', 'TEXT'), ('exposure', 'REAL'),
    ('width', 'INTEGER'), ('height', 'INTEGER'), ('lat', 'REAL'),
    ('lon', 'REAL'), ('rating', 'INTEGER'), ('mark', 'TEXT'),
    ('comment', 'TEXT'), ('sig', 'TEXT'), ('color', 'TEXT'),
    ('color_hex', 'TEXT'), ('color_share', 'REAL'), ('color_center', 'TEXT'),
    ('color_center_hex', 'TEXT'), ('color_center_share', 'REAL'), ('brightness', 'REAL'),
    ('contrast', 'REAL'), ('chroma', 'REAL'), ('clip_hi', 'REAL'),
    ('clip_lo', 'REAL'), ('sharp_center', 'REAL'), ('sharp_edge', 'REAL'),
    ('sharp_v', 'INTEGER'),
)

JOBS_SQL = """CREATE TABLE IF NOT EXISTS jobs (
    id       INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL,      -- a key of KINDS
    state    TEXT NOT NULL,      -- running | done | stopped | failed
    phase    TEXT,               -- what a running pass is busy with, in words
    done     INTEGER NOT NULL DEFAULT 0,
    total    INTEGER,            -- NULL while it is not known yet
    started  REAL NOT NULL,      -- Unix time, so the server can measure silence
    beat     REAL NOT NULL,
    finished REAL,
    note     TEXT                -- the closing word: an error, "nothing to do"
)"""

TAG_SQL = [
    """CREATE TABLE IF NOT EXISTS photo_tags (
           photo_id INTEGER NOT NULL,
           tag      TEXT NOT NULL,
           PRIMARY KEY (photo_id, tag)
       )""",
    """CREATE INDEX IF NOT EXISTS ix_tag ON photo_tags(tag, photo_id)""",
    """CREATE TRIGGER IF NOT EXISTS photo_tags_gone
       AFTER DELETE ON photos BEGIN
           DELETE FROM photo_tags WHERE photo_id = OLD.id;
       END""",
]

COLOR_RENAME = {
    'красный': 'red',
    'оранжевый': 'orange',
    'коричневый': 'brown',
    'жёлтый': 'yellow',
    'зелёный': 'green',
    'голубой': 'cyan',
    'синий': 'blue',
    'фиолетовый': 'violet',
    'белый': 'white',
    'серый': 'gray',
    'чёрный': 'black',
    'не определён': 'unknown',
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rating_key():
    return "COALESCE(mark, CAST(rating AS TEXT))"


def tune(con):
    con.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
    try:
        con.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        pass  # Some network drives support only the rollback journal.
    return con


def connect(path, **kwargs):
    return tune(sqlite3.connect(path, timeout=BUSY_MS / 1000, **kwargs))


def shutter_seconds(txt):
    if not txt:
        return None
    value = str(txt).strip().rstrip("sс")
    try:
        if "/" in value:
            numerator, denominator = value.split("/")
            return float(numerator) / float(denominator)
        return float(value)
    except (ValueError, ZeroDivisionError):
        return None


def _get(con, key, default=None):
    row = con.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row[0] if row else default


def _set(con, key, value):
    con.execute("INSERT INTO meta(k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, str(value)))


def _tables(con):
    con.execute(PHOTOS_SQL)
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    columns = {row[1] for row in con.execute("PRAGMA table_info(photos)")}
    for name, kind in PHOTO_COLUMNS:
        if name not in columns:
            if name in ("id", "path"):
                raise RuntimeError("Unrecognised photos table: id and path are required")
            con.execute(f"ALTER TABLE photos ADD COLUMN {name} {kind}")
    if "exposure" not in columns:
        # The whole batch rolls back on interruption, including the new column.
        # A retry repeats the backfill; fetchmany bounds memory on large archives.
        cursor = con.execute("SELECT id, shutter FROM photos WHERE shutter IS NOT NULL")
        while rows := cursor.fetchmany(1000):
            con.executemany("UPDATE photos SET exposure = ? WHERE id = ?",
                            [(shutter_seconds(txt), pid) for pid, txt in rows])
    con.execute(JOBS_SQL)
    for stmt in TAG_SQL:
        con.execute(stmt)


def managed_indexes():
    columns = ("year", "ym", "month", "hour", "brand", "camera", "lens", "focal",
               "iso", "color", "sig", "taken", "focal35", "fnumber", "exposure",
               "brightness", "contrast", "chroma", "sharp_center", "sharp_edge")
    expressions = {f"ix_{column}": column for column in columns}
    expressions.update(ix_geo="lat, lon", ix_dupkey=dupkey.name_key(),
                       ix_rating=rating_key())
    return {name: f"CREATE INDEX {name} ON photos({expression})"
            for name, expression in expressions.items()}


def _normal_sql(sql):
    # Ignore formatting outside quoted text, preserving expression literals.
    tokens = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\S", sql)
    return "".join(t if t.startswith(("'", '"')) else t.lower() for t in tokens)


def _index_changes(con):
    actual = dict(con.execute("SELECT name, sql FROM sqlite_schema WHERE type = 'index'"))
    expected = managed_indexes()
    changed = [name for name, sql in expected.items()
               if _normal_sql(actual.get(name) or "") != _normal_sql(sql)]
    return changed, "ix_dup" in actual


def _indexes(con):
    changed, obsolete = _index_changes(con)
    if obsolete:
        con.execute("DROP INDEX ix_dup")
    for name in changed:
        con.execute(f"DROP INDEX IF EXISTS {name}")
        con.execute(managed_indexes()[name])
    if changed or obsolete:
        _set(con, "planner_pending", "indexes")
        _set(con, "schema_indexes_changed_at", utc_now())
    return changed


def _revision(con):
    con.execute("CREATE TABLE db_state (id INTEGER PRIMARY KEY CHECK(id = 1), "
                "revision INTEGER NOT NULL, photo_changes INTEGER NOT NULL)")
    con.execute("INSERT INTO db_state VALUES (1, 0, 0)")
    for table in ("photos", "photo_tags"):
        for event in ("INSERT", "UPDATE", "DELETE"):
            # Count real changes: an idempotent rescan must not invalidate caches.
            names = ([name for name, _ in PHOTO_COLUMNS] if table == "photos"
                     else ["photo_id", "tag"])
            when = (" WHEN " + " OR ".join(f"OLD.{n} IS NOT NEW.{n}" for n in names)
                    if event == "UPDATE" else "")
            con.execute(f"CREATE TRIGGER revision_{table}_{event.lower()} AFTER {event} "
                        f"ON {table}{when} BEGIN UPDATE db_state SET revision = revision + 1"
                        + (", photo_changes = photo_changes + 1" if table == "photos" else "")
                        + " WHERE id = 1; END")
    # The signature mode changes the duplicate rule even before a photo is written.
    for event, key in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        condition = f"{key}.k IN ('sig_mode', 'color_algo', 'exif_version')"
        if event == "UPDATE":
            condition = (f"({condition} OR OLD.k IN ('sig_mode', 'color_algo', 'exif_version'))"
                         " AND (OLD.k IS NOT NEW.k OR OLD.v IS NOT NEW.v)")
        con.execute(f"CREATE TRIGGER revision_meta_{event.lower()} AFTER {event} ON meta "
                    f"WHEN {condition} BEGIN UPDATE db_state SET revision = revision + 1 "
                    "WHERE id = 1; END")


def _colors(con):
    for src, dst in COLOR_RENAME.items():
        for column in ("color", "color_center"):
            con.execute(f"UPDATE photos SET {column} = ? WHERE {column} = ?", (dst, src))


MIGRATIONS = ((1, "adopt legacy tables and backfill exposure", _tables),
              (2, "canonical indexes", _indexes),
              (3, "transactional data revision", _revision),
              (4, "normalise legacy colour names", _colors))


def migration_backup(con):
    """Copy the committed pre-migration snapshot while BEGIN IMMEDIATE excludes writers.

    A separate reader avoids backing up a connection with a write transaction,
    which would wait for itself. No schema statements may precede this call.
    """
    path = next(row[2] for row in con.execute("PRAGMA database_list") if row[1] == "main")
    if not path:  # In-memory fixtures have no persistent data to recover.
        return None
    source = Path(path).resolve()
    folder = source.parent / (source.name + ".backups")
    folder.mkdir(exist_ok=True)
    target = folder / f"schema-{con.execute('PRAGMA user_version').fetchone()[0]}-{uuid.uuid4().hex}.db"
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(target)) as backup:
                reader.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("The migration backup failed its integrity check")
        return str(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def ensure_schema(con):
    """Serialise every entry point; DDL, backfills and version records commit together."""
    if con.in_transaction:
        raise RuntimeError("Schema migration requires a connection with no pending writes")
    con.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
    con.execute("BEGIN IMMEDIATE")
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema {version} is newer than supported {SCHEMA_VERSION}")
        changed, obsolete = _index_changes(con)
        has_photos = con.execute("SELECT 1 FROM sqlite_schema WHERE name = 'photos' "
                                 "AND type = 'table'").fetchone()
        backup = None
        if (version < SCHEMA_VERSION or changed or obsolete) and has_photos:
            if con.execute("SELECT 1 FROM photos LIMIT 1").fetchone():
                backup = migration_backup(con)
        con.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                    "applied_at TEXT NOT NULL, result TEXT NOT NULL)")
        applied = dict(con.execute("SELECT version, result FROM schema_migrations"))
        if any(applied.get(number) != "ok" for number in range(1, version + 1)):
            raise RuntimeError("Schema version and migration history disagree")
        for number, name, migrate in MIGRATIONS:
            if number > version:
                migrate(con)
                con.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, 'ok')",
                            (number, name, utc_now()))
                con.execute(f"PRAGMA user_version = {number}")
        # Check expressions as well as names, including databases from builds
        # that changed a shared expression without incrementing the schema version.
        _indexes(con)
        if backup:
            _set(con, "schema_backup", backup)
        con.commit()
    except BaseException:
        con.rollback()
        raise


def maintain_statistics(con, force=False):
    """Run only at writer/startup boundaries, never from an HTTP GET.

    A full, table-scoped ANALYZE is deliberate: optimize may skip a 10% change
    or rating-only updates, and approximate analysis gave poor date/rating plans
    on older SQLite. Newer SQLite also gets its bounded optimize for other tables.
    https://www.sqlite.org/lang_analyze.html
    """
    if con.in_transaction:
        raise RuntimeError("Planner maintenance requires committed application writes")
    con.execute("BEGIN IMMEDIATE")
    try:
        count = con.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        changes = con.execute("SELECT photo_changes FROM db_state WHERE id = 1").fetchone()[0]
        previous = json.loads(_get(con, "planner_maintenance", "{}"))
        has_stat = con.execute("SELECT 1 FROM sqlite_schema WHERE name = 'sqlite_stat1'").fetchone()
        has_stat = has_stat and con.execute("SELECT 1 FROM sqlite_stat1 WHERE tbl = 'photos'").fetchone()
        threshold = max(1000, math.ceil(previous.get("rows", 0) * 0.1))
        reason = ("manual" if force else _get(con, "planner_pending") or
                  ("missing statistics" if count and not has_stat else None) or
                  ("archive changed" if changes - previous.get("photo_changes", 0) >= threshold else None))
        if not reason or not count:
            con.rollback()
            return None
        started = time.perf_counter()
        limit = con.execute("PRAGMA analysis_limit").fetchone()[0]
        try:
            con.execute("PRAGMA analysis_limit = 0")
            con.execute("ANALYZE photos")
            con.execute("ANALYZE photo_tags")
        finally:
            con.execute(f"PRAGMA analysis_limit = {limit}")
        method = "ANALYZE photos; ANALYZE photo_tags"
        if sqlite3.sqlite_version_info >= (3, 46, 0):
            con.execute("PRAGMA optimize = 0x10002").fetchall()
            method += "; PRAGMA optimize=0x10002"
        result = {"at": utc_now(), "sqlite_version": sqlite3.sqlite_version,
                  "schema_version": SCHEMA_VERSION, "method": method,
                  "reason": reason, "rows": count, "photo_changes": changes,
                  "duration_ms": round((time.perf_counter() - started) * 1000, 3), "result": "ok"}
        _set(con, "planner_maintenance", json.dumps(result))
        con.execute("DELETE FROM meta WHERE k IN ('planner_pending', 'planner_error')")
        con.commit()
        logging.info("SQLite planner maintenance: %s", result)
        return result
    except sqlite3.Error as exc:
        con.rollback()
        with con:
            _set(con, "planner_error", json.dumps({"at": utc_now(), "error": str(exc)}))
        logging.warning("SQLite planner maintenance failed: %s", exc)
        return {"result": "error", "error": str(exc)}
    except BaseException:
        con.rollback()
        raise


def open_database(path, maintenance=True, **kwargs):
    con = connect(path, **kwargs)
    try:
        ensure_schema(con)
        if maintenance:
            maintain_statistics(con)
        return con
    except BaseException:
        con.close()
        raise


def diagnostics(con):
    """Read-only: safe to poll without creating schema or collecting statistics."""
    state = con.execute("SELECT revision, photo_changes FROM db_state WHERE id = 1").fetchone()
    return {"sqlite_version": sqlite3.sqlite_version,
            "schema_version": con.execute("PRAGMA user_version").fetchone()[0],
            "revision": state[0], "photo_changes": state[1],
            "maintenance": json.loads(_get(con, "planner_maintenance", "null")),
            "maintenance_error": json.loads(_get(con, "planner_error", "null")),
            "maintenance_pending": _get(con, "planner_pending"),
            "migrations": [dict(zip(("version", "name", "applied_at", "result"), row))
                           for row in con.execute("SELECT * FROM schema_migrations ORDER BY version")]}
