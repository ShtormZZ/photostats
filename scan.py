# -*- coding: utf-8 -*-
# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License, version 3, as published by
# the Free Software Foundation. See LICENSE for the full text.
# Commercial licensing is available — see COMMERCIAL.md.
# SPDX-License-Identifier: AGPL-3.0-only
"""
scan.py — walks folders of photographs, reads their EXIF and puts it all in SQLite.

Running it:
    python scan.py "D:\\Photos" "E:\\Archive 2001-2010"
    python scan.py D:\\Pictures --db photos.db --full

Reads EXIF through ExifTool where it is found — that covers RAW and every
maker-specific tag — and falls back to the exifread library otherwise. A repeat
run only processes files that are new or have changed.
"""

import argparse
import colorsys
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime

import dupkey
import tags

try:
    from version import VERSION
except ImportError:          # the file was copied without version.py
    VERSION = "unknown"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --------------------------------------------------------------------------
# Which files count as photographs
# --------------------------------------------------------------------------

# Exactly two extensions. .jpe and .jfif are JPEG too, but that is how a browser
# saves pictures rather than how a camera names them; anyone who wants them can
# add them with --ext.
JPEG_EXT = {".jpg", ".jpeg"}
RASTER_EXT = JPEG_EXT | {
    ".jpe", ".jfif",        # JPEG as well, but left out by default — see above
    ".png", ".tif", ".tiff", ".heic", ".heif", ".webp", ".avif", ".bmp", ".gif",
}
RAW_EXT = {
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf",
    ".rw2", ".raf", ".dng", ".pef", ".ptx", ".raw", ".rwl", ".3fr", ".fff",
    ".iiq", ".erf", ".mos", ".mrw", ".x3f", ".srw", ".kdc", ".dcr", ".mef",
}
PHOTO_EXT = RASTER_EXT | RAW_EXT          # everything the program can read at all
DEFAULT_EXT = set(JPEG_EXT)               # what is taken unless told otherwise

# Files smaller than this are thumbnails, avatars and pictures saved out of chat
# apps. In the statistics they only get in the way: they drag down the size
# distribution and invent duplicates, since identical thumbnails are everywhere.
MIN_SIZE = 50 * 1024

SKIP_DIRS = {
    ".git", "$recycle.bin", "system volume information", "__pycache__",
    ".thumbnails", ".picasaoriginals", "lightroom previews.lrdata",
}

# --------------------------------------------------------------------------
# Normalising brands and models
# --------------------------------------------------------------------------

BRAND_MAP = {
    "nikon corporation": "Nikon", "nikon": "Nikon",
    "canon": "Canon",
    "sony": "Sony", "sony corporation": "Sony",
    "olympus imaging corp.": "Olympus", "olympus corporation": "Olympus",
    "olympus optical co.,ltd": "Olympus", "olympus": "Olympus",
    "om digital solutions": "OM System",
    "panasonic": "Panasonic",
    "matsushita electric industrial co.,ltd.": "Panasonic",
    "fujifilm": "Fujifilm", "fujifilm corporation": "Fujifilm", "fuji photo film co., ltd.": "Fujifilm",
    "pentax": "Pentax", "pentax corporation": "Pentax", "asahi optical co.,ltd": "Pentax",
    "ricoh": "Ricoh", "ricoh imaging company, ltd.": "Ricoh",
    "apple": "Apple", "apple computer, inc.": "Apple",
    "samsung": "Samsung", "samsung techwin": "Samsung", "samsung electronics": "Samsung",
    "xiaomi": "Xiaomi", "huawei": "Huawei", "google": "Google",
    "oneplus": "OnePlus", "oppo": "OPPO", "vivo": "vivo",
    "motorola": "Motorola", "nokia": "Nokia", "htc": "HTC",
    "lg electronics": "LG", "lge": "LG",
    "leica camera ag": "Leica", "leica": "Leica",
    "sigma": "Sigma", "sigma corporation": "Sigma",
    "casio": "Casio", "casio computer co.,ltd.": "Casio",
    "eastman kodak company": "Kodak", "kodak": "Kodak",
    "minolta co., ltd.": "Minolta", "konica minolta": "Konica Minolta",
    "konica minolta camera, inc.": "Konica Minolta",
    "hewlett-packard": "HP", "hp": "HP",
    "dji": "DJI", "gopro": "GoPro", "hasselblad": "Hasselblad",
    "phase one": "Phase One", "epson": "Epson", "sanyo electric co.,ltd.": "Sanyo",
    "seiko epson corp.": "Epson", "zenit": "Zenit",
}

_BRAND_TAIL = re.compile(
    r"\s*(corporation|corp\.?|co\.?,?\s*ltd\.?|company|inc\.?|imaging|"
    r"electronics|optical|camera|ag|gmbh)\.?$", re.I)


def norm_brand(make):
    if not make:
        return None
    s = " ".join(str(make).split()).strip(" .,")
    key = s.lower()
    if key in BRAND_MAP:
        return BRAND_MAP[key]
    prev = None
    while prev != s:
        prev = s
        s = _BRAND_TAIL.sub("", s).strip(" .,")
        if s.lower() in BRAND_MAP:
            return BRAND_MAP[s.lower()]
    if not s:
        return None
    return s if (s.isupper() and len(s) <= 4) else s.title()


def norm_model(brand, model):
    """The full camera name without repeating the brand: 'Canon' + 'Canon EOS 20D'."""
    if not model:
        return None
    m = " ".join(str(model).split()).strip(" .,")
    if not m:
        return None
    if brand:
        if m.lower().startswith(brand.lower()):
            m = brand + m[len(brand):]          # one spelling of the brand
        elif m.split()[0].lower() not in brand.lower():
            m = f"{brand} {m}"
    return m


def norm_lens(*candidates):
    for c in candidates:
        if c is None:
            continue
        s = " ".join(str(c).split()).strip(" .,")
        if not s or s.lower() in ("unknown", "n/a", "----", "0", "----."):
            continue
        if re.fullmatch(r"[\d.\s]+", s):          # junk of the "0 0 0 0" kind
            continue
        return s
    return None


# --------------------------------------------------------------------------
# The database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
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
    clip_lo     REAL       -- share of pixels crushed to black
);
CREATE INDEX IF NOT EXISTS ix_year   ON photos(year);
CREATE INDEX IF NOT EXISTS ix_ym     ON photos(ym);
CREATE INDEX IF NOT EXISTS ix_month  ON photos(month);
CREATE INDEX IF NOT EXISTS ix_hour   ON photos(hour);
CREATE INDEX IF NOT EXISTS ix_brand  ON photos(brand);
CREATE INDEX IF NOT EXISTS ix_camera ON photos(camera);
CREATE INDEX IF NOT EXISTS ix_lens   ON photos(lens);
CREATE INDEX IF NOT EXISTS ix_focal  ON photos(focal);
CREATE INDEX IF NOT EXISTS ix_iso    ON photos(iso);
CREATE INDEX IF NOT EXISTS ix_color  ON photos(color);
CREATE INDEX IF NOT EXISTS ix_geo    ON photos(lat, lon);
CREATE INDEX IF NOT EXISTS ix_sig    ON photos(sig);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

COLUMNS = [
    "path", "folder", "filename", "ext", "size", "mtime", "taken", "date_src",
    "year", "month", "ym", "day", "hour", "weekday", "brand", "camera", "lens",
    "focal", "focal35", "iso", "fnumber", "shutter", "exposure", "width", "height",
    "lat", "lon", "rating",
]


# How long to wait for the database to free up. The scanner and the server run at
# the same time, and without waiting nearly half the queries fail with "database
# is locked": over a measured three seconds of concurrent work that was 891 read
# errors and 513 write errors.
BUSY_MS = 15000


