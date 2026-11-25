# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
# SPDX-License-Identifier: AGPL-3.0-only
"""Indexed RAW/JPEG pairing across the catalogue, independent of folders."""

import os
import unicodedata

JPEG_EXT = {".jpg", ".jpeg"}
RAW_EXT = {
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf",
    ".rw2", ".raf", ".dng", ".pef", ".ptx", ".raw", ".rwl", ".3fr", ".fff",
    ".iiq", ".erf", ".mos", ".mrw", ".x3f", ".srw", ".kdc", ".dcr", ".mef",
}


def stem(filename):
    # SQLite lower() cannot fold Cyrillic. Store the same Unicode key on scan
    # and migration, so indexed equality also works for non-Latin filenames.
    return unicodedata.normalize("NFC", os.path.splitext(filename or "")[0].casefold()) or None


def files_for(con, photo_ids):
    """Companions for a batch of visible JPEGs, also used to validate file actions."""
    if not photo_ids:
        return {}
    rows = con.execute(
        "SELECT jpeg.id, raw.id, raw.path FROM visible_photos AS jpeg JOIN photos AS raw "
        "ON raw.pair_stem = jpeg.pair_stem AND raw.taken = jpeg.taken "
        f"WHERE jpeg.id IN ({','.join('?' for _ in photo_ids)}) "
        "AND jpeg.has_raw = 1 AND raw.raw_hidden = 1 "
        "ORDER BY raw.path COLLATE NOCASE, raw.path, raw.id", list(photo_ids))
    result = {}
    for photo_id, raw_id, path in rows:
        result.setdefault(photo_id, []).append({"id": raw_id, "path": path})
    return result


def pair_values_sql(track_missing=True):
    raw = ",".join(repr(ext[1:]) for ext in sorted(RAW_EXT))
    jpeg = ",".join(repr(ext[1:]) for ext in sorted(JPEG_EXT))
    present = "missing = 0 AND " if track_missing else ""
    raw_present = "AND paired_raw.missing = 0" if track_missing else ""
    jpeg_present = "AND paired_jpeg.missing = 0" if track_missing else ""
    has_raw = f"""CASE WHEN {present}ext IN ({jpeg}) AND date_src = 'exif' THEN
                EXISTS (SELECT 1 FROM photos AS paired_raw
                        WHERE paired_raw.pair_stem = photos.pair_stem
                          AND paired_raw.taken = photos.taken
                          AND paired_raw.date_src = 'exif'
                          AND paired_raw.ext IN ({raw}) {raw_present})
                ELSE 0 END"""
    hidden = f"""CASE WHEN {present}ext IN ({raw}) AND date_src = 'exif' THEN
                EXISTS (SELECT 1 FROM photos AS paired_jpeg
                        WHERE paired_jpeg.pair_stem = photos.pair_stem
                          AND paired_jpeg.taken = photos.taken
                          AND paired_jpeg.date_src = 'exif'
                          AND paired_jpeg.ext IN ({jpeg}) {jpeg_present})
                ELSE 0 END"""
    return f"has_raw = {has_raw}, raw_hidden = {hidden}"


def install(con, track_missing=False):
    # Cache only the two display flags. Repeating indexed EXISTS lookups for
    # every chart cost 11 seconds on 100k pairs; the underlying rows stay intact.
    values = pair_values_sql(track_missing)
    con.execute(f"UPDATE photos SET {values}")
    con.execute("CREATE VIEW visible_photos AS SELECT * FROM photos WHERE raw_hidden = 0")
    for event, sides in (("INSERT", ("NEW",)), ("DELETE", ("OLD",)),
                         ("UPDATE", ("OLD", "NEW"))):
        watched = ("pair_stem", "taken", "date_src", "ext")
        if track_missing:
            watched += ("missing",)
        watch = " OF " + ", ".join(watched) if event == "UPDATE" else ""
        when = (" WHEN " + " OR ".join(f"OLD.{key} IS NOT NEW.{key}"
                for key in watched) if event == "UPDATE" else "")
        groups = [f"(pair_stem = {side}.pair_stem AND taken = {side}.taken)" for side in sides]
        if event != "DELETE":
            # Changing a key to NULL must also clear the changed row's flags.
            groups.append("id = NEW.id")
        con.execute(f"CREATE TRIGGER raw_pairs_{event.lower()} AFTER {event}{watch} ON photos{when} "
                    f"BEGIN UPDATE photos SET {values} WHERE {' OR '.join(groups)}; END")