def tune(con):
    """The modes without which the scanner and the server cannot run together."""
    con.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
    # WAL lets a reader and a writer through at once and gives half again as many
    # reads. On network drives it is unavailable — the ordinary journal then stays
    # in place silently, and only the lock wait above saves the situation.
    try:
        con.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        pass
    return con


def open_db(path):
    """Tables, then the migration, and only then the indexes: an index on a new
    column cannot be created before the migration adds it to a database that
    already exists."""
    con = tune(sqlite3.connect(path, timeout=BUSY_MS / 1000))
    # comments are stripped before parsing: they contain semicolons
    clean = "\n".join(re.sub(r"--.*$", "", ln) for ln in SCHEMA.splitlines())
    stmts = [x.strip() for x in clean.split(";") if x.strip()]
    for st in stmts:
        if not st.upper().startswith("CREATE INDEX"):
            con.execute(st)
    migrate(con)
    for st in stmts:
        if st.upper().startswith("CREATE INDEX"):
            con.execute(st)
    con.commit()
    # Tags live in a table of their own and are not part of SCHEMA above: the
    # statements there are split on semicolons, and a trigger body holds one.
    # The scanner never writes a tag — it needs the table only so that the
    # trigger is in place when rows for vanished files are dropped.
    tags.ensure_schema(con)
    return con


def migrate(con):
    """Builds up databases made by earlier versions of the program."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(photos)")}
    for name, decl in (("lat", "REAL"), ("lon", "REAL"), ("sig", "TEXT"),
                       ("rating", "INTEGER"), ("mark", "TEXT"),
                       ("color", "TEXT"), ("color_hex", "TEXT"),
                       ("color_share", "REAL"), ("color_center", "TEXT"),
                       ("color_center_hex", "TEXT"),
                       ("color_center_share", "REAL"), ("brightness", "REAL"),
                       ("contrast", "REAL"), ("chroma", "REAL"),
                       ("clip_hi", "REAL"), ("clip_lo", "REAL")):
        if name not in cols:
            con.execute(f"ALTER TABLE photos ADD COLUMN {name} {decl}")
            con.commit()
    if "exposure" not in cols:
        con.execute("ALTER TABLE photos ADD COLUMN exposure REAL")
        rows = con.execute("SELECT id, shutter FROM photos "
                           "WHERE shutter IS NOT NULL").fetchall()
        upd = [(shutter_seconds(t), i) for i, t in rows]
        con.executemany("UPDATE photos SET exposure = ? WHERE id = ?",
                        [u for u in upd if u[0]])
        con.commit()
        print(f"Database upgraded: shutter speed converted to seconds "
              f"({len(upd)} rows).")


# --------------------------------------------------------------------------
# Parsing values
# --------------------------------------------------------------------------

_DT_RE = re.compile(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def parse_dt(value):
    if not value:
        return None
    m = _DT_RE.search(str(value))
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    if not (1970 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 and h < 24):
        return None
    try:
        return datetime(y, mo, d, h, mi, min(s, 59))
    except ValueError:
        return None


def to_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) or None
    num, den = getattr(v, "numerator", None), getattr(v, "denominator", None)
    if num is not None and den:
        return float(num) / float(den) or None
    if isinstance(v, (tuple, list)) and len(v) == 2 and v[1]:
        return float(v[0]) / float(v[1]) or None
    s = str(v).strip()
    m = re.match(r"^\s*([\d.]+)\s*/\s*([\d.]+)\s*$", s)
    if m:
        try:
            den = float(m.group(2))
            return float(m.group(1)) / den if den else None
        except ValueError:
            return None
    m = re.search(r"[\d.]+", s)
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def to_int(v):
    f = to_float(v.split()[0] if isinstance(v, str) and v.split() else v)
    return int(f) if f else None


# Percent values Windows writes alongside the star count, and the star each one
# stands for. Some tools fill in only the percentage.
RATING_PERCENT = ((99, 5), (75, 4), (50, 3), (25, 2), (1, 1))


def rating_stars(tags):
    """Stars from EXIF, 1…5, or None.

    Only the EXIF side is read: Rating (0x4746) is what a camera writes when you
    press the star button, and what Windows Explorer shows. Lightroom keeps its
    stars in xmp:Rating or in its own catalogue, neither of which is EXIF, so a
    file rated only in Lightroom arrives here unrated.

    A Rating of 0 is the tag saying "unrated" in so many words, and is stored as
    NULL like an absent tag: the difference would show up nowhere but would need
    explaining in every panel.
    """
    v = to_int(tags.get("Rating"))
    if v is None:
        pct = to_int(tags.get("RatingPercent"))
        if pct is not None:
            v = next((s for p, s in RATING_PERCENT if pct >= p), 0)
    return v if v and 1 <= v <= 5 else None


def fmt_shutter(sec):
    """0.008 -> '1/125'. For display; the number itself is stored for statistics."""
    if not sec:
        return None
    if sec >= 1:
        return f"{sec:g}s"
    return f"1/{int(round(1 / sec))}"


def shutter_seconds(txt):
    """The reverse: '1/125' -> 0.008. Needed when migrating old databases."""
    if not txt:
        return None
    t = str(txt).strip().rstrip("sс")
    try:
        if "/" in t:
            a, b = t.split("/")
            return float(a) / float(b)
        return float(t)
    except (ValueError, ZeroDivisionError):
        return None


def gps_value(raw, ref):
    """A coordinate from EXIF: either a ready number or degrees-minutes-seconds.
    The hemisphere comes in a tag of its own, where S and W mean a negative
    value."""
    if raw is None:
        return None
    val = None
    if isinstance(raw, (int, float)):
        val = float(raw)
    elif isinstance(raw, (list, tuple)) and len(raw) == 3:
        parts = [to_float(x) or 0 for x in raw]
        val = parts[0] + parts[1] / 60 + parts[2] / 3600
    else:
        txt = str(raw).strip().strip("[]")
        if "," in txt:
            parts = [to_float(x) or 0 for x in txt.split(",")]
            if len(parts) == 3:
                val = parts[0] + parts[1] / 60 + parts[2] / 3600
        if val is None:
            val = to_float(txt)
    if val is None:
        return None
    if ref and str(ref).strip().upper().startswith(("S", "W")) and val > 0:
        val = -val
    return val


def make_geo(tags):
    lat = gps_value(tags.get("GPSLatitude"), tags.get("GPSLatitudeRef"))
    lon = gps_value(tags.get("GPSLongitude"), tags.get("GPSLongitudeRef"))
    if lat is None or lon is None:
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:      # null island — almost always junk
        return None, None
    return round(lat, 6), round(lon, 6)


def make_row(path, st, tags):
    """tags is a dictionary of EXIF fields, already made uniform."""
    dt = None
    src = "exif"
    for k in ("DateTimeOriginal", "CreateDate", "DateTimeDigitized", "ModifyDate"):
        dt = parse_dt(tags.get(k))
        if dt:
            break
    if not dt:
        dt = datetime.fromtimestamp(st.st_mtime)
        src = "file"

    geo = make_geo(tags)
    brand = norm_brand(tags.get("Make"))
    camera = norm_model(brand, tags.get("Model"))
    lens = norm_lens(tags.get("LensModel"), tags.get("LensID"), tags.get("Lens"),
                     tags.get("LensType"), tags.get("LensSpec"))
    focal = to_float(tags.get("FocalLength"))
    focal35 = to_float(tags.get("FocalLengthIn35mmFormat"))

    return {
        "path": path,
        "folder": os.path.dirname(path),
        "filename": os.path.basename(path),
        "ext": os.path.splitext(path)[1].lower().lstrip("."),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "taken": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "date_src": src,
        "year": dt.year,
        "month": dt.month,
        "ym": dt.strftime("%Y-%m"),
        "day": dt.day,
        "hour": dt.hour,
        "weekday": dt.weekday(),
        "brand": brand,
        "camera": camera,
        "lens": lens,
        "focal": focal if focal and 0 < focal < 3000 else None,
        "focal35": focal35 if focal35 and 0 < focal35 < 3000 else None,
        "iso": to_int(tags.get("ISO")),
        "fnumber": to_float(tags.get("FNumber")),
        "shutter": fmt_shutter(to_float(tags.get("ExposureTime"))),
        "exposure": to_float(tags.get("ExposureTime")),
        "width": to_int(tags.get("ImageWidth")),
        "height": to_int(tags.get("ImageHeight")),
        "lat": geo[0],
        "lon": geo[1],
        "rating": rating_stars(tags),
    }


# --------------------------------------------------------------------------
# Reading EXIF — ExifTool
# --------------------------------------------------------------------------

EXIFTOOL_TAGS = [
    "-DateTimeOriginal", "-CreateDate", "-ModifyDate", "-Make", "-Model",
    "-LensModel", "-LensID", "-Lens", "-LensType", "-LensSpec",
    "-FocalLength", "-FocalLengthIn35mmFormat", "-ISO", "-FNumber",
    "-ExposureTime", "-ImageWidth", "-ImageHeight",
    "-GPSLatitude", "-GPSLongitude",
    "-Rating", "-RatingPercent",
]


def find_exiftool():
    """Look in PATH, then next to the script. Returns (path, version)."""
    candidates = []
    for name in ("exiftool", "exiftool.exe"):
        p = shutil.which(name)
        if p:
            candidates.append(p)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [os.path.join(here, "exiftool.exe"),
                   os.path.join(here, "exiftool", "exiftool.exe"),
                   os.path.join(here, "exiftool")]
    for cand in candidates:
        if not os.path.isfile(cand):
            continue
        ver = exiftool_version(cand)
        if ver:
            return cand, ver
        # the exe is there but will not run — nearly always a lost exiftool_files
        near = os.path.join(os.path.dirname(cand), "exiftool_files")
        print(f"  ! {cand} does not run.")
        if not os.path.isdir(near):
            print(f"  ! No exiftool_files folder next to it — move that folder "
                  f"together with the exe, from the same archive.")
    return None, None


def exiftool_version(exe):
    try:
        out = subprocess.run([exe, "-ver"], capture_output=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ver = out.stdout.decode("utf-8", "replace").strip()
        return ver if re.fullmatch(r"[\d.]+", ver) else None
    except Exception:
        return None


def read_chunk_exiftool(exe, paths):
    fd, arg = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(paths))
        cmd = [exe, "-json", "-n", "-q", "-q", "-m", "-fast2",
               "-charset", "filename=UTF8", *EXIFTOOL_TAGS, "-@", arg]
        out = subprocess.run(cmd, capture_output=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = out.stdout.decode("utf-8", "replace").strip()
        if not text:
            return {}
        data = json.loads(text)
        return {os.path.normpath(d.get("SourceFile", "")): d for d in data}
    except Exception as e:
        print(f"  ! ExifTool: {e}", flush=True)
        return {}
    finally:
        try:
            os.unlink(arg)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Reading EXIF — the fallback without ExifTool
# --------------------------------------------------------------------------

BASE_IFD = {0x010F: "Make", 0x0110: "Model", 0x0132: "ModifyDate",
            0x4746: "Rating", 0x4749: "RatingPercent"}
GPS_IFD = {1: "GPSLatitudeRef", 2: "GPSLatitude",
           3: "GPSLongitudeRef", 4: "GPSLongitude"}
EXIF_IFD = {
    0x9003: "DateTimeOriginal", 0x9004: "CreateDate", 0x920A: "FocalLength",
    0xA405: "FocalLengthIn35mmFormat", 0x8827: "ISO", 0x829D: "FNumber",
    0x829A: "ExposureTime", 0xA002: "ImageWidth", 0xA003: "ImageHeight",
    0xA434: "LensModel", 0xA432: "LensSpec", 0xA433: "Lens",
}


def read_one_fallback(path):
    tags = {}
    try:
        import exifread
        with open(path, "rb") as f:
            raw = exifread.process_file(f, details=False)
        get = lambda k: str(raw[k]) if k in raw else None
        tags = {
            "DateTimeOriginal": get("EXIF DateTimeOriginal"),
            "CreateDate": get("EXIF DateTimeDigitized"),
            "ModifyDate": get("Image DateTime"),
            "Make": get("Image Make"),
            "Model": get("Image Model"),
            "LensModel": get("EXIF LensModel") or get("MakerNote LensModel"),
            "LensID": get("MakerNote LensType") or get("MakerNote Lens"),
            "FocalLength": get("EXIF FocalLength"),
            "FocalLengthIn35mmFormat": get("EXIF FocalLengthIn35mmFilm"),
            "ISO": get("EXIF ISOSpeedRatings"),
            "Rating": get("Image Rating"),
            "RatingPercent": get("Image RatingPercent"),
            "FNumber": get("EXIF FNumber"),
            "ExposureTime": get("EXIF ExposureTime"),
            "ImageWidth": get("EXIF ExifImageWidth"),
            "ImageHeight": get("EXIF ExifImageLength"),
            "GPSLatitude": get("GPS GPSLatitude"),
            "GPSLatitudeRef": get("GPS GPSLatitudeRef"),
            "GPSLongitude": get("GPS GPSLongitude"),
            "GPSLongitudeRef": get("GPS GPSLongitudeRef"),
        }
    except Exception:
        pass
    if not tags.get("DateTimeOriginal"):
        try:
            from PIL import Image
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except Exception:
                pass
            with Image.open(path) as im:
                tags.setdefault("ImageWidth", im.width)
                tags.setdefault("ImageHeight", im.height)
                ex = im.getexif()
                for tid, name in BASE_IFD.items():
                    if ex.get(tid) is not None:
                        tags[name] = ex.get(tid)
                sub = ex.get_ifd(0x8769) or {}
                for tid, name in EXIF_IFD.items():
                    if sub.get(tid) is not None:
                        tags[name] = sub.get(tid)
                gps = ex.get_ifd(0x8825) or {}
                for tid, name in GPS_IFD.items():
                    if gps.get(tid) is not None:
                        tags[name] = gps.get(tid)
        except Exception:
            pass
    return {k: v for k, v in tags.items() if v is not None}


# --------------------------------------------------------------------------
# The content signature
# --------------------------------------------------------------------------

SIG_HEAD = 64 * 1024        # how many bytes are taken from the start and the end of a file


def file_sig(job):
    """The signature of a file: its size plus the start and end of its content.

    There is no point reading the whole file. For a 4.5 MB photo, matching size
    and matching first and last 64 kilobytes mean matching content — they can
    differ only in a deliberately built file. In exchange, five gigabytes are read
    off the disk instead of a hundred and seventy, and the pass takes minutes
    rather than hours. --hash-full turns on reading everything."""
    pid, path, full = job
    try:
        size = os.path.getsize(path)
        h = hashlib.sha1(str(size).encode())
        with open(path, "rb") as f:
            if full:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                return pid, "f1:" + h.hexdigest()
            h.update(f.read(SIG_HEAD))
            if size > SIG_HEAD * 2:
                f.seek(-SIG_HEAD, os.SEEK_END)
                h.update(f.read(SIG_HEAD))
        return pid, "p1:" + h.hexdigest()
    except OSError:
        return pid, None


def sign_files(con, workers, full=False):
    """Computes signatures where there are none yet. Stops and resumes."""
    mode = "full" if full else "partial"
    row = con.execute("SELECT v FROM meta WHERE k = 'sig_mode'").fetchone()
    if row and row[0] != mode:
        print(f"Signature mode changed ({row[0]} -> {mode}) — recomputing all.")
        con.execute("UPDATE photos SET sig = NULL")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('sig_mode', ?)", (mode,))
    con.commit()

    rows = con.execute("SELECT id, path FROM photos WHERE sig IS NULL").fetchall()
    if not rows:
        print("Signatures already computed for every photo.")
        return
    jobs = [(pid, path, full) for pid, path in rows]
    print(f"Signing files: {len(jobs)}, {'whole file' if full else 'head and tail'}.")
    print("Ctrl+C stops it; whatever is done is kept.", flush=True)

    started, done, buf = time.time(), 0, []
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for pid, sig in pool.map(file_sig, jobs):
            buf.append((sig, pid))
            done += 1
            if len(buf) >= 400:
                con.executemany("UPDATE photos SET sig = ? WHERE id = ?", buf)
                con.commit()
                buf = []
            if done % 200 == 0 or done == len(jobs):
                el = time.time() - started
                speed = done / max(el, 0.001)
                left = (len(jobs) - done) / max(speed, 0.001)
                print(f"\r  {done}/{len(jobs)} ({100 * done / len(jobs):5.1f}%)  "
                      f"{speed:.0f} files/s  about {left / 60:.0f} min left    ",
                      end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        pool.shutdown(wait=False)
        if buf:
            con.executemany("UPDATE photos SET sig = ? WHERE id = ?", buf)
            con.commit()
    print()
    bad, = con.execute("SELECT COUNT(*) FROM photos WHERE sig IS NULL").fetchone()
    if bad:
        print(f"{bad} files could not be read; duplicates fall back to "
              f"name, size and capture time until they are.")


# --------------------------------------------------------------------------
# The dominant colour of a frame
# --------------------------------------------------------------------------

# Hue groups round the circle, in degrees: (upper bound, name).
# Violet was added to the nine listed: between blue and red there would otherwise
# be a gap, and lilac and a sunset sky would be assigned at random.
# To go back to nine groups, replace "violet" with "blue".
# The orange/yellow boundary is at 40°, not the 45° the wheel would suggest. A
# sunlit autumn crown measures 35 to 50° at a purity near 1.0, and the eye calls
# all of it yellow; at 45° the larger half of such a frame fell on the orange
# side and a golden maple came out orange. Below 40° are the hues nobody calls
# yellow — skin, terracotta, a ripe orange — so they keep their name.
HUE_GROUPS = [(15, "red"), (40, "orange"), (70, "yellow"),
              (165, "green"), (215, "cyan"), (260, "blue"),
              (335, "violet")]
CHROMATIC = {"red", "orange", "brown", "yellow", "green",
             "cyan", "blue", "violet"}

# Brown, orange and yellow are one warm family: what splits them from each other
# is not hue but brightness and purity. When the dominant colour is chosen they
# count together, or an autumn frame loses to a solid green although there is more
# warm in it: the leaves scatter into 14% orange, 12% yellow and 8% brown against
# 30% moss. The winning family is named after its largest group.
WARM = {"brown", "orange", "yellow"}

# The same happens at any hue boundary the subject runs across: a deep sky graded
# from 206 to 219° splits into cyan and blue and loses to grass, although there is
# twice as much sky in the frame. So two neighbouring groups count as one colour
# when their pixels form a continuous band — that is, when there is something on
# both sides of the hue boundary. Greenery and sky, far apart, do not merge this
# way: the boundary between them is empty.
# The boundaries around green are deliberately left out. They are exactly where
# one subject usually ends and another begins: golden leaves beside moss, tree
# crowns against sky or water. Merging them would erase what the metric is for:
# an autumn frame would come out green again.
NEIGHBOURS = [({"warm"}, {"red"}, 15), ({"cyan"}, {"blue"}, 215),
              ({"blue"}, {"violet"}, 260), ({"violet"}, {"red"}, 335)]
TOUCH_BAND = 18      # how close to the boundary a pixel counts as being on it
TOUCH_MIN = 0.03     # what share of the pair the pixels on each side must make up

GRAY_SAT = 0.10     # below this saturation a pixel counts as achromatic
BLACK_VAL = 0.16    # below this brightness it is black, whatever the hue
DARK_VAL = 0.28     # up to this brightness a colour only counts when its purity is high
DARK_SAT = 0.80     # the purity a dark pixel needs if it is not to become black

# Brown is a warm hue that is either dark or muted. It has no place of its own on
# the circle: chocolate and a bright orange differ in brightness, not in hue, so it
# is picked out from inside the red-to-yellow arc.
# Brown is only the dark warm tone: wood, bark, chocolate, earth. Light muted warm
# tones do not count as brown — a wheat field and sand read to the eye as golden
# rather than brown.
BROWN_HUE = (8, 50)        # the arc of hues where brown is possible, in degrees
# The arc reaches onto the red side of the circle as well: rotting leaves and leaf
# litter lie at 345 to 8°, and without this they landed in red — and dragged the
# name of the whole warm family along with them. Pure dark red occurs there too
# (wine, brick), so a purity ceiling applies over that stretch.
BROWN_RED_LO = 345
BROWN_RED_SAT = 0.45
# A warm hue darker than this is brown: half brightness, a brightest channel
# below 128. Everything lighter is an ochre, and calling ochre brown made brown
# the commonest colour in the archive — 30% of the frames measured here at the
# old 0.62, still 22% at 0.55, and 13% at this line, where it stops crowding
# out everything else. What it gives up goes where the eye puts it: the muted
# light tones to gold, that is yellow, the vivid ones to orange.
#
# Not lower, though. At 0.45 brown is down to 5% and orange inherits the whole
# surplus, tans and all — the dominance moves rather than goes, and a beige at
# 25° called orange is further from the eye than the brown it replaced.
BROWN_VAL = 0.50
BROWN_SAT_MIN = 0.18       # but not nearly grey: a warm grey stays grey

# Gold is the light muted warm tone. Its hue is orange, 30 to 50°, but it does not
# look like a ripe orange: straw, sand and sepia read as yellow. Purity tells them
# apart: for an orange it is close to one, for a field half that.
GOLD_HUE = (30, 50)
GOLD_SAT = 0.55            # above this purity the tone stays orange
# The same line as BROWN_VAL, seen from the other side: above it a muted warm
# tone is gold, below it brown. They are written as one value because they are
# one boundary — moved apart, the gap between them would be muted warm pixels
# that are neither, and they would fall through to be named by hue alone.
GOLD_VAL = BROWN_VAL

OLIVE_HUE = (55, 70)       # where yellow can turn out to be olive at all
OLIVE_MAX = 0.30           # below this product yellow reads as green
WHITE_VAL = 0.82
CHROMA_MIN = 0.25   # share of coloured pixels from which a frame counts as colour
CENTER_AREA = 0.50  # what share of the area the centre region of the frame holds
CLIP_HI = 0.98      # brighter than this and the highlights are blown out to white
CLIP_LO = 0.016     # darker than this and the shadows are crushed to black
UNKNOWN = "unknown"

# The version of the analysis rules. When it changes the program re-analyses the
# archive itself: otherwise photos analysed under the earlier rules would silently
# stay in their old groups and the statistics would stop being consistent.
ALGO_VERSION = 13

# The version of the EXIF field set. When it changes the scanner re-reads the
# metadata of every file: otherwise a new field would stay empty for everything
# already in the database. The image analysis is not lost in the process — it sits
# in columns of its own.
EXIF_VERSION = 3


def pixel_group(r, g, b):
    """One pixel, one group.

    Warm tones are split by brightness and purity: the dark ones are brown, the
    light muted ones are gold, that is to say yellow, and the vivid ones stay
    orange. The lower purity threshold keeps a warm grey, which is half a step
    away, from being drawn into brown.

    The rule is stricter for dark pixels. A dark brown (70, 35, 20) formally has a
    reddish hue, but the eye reads it as shadow rather than colour — its purity is
    a little over half. Whereas (57, 4, 3), where red accounts for almost all the
    brightness, stays red, dark though it is. What separates these cases is purity
    exactly, which is why its threshold is raised in the dark zone."""
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if v < BLACK_VAL:
        return "black"
    if s < GRAY_SAT:
        return "white" if v > WHITE_VAL else "gray"
    if v < DARK_VAL and s < DARK_SAT:
        return "black"
    deg = h * 360
    reddish = (deg >= BROWN_RED_LO or deg < BROWN_HUE[0]) and s < BROWN_RED_SAT
    if ((BROWN_HUE[0] <= deg < BROWN_HUE[1] or reddish)
            and s >= BROWN_SAT_MIN and v < BROWN_VAL):
        return "brown"
    if GOLD_HUE[0] <= deg < GOLD_HUE[1] and s < GOLD_SAT and v >= GOLD_VAL:
        return "yellow"
    if OLIVE_HUE[0] <= deg < OLIVE_HUE[1] and s * v < OLIVE_MAX:
        return "green"
    for edge, name in HUE_GROUPS:
        if deg < edge:
            return name
    return "red"                      # the tail of the circle, 335…360


def band_touches(hues, edge, total):
    """A band crosses a hue boundary when there are pixels on both sides of it."""
    lo = sum(n for d, n in hues.items() if 0 < (edge - d) % 360 <= TOUCH_BAND)
    hi = sum(n for d, n in hues.items() if 0 <= (d - edge) % 360 < TOUCH_BAND)
    need = max(1, int(total * TOUCH_MIN))
    return lo >= need and hi >= need


def winner(tally, sums, hues, total):
    """The largest group wins; neighbours merge when the subject runs across a hue
    boundary.

    Achromatic groups compete separately from the coloured ones: if at least a
    quarter of the pixels have colour, a grey sky or a black background stays out
    of the contest — otherwise almost the whole archive would come out grey."""
    if not total:
        return None, None, None, 0.0
    chroma = sum(n for k, n in tally.items() if k in CHROMATIC) / total
    allowed = CHROMATIC if chroma >= CHROMA_MIN else set(tally) - CHROMATIC
    cand = {k: n for k, n in tally.items() if k in allowed} or dict(tally)

    # warm tones count as one group: what splits them is brightness, not hue
    groups = {}
    warm = {k: n for k, n in cand.items() if k in WARM}
    if warm:
        groups["warm"] = set(warm)
    for k in cand:
        if k not in WARM:
            groups[k] = {k}

    size = lambda names: sum(cand[n] for n in names)
    best = max(groups.values(), key=size)

    for a, b, edge in NEIGHBOURS:
        if not (a <= set(groups) and b <= set(groups)):
            continue
        pair = groups[next(iter(a))] | groups[next(iter(b))]
        if size(pair) > size(best) and band_touches(hues, edge, size(pair)):
            best = pair

    n = size(best)
    acc = [0, 0, 0]
    for k in best:
        for i in range(3):
            acc[i] += sums[k][i]
    mean = [c // n for c in acc]
    hexv = "#%02X%02X%02X" % tuple(mean)

    # A merged family is named by its own average colour, not by whichever group
    # in it has the most pixels. Counting decided it before, and a plurality of
    # 43% then named the whole 92%: a golden maple came out brown because its
    # shaded half outnumbered each of the lit halves separately. The average is
    # also the swatch shown beside the name, so the two can no longer contradict
    # each other. The name has to be a group that is really in the frame, or the
    # panel would offer a colour that selects nothing — where the average falls
    # outside the family, counting decides as before.
    win = max(best, key=cand.get)
    if len(best) > 1:
        named = pixel_group(*mean)
        if named in best:
            win = named
    return win, hexv, round(n / total, 3), chroma


def image_stats(im):
    """One pass over the pixels of the scaled copy — the colour and the light.

    Colour is decided by a vote among the pixels. Averaging RGB over the frame is
    not an option: for a varied photo the average always comes out muddy — a
    colour that is nowhere in the frame at all.

    The centre region is counted separately — half the frame area, cut from the
    middle. The dominant colour describes the background first of all, the centre
    one describes what was photographed. Apart they say little; together they let
    you search by subject: a black cat on a white sheet is white dominant and
    black centre.

    Brightness, contrast, the share of coloured pixels and the losses in the
    highlights and shadows are counted along the way: the pixels have been walked
    already, and all of this costs a few additions."""
    # tobytes() gives the same pixels and does not depend on the Pillow version,
    # unlike getdata(), which was deprecated in 13
    raw = im.tobytes()
    w, hgt = im.size
    total = len(raw) // 3
    if not total:
        return None

    # the side of the centre square: half the area means a side smaller by a
    # factor of √2, that is about 0.707 of the frame. The region does not touch
    # the edges.
    side = max(1, round(min(w, hgt) * CENTER_AREA ** 0.5))
    cx0 = (w - side) // 2
    cy0 = (hgt - side) // 2
    cx1, cy1 = cx0 + side, cy0 + side

    tally, sums = Counter(), defaultdict(lambda: [0, 0, 0])
    ctally, csums = Counter(), defaultdict(lambda: [0, 0, 0])
    hues, chues = Counter(), Counter()      # for checking that the band is continuous
    ctotal = 0
    lum_sum = lum_sq = 0.0
    hi = lo = 0

    for i in range(total):
        p = i * 3
        r, g, b = raw[p], raw[p + 1], raw[p + 2]

        key = pixel_group(r, g, b)
        tally[key] += 1
        acc = sums[key]
        acc[0] += r
        acc[1] += g
        acc[2] += b
        if key in CHROMATIC:
            deg = int(colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[0] * 360)
            hues[deg] += 1

        y, x = divmod(i, w)
        if cy0 <= y < cy1 and cx0 <= x < cx1:
            ctally[key] += 1
            cacc = csums[key]
            cacc[0] += r
            cacc[1] += g
            cacc[2] += b
            ctotal += 1
            if key in CHROMATIC:
                chues[deg] += 1

        # perceived brightness: the eye is most sensitive to green
        lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
        lum_sum += lum
        lum_sq += lum * lum
        if lum > CLIP_HI:
            hi += 1
        elif lum < CLIP_LO:
            lo += 1

    win, hexv, share, chroma = winner(tally, sums, hues, total)
    cwin, chexv, cshare, _ = winner(ctally, csums, chues, ctotal)

    mean = lum_sum / total
    var = max(lum_sq / total - mean * mean, 0.0)      # the spread of brightness is the contrast

    return {
        "color": win,
        "color_hex": hexv,
        "color_share": share,
        "color_center": cwin,
        "color_center_hex": chexv,
        "color_center_share": cshare,
        "brightness": round(mean, 4),
        "contrast": round(var ** 0.5, 4),
        "chroma": round(chroma, 4),
        "clip_hi": round(hi / total, 4),
        "clip_lo": round(lo / total, 4),
    }


def load_small(path, exe=None, grid=48):
    """A scaled-down copy of the frame, for analysis.

    A JPEG is shrunk during decoding itself, so the full size never comes into
    memory. The scaling is strictly NEAREST, that is, existing pixels are taken
    rather than neighbours averaged: with smoothing, a night frame of black
    background and yellow lights would turn into solid mud."""
    try:
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        with Image.open(path) as im:
            try:
                im.draft("RGB", (grid * 4, grid * 4))
            except Exception:
                pass
            return im.convert("RGB").resize((grid, grid), Image.NEAREST)
    except Exception:
        pass
    if exe:                                # RAW: take the embedded preview
        import io
        for tag in ("-ThumbnailImage", "-PreviewImage", "-JpgFromRaw"):
            try:
                out = subprocess.run(
                    [exe, "-b", tag, path], capture_output=True, timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if out.stdout and len(out.stdout) > 1000:
                    from PIL import Image
                    with Image.open(io.BytesIO(out.stdout)) as im:
                        return im.convert("RGB").resize((grid, grid), Image.NEAREST)
            except Exception:
                continue
    return None


STAT_KEYS = ["color", "color_hex", "color_share",
             "color_center", "color_center_hex", "color_center_share",
             "brightness", "contrast",
             "chroma", "clip_hi", "clip_lo"]
# the length always matches STAT_KEYS: add a metric and the blank follows by itself
FAILED = (UNKNOWN,) + (None,) * (len(STAT_KEYS) - 1)


def stats_of(job):
    """A job for a worker process: (id, path, path to exiftool)."""
    pid, path, exe = job
    im = load_small(path, exe)
    if im is None:
        return (pid, *FAILED)
    try:
        got = image_stats(im)
    except Exception:
        got = None
    if not got:
        return (pid, *FAILED)
    return (pid, *(got[k] for k in STAT_KEYS))


def analyze_colors(con, exe, workers, reset=False):
    """Analyses only the photos that have not been analysed yet: the pass can be
    interrupted and carried on by another run. The condition includes brightness so
    that databases built before the light metrics existed fill themselves in."""
    row = con.execute("SELECT v FROM meta WHERE k = 'color_algo'").fetchone()
    stored = int(row[0]) if row else 0
    if reset or stored != ALGO_VERSION:
        done_before, = con.execute("SELECT COUNT(*) FROM photos "
                                   "WHERE color IS NOT NULL").fetchone()
        if done_before:
            print("Image analysis rules have changed — "
                  f"re-analysing all {done_before} photos.")
        con.execute("UPDATE photos SET color=NULL, color_hex=NULL, color_share=NULL,"
                    " brightness=NULL, contrast=NULL, chroma=NULL,"
                    " clip_hi=NULL, clip_lo=NULL")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('color_algo', ?)",
                    (str(ALGO_VERSION),))
        con.commit()

    rows = con.execute("SELECT id, path FROM photos "
                       "WHERE color IS NULL OR brightness IS NULL").fetchall()
    if not rows:
        print("Images already analysed — colour and light are done for every photo.")
        return
    jobs = [(pid, path, exe) for pid, path in rows]
    print(f"Analysing images: {len(jobs)} files, {workers} processes.")
    print("This is much slower than EXIF — the pixels are read.")
    print("Ctrl+C stops it; whatever is done is kept.", flush=True)

    sql = ("UPDATE photos SET " + ", ".join(f"{k}=?" for k in STAT_KEYS) +
           " WHERE id=?")
    started, done, buf = time.time(), 0, []
    pool = ProcessPoolExecutor(max_workers=workers)
    try:
        for row in pool.map(stats_of, jobs, chunksize=16):
            pid, values = row[0], row[1:]
            buf.append((*values, pid))
            done += 1
            if len(buf) >= 200:
                con.executemany(sql, buf)
                con.commit()
                buf = []
            if done % 100 == 0 or done == len(jobs):
                el = time.time() - started
                speed = done / max(el, 0.001)
                left = (len(jobs) - done) / max(speed, 0.001)
                print(f"\r  {done}/{len(jobs)} ({100 * done / len(jobs):5.1f}%)  "
                      f"{speed:.0f} files/s  about {left / 60:.0f} min left    ",
                      end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if buf:
            con.executemany(sql, buf)
            con.commit()
    print()

    bad, = con.execute("SELECT COUNT(*) FROM photos WHERE color = ?",
                       (UNKNOWN,)).fetchone()
    if bad:
        print(f"Could not read the image of {bad} files"
              f"{' (RAW needs ExifTool)' if not exe else ''}.")
    left, = con.execute("SELECT COUNT(*) FROM photos "
                        "WHERE color IS NULL OR brightness IS NULL").fetchone()
    if left:
        print(f"Still to analyse: {left}. Run again to continue.")


# --------------------------------------------------------------------------
# Walking the file system
# --------------------------------------------------------------------------

def walk_photos(roots, exts, passed_by=None):
    """Yields files whose extension is in exts. Other formats it recognises are
    counted into passed_by, so that --raw and --all can be suggested later."""
    for root in roots:
        root = os.path.abspath(root)
        if os.path.isfile(root):
            if os.path.splitext(root)[1].lower() in exts:
                yield os.path.normpath(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS
                           and not d.startswith(".")]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in exts:
                    yield os.path.normpath(os.path.join(dirpath, fn))
                elif ext in PHOTO_EXT and passed_by is not None:
                    passed_by[ext] += 1


def active_exts(args):
    """--ext sets the whole list, --raw adds RAW on top, --all takes everything."""
    if args.all_formats:
        return set(PHOTO_EXT)
    if args.ext:
        exts = {"." + e.strip().lower().lstrip(".")
                for e in args.ext.split(",") if e.strip()}
    else:
        exts = set(DEFAULT_EXT)
    if args.raw:
        exts |= RAW_EXT
    return exts


def describe_exts(exts):
    if exts >= PHOTO_EXT:
        return "every supported format"
    if exts == DEFAULT_EXT:
        return "JPEG only"
    if exts == DEFAULT_EXT | RAW_EXT:
        return "JPEG, RAW and DNG"
    return ", ".join(sorted(e.lstrip(".") for e in exts))


def report_passed(passed_by):
    """Reports that files of formats we did not take were lying alongside."""
    if not passed_by:
        return
    raw = {e: n for e, n in passed_by.items() if e in RAW_EXT}
    other = {e: n for e, n in passed_by.items() if e not in RAW_EXT}
    top = lambda d: ", ".join(f"{e.lstrip('.')} {n}" for e, n in
                              sorted(d.items(), key=lambda x: -x[1])[:5])
    if raw:
        print(f"Skipped RAW and DNG: {sum(raw.values())} ({top(raw)}). "
              f"The --raw switch includes them.")
    if other:
        print(f"Skipped other formats: {sum(other.values())} ({top(other)}). "
              f"The --all switch includes everything.")


# --------------------------------------------------------------------------
# Checking a folder: how much of it is already in the archive
# --------------------------------------------------------------------------
#
# The question this pass answers: if this folder were tipped into the archive,
# how much of it would turn out to be copies of what is already there? The answer
# is needed before the files reach the database — which is why the database is
# opened read-only here, and that is not a promise in a comment but the mode of
# the connection: writing through it is impossible.
#
# The rule for "this is a copy" comes from dupkey.py — the same one the interface
# counts duplicates by. Otherwise the check would promise one thing and the
# duplicates panel would then show another.


def open_db_ro(path):
    """A read-only connection: mode=ro forbids writing at the SQLite level."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.execute("PRAGMA busy_timeout = 10000")
    return con


def stop_on_break():
    """Asks Windows to treat CTRL_BREAK as an ordinary Ctrl+C.

    The Stop button in the launcher window sends CTRL_BREAK_EVENT to the child
    process: a Ctrl+C signal would reach the whole process group and stop the
    window itself along with it. On CTRL_BREAK, Python does not raise
    KeyboardInterrupt by default but ends the process where it stands — the pass
    would break off right before the point where the summary is printed, and a
    stopped check would say nothing at all.

    Passes that write to the database do not need this: they save in parts as they
    go, and breaking one off loses nothing. So only the check installs the handler
    — it has nothing to lose but its answer.
    """
    if not hasattr(signal, "SIGBREAK"):          # not Windows
        return

    def raise_interrupt(_sig, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGBREAK, raise_interrupt)
    except (ValueError, OSError):                # not the main thread
        pass


def candidate_keys(con, todo, by_sig, full, exe, workers):
    """Yields (path, key) for every file in todo.

    By signature it is enough to read a hundred and twenty-eight kilobytes and no
    EXIF is needed at all. By name the capture time is needed, which means the same
    EXIF work as in an ordinary pass — hence the difference in speed between the
    two rules.
    """
    if by_sig:
        jobs = [(p, p, full) for p, _ in todo]
        pool = ThreadPoolExecutor(max_workers=workers * 2)
        try:
            yield from pool.map(file_sig, jobs)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return

    # The name key is assembled not in Python but by the same expression and the
    # same engine as the keys in the database. SQLite lowercases only Latin
    # letters, and str.lower() here would disagree with the database on every name
    # with Cyrillic in it.
    key_sql = ("SELECT " + dupkey.name_key("c") +
               " FROM (SELECT ? AS filename, ? AS size, ? AS taken,"
               " ? AS date_src) c")

    def read(chunk):
        if exe:
            tags = read_chunk_exiftool(exe, [p for p, _ in chunk])
            return [make_row(p, st, tags.get(p, {})) for p, st in chunk]
        return [make_row(p, st, read_one_fallback(p)) for p, st in chunk]

    chunks = [todo[i:i + 150] for i in range(0, len(todo), 150)]
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for rows in pool.map(read, chunks):
            for row in rows:
                key, = con.execute(key_sql, (row["filename"], row["size"],
                                             row["taken"], row["date_src"])).fetchone()
                yield row["path"], key
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def check_dups(db_path, roots, exts, min_size, workers=4):
    """Checks a folder against the archive and prints the summary. Writes nothing."""
    if not os.path.exists(db_path):
        print(f"No database at {db_path}. Scan your folders first.")
        return 1
    stop_on_break()
    try:
        con = open_db_ro(db_path)
        by_sig = dupkey.sigs_complete(con)
        row = con.execute("SELECT v FROM meta WHERE k = 'sig_mode'").fetchone()
        full = bool(row and row[0] == "full")
        # The archive's keys go into memory whole: forty thousand rows is a couple
        # of megabytes, and every file is then checked without a query.
        keys = {k for k, in con.execute(
            f"SELECT {dupkey.dup_key(by_sig=by_sig)} FROM photos") if k}
        paths = {p for p, in con.execute("SELECT path FROM photos")}
    except sqlite3.Error as e:
        print(f"Cannot read the database: {e}")
        return 1
    print(f"Checking against {len(paths)} photos in the archive.")
    print(f"Rule: {dupkey.rule_name(by_sig)}.")
    if not by_sig and paths:
        print("  Signatures are not computed for every photo yet, so name, size\n"
              "  and capture time are used. Sign files first for the strict rule —\n"
              "  it also catches copies that were renamed.")
    print(f"Looking at: {describe_exts(exts)}", flush=True)
    print("Looking for files…", flush=True)

    todo, tiny, unreadable = [], 0, 0
    for path in walk_photos(roots, exts):
        try:
            st = os.stat(path)
        except OSError:
            unreadable += 1
            continue
        if st.st_size < min_size:
            tiny += 1
            continue
        todo.append((path, st))

    if tiny:
        print(f"Skipped {tiny} file{'' if tiny == 1 else 's'} under "
              f"{min_size // 1024} KB — thumbnails and the like, which a scan "
              f"would leave out too.")
    if not todo:
        print("No photos to check in that folder.")
        con.close()
        return 0

    # Under the signature rule no EXIF is needed, so there is no point looking
    # for ExifTool.
    exe = None
    if not by_sig:
        exe, ver = find_exiftool()
        print(f"Reading EXIF with {'ExifTool ' + ver if exe else 'exifread/Pillow'}.",
              flush=True)
    print(f"Checking {len(todo)} file{'' if len(todo) == 1 else 's'}. "
          f"Ctrl+C stops it; the summary still comes.", flush=True)

    dup_archive = dup_inside = same_path = new = failed = 0
    seen = set()
    started, done = time.time(), 0
    stopped = False
    try:
        for path, key in candidate_keys(con, todo, by_sig, full, exe, workers):
            done += 1
            if key is None:                 # the file could not be read
                failed += 1
            elif path in paths:
                same_path += 1
            elif key in keys:
                dup_archive += 1
            elif key in seen:
                dup_inside += 1
            else:
                new += 1
                seen.add(key)
            if done % 200 == 0 or done == len(todo):
                el = time.time() - started
                speed = done / max(el, 0.001)
                left = (len(todo) - done) / max(speed, 0.001)
                print(f"\r  {done}/{len(todo)} ({100 * done / len(todo):5.1f}%)  "
                      f"{speed:.0f} files/s  about {left / 60:.0f} min left    ",
                      end="", flush=True)
    except KeyboardInterrupt:
        stopped = True
    con.close()

    print()
    print(f"\nChecked {done} of {len(todo)} files." if stopped
          else f"\nChecked {done} file{'' if done == 1 else 's'} "
               f"in {time.time() - started:.0f} s.")
    print(f"  duplicates: {dup_archive + dup_inside}")
    if dup_inside:
        print(f"    {dup_archive} already in the archive, "
              f"{dup_inside} repeated inside the folder itself")
    print(f"  new:        {new}")
    if same_path:
        # Neither a duplicate nor new: this is the very same file, already counted
        # by the archive. Putting it among the copies would be to say the folder
        # can be deleted — and what would be deleted is what the database points
        # at.
        print(f"  already scanned: {same_path} — these very files are in the "
              f"archive\n                   (same path), not copies of them")
    if failed:
        print(f"  could not be read: {failed}")
    if unreadable:
        print(f"  skipped, unreadable: {unreadable}")
    if stopped:
        print("\nStopped early — the numbers above cover the files checked so far.")
    print("\nNothing was written to the database.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Scan a photo archive into a database")
    ap.add_argument("roots", nargs="*", help="folders with photos")
    ap.add_argument("--version", action="version", version=f"photostats {VERSION}")
    ap.add_argument("--db", default="photos.db", help="database file (default photos.db)")
    ap.add_argument("--full", action="store_true", help="re-read every file")
    ap.add_argument("--raw", action="store_true",
                    help="include RAW and DNG (cr2, cr3, nef, arw, orf, rw2, raf, dng…)")
    ap.add_argument("--all", action="store_true", dest="all_formats",
                    help="every format: JPEG, PNG, TIFF, HEIC, WebP and RAW")
    ap.add_argument("--ext", help="your own extension list instead of JPEG, e.g. heic,tif")
    ap.add_argument("--workers", type=int, default=4, help="parallel threads")
    ap.add_argument("--min-size", type=int, default=MIN_SIZE // 1024, dest="min_kb",
                    help=f"skip files smaller than this many KB "
                         f"(default {MIN_SIZE // 1024}; 0 keeps everything)")
    ap.add_argument("--colors", action="store_true",
                    help="after EXIF, work out the dominant colour of every photo")
    ap.add_argument("--colors-only", action="store_true", dest="colors_only",
                    help="images only, over an existing database; no folders needed")
    ap.add_argument("--hash", action="store_true",
                    help="also compute a content signature for every file")
    ap.add_argument("--hash-only", action="store_true", dest="hash_only",
                    help="signatures only, over an existing database")
    ap.add_argument("--hash-full", action="store_true", dest="hash_full",
                    help="read whole files for the signature instead of head and tail")
    ap.add_argument("--recolor", action="store_true",
                    help="re-analyse images even where it was already done")
    ap.add_argument("--check-dups", action="store_true", dest="check_dups",
                    help="report how much of a folder is already in the archive; "
                         "reads the database, never writes to it")
    args = ap.parse_args()
    if args.check_dups and not args.roots:
        ap.error("--check-dups needs the folder to check")
    if args.recolor and not args.roots:
        args.colors_only = True
    if (args.hash_only or args.hash_full) and not args.roots:
        args.hash_only = True
    # The signature pass needs no folders — it works off what is already in the
    # database. Forgetting hash_only here made argument parsing reject both
    # `scan.py --hash-only` from the README and the Sign files button.
    if not args.roots and not (args.colors_only or args.hash_only):
        ap.error("give at least one folder "
                 "(or use --colors-only / --hash-only)")

    if args.check_dups:
        return check_dups(args.db, args.roots, active_exts(args),
                          max(0, args.min_kb) * 1024, args.workers)

    if args.hash_only:
        if not os.path.exists(args.db):
            print(f"No database at {args.db}. Scan your folders first.")
            return
        con = open_db(args.db)
        sign_files(con, min((os.cpu_count() or 4) * 2, 12), full=args.hash_full)
        con.close()
        print("\nDone. Now run:  python app.py")
        return

    if args.colors_only:
        if not os.path.exists(args.db):
            print(f"No database at {args.db}. Scan your folders first.")
            return
        con = open_db(args.db)
        analyze_colors(con, find_exiftool()[0], min(os.cpu_count() or 4, 8),
                       reset=args.recolor)
        con.close()
        print("\nDone. Now run:  python app.py")
        return

    con = open_db(args.db)

    row = con.execute("SELECT v FROM meta WHERE k = 'exif_version'").fetchone()
    stored_exif = int(row[0]) if row else 0
    if stored_exif != EXIF_VERSION and not args.full:
        have, = con.execute("SELECT COUNT(*) FROM photos").fetchone()
        if have:
            print(f"The EXIF field set has changed — re-reading metadata of "
                  f"{have} files. Image analysis is kept.")
            args.full = True

    known = {}
    if not args.full:
        for p, s, m in con.execute("SELECT path, size, mtime FROM photos"):
            known[p] = (s, m)


    args.min_size = max(0, args.min_kb) * 1024
    exts = active_exts(args)
    passed_by = Counter()
    print(f"Looking at: {describe_exts(exts)}", flush=True)
    print("Looking for files…", flush=True)
    todo, skipped, gone = [], 0, set(known)
    tiny, tiny_known = 0, []
    for path in walk_photos(args.roots, exts, passed_by):
        gone.discard(path)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_size < args.min_size:
            tiny += 1
            if path in known:
                tiny_known.append(path)   # got into the database on an earlier run
            continue
        prev = known.get(path)
        if prev and abs(prev[0] - st.st_size) < 1 and abs(prev[1] - st.st_mtime) < 2:
            skipped += 1
            continue
        todo.append((path, st))

    if tiny_known:
        con.executemany("DELETE FROM photos WHERE path = ?",
                        [(p,) for p in tiny_known])
        con.commit()

    # only files genuinely absent from the disk count as gone, or changing the set
    # of formats would sweep previously collected RAW out of the database
    gone = {p for p in gone if not os.path.exists(p)}
    if gone:
        con.executemany("DELETE FROM photos WHERE path = ?", [(p,) for p in gone])
        con.commit()
        print(f"Dropped from the database (files are gone): {len(gone)}")

    if tiny:
        kb = args.min_size // 1024
        print(f"Skipped {tiny} files under {kb} KB — thumbnails and the like." +
              (f" {len(tiny_known)} of them were dropped from the database."
               if tiny_known else ""))
    print(f"New and changed: {len(todo)}   already in the database: {skipped}")
    report_passed(passed_by)
    if not todo:
        con.close()
        print("Nothing to update.")
        return

    exe, ver = find_exiftool()
    if exe:
        print(f"Reading EXIF with ExifTool {ver} — {exe}", flush=True)
    else:
        print("Reading EXIF with exifread/Pillow.", flush=True)
        if exts & RAW_EXT:
            print("  That is not enough for RAW: brand, lens and focal length will\n"
                  "  usually stay empty. Install ExifTool — winget install "
                  "-e --id OliverBetz.ExifTool", flush=True)

    # Only the EXIF columns are updated. The earlier INSERT OR REPLACE rewrote the
    # whole row and cleared the image analysis — re-reading the metadata would have
    # meant losing colour and light across the whole archive.
    upd = ", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c != "path")
    sql = (f"INSERT INTO photos ({','.join(COLUMNS)}) "
           f"VALUES ({','.join(':' + c for c in COLUMNS)}) "
           f"ON CONFLICT(path) DO UPDATE SET {upd}")

    started = time.time()
    done = 0
    CHUNK = 150

    def flush(rows):
        nonlocal done
        con.executemany(sql, rows)
        con.commit()
        done += len(rows)
        speed = done / max(time.time() - started, 0.001)
        pct = 100 * done / len(todo)
        print(f"\r  processed {done}/{len(todo)}  ({pct:5.1f}%)  "
              f"{speed:.0f} files/s", end="", flush=True)

    if exe:
        chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)]

        def work(chunk):
            tags_by_path = read_chunk_exiftool(exe, [p for p, _ in chunk])
            return [make_row(p, st, tags_by_path.get(p, {})) for p, st in chunk]

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for rows in pool.map(work, chunks):
                flush(rows)
    else:
        with ThreadPoolExecutor(max_workers=args.workers * 2) as pool:
            buf = []
            for (path, st), tags in zip(todo, pool.map(
                    lambda t: read_one_fallback(t[0]), todo)):
                buf.append(make_row(path, st, tags))
                if len(buf) >= CHUNK:
                    flush(buf)
                    buf = []
            if buf:
                flush(buf)

    print()
    if args.hash:
        print()
        sign_files(con, min((os.cpu_count() or 4) * 2, 12), full=args.hash_full)

    if args.colors:
        print()
        analyze_colors(con, exe, min(os.cpu_count() or 4, 8), reset=args.recolor)

    con.execute("INSERT OR REPLACE INTO meta VALUES ('scanned_at', ?)",
                (datetime.now().isoformat(timespec="seconds"),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('roots', ?)",
                (json.dumps([os.path.abspath(r) for r in args.roots], ensure_ascii=False),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('exts', ?)",
                (json.dumps(sorted(exts)),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('exif_version', ?)",
                (str(EXIF_VERSION),))
    con.commit()

    total, = con.execute("SELECT COUNT(*) FROM photos").fetchone()
    no_exif, = con.execute("SELECT COUNT(*) FROM photos WHERE date_src='file'").fetchone()
    yrs = con.execute("SELECT MIN(year), MAX(year) FROM photos").fetchone()
    by_ext = con.execute("SELECT ext, COUNT(*) c FROM photos GROUP BY ext "
                         "ORDER BY c DESC").fetchall()
    pending_color, = con.execute("SELECT COUNT(*) FROM photos "
                                 "WHERE color IS NULL OR brightness IS NULL"
                                 ).fetchone()
    year_limit = datetime.now().year + 1
    odd, = con.execute("SELECT COUNT(*) FROM photos WHERE year < 1990 OR year > ?",
                       (year_limit,)).fetchone()
    con.close()

    print(f"\nDone in {time.time() - started:.0f} s.")
    print(f"In the database: {total} photos, years {yrs[0]}–{yrs[1]}.")
    if len(by_ext) > 1:
        print("By format: " + ", ".join(f"{e} {c}" for e, c in by_ext[:8]))
        print("A frame stored as both RAW and JPEG is counted twice —"
              "\n  the interface can filter by format.")
    if no_exif:
        print(f"{no_exif} files have no EXIF date — the file date was used instead.")
    if odd:
        print(f"{odd} photo{'' if odd == 1 else 's'} dated outside 1990–{year_limit} —\n"
              f"  usually a camera clock that was reset. They stay out of the\n"
              f"  timeline but remain in the database, reachable from a link\n"
              f"  under the timeline card.")
    print(f"Database: {os.path.abspath(args.db)}")
    if pending_color:
        print(f"{pending_color} photos have no image analysis yet "
              f"(colour, brightness, contrast).\n"
              f"  That is a separate pass:  scan.py --colors-only")
    print("\nNow run:  python app.py")


if __name__ == "__main__":
    sys.exit(main())
