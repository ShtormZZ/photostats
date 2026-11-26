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
app.py — the photo archive statistics, viewed in a browser.

Running it:
    python app.py                 (opens http://127.0.0.1:5577)
    python app.py --db photos.db --port 5577 --no-browser
"""

import argparse
import datetime
import io
import ipaddress
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from contextlib import closing
from hashlib import md5

from flask import (Flask, Response, abort, g, jsonify, request, send_file,
                   send_from_directory)
from werkzeug.serving import WSGIRequestHandler

import db_schema
import dupkey
import jobs
import raw_pairs
import tags

try:
    from version import VERSION
except ImportError:          # the file was copied without version.py
    VERSION = "unknown"

APP_DIR = os.path.dirname(os.path.abspath(__file__))


class TimedRequestHandler(WSGIRequestHandler):
    """Include server response time in the log the launcher already displays."""

    def handle_one_request(self):
        # A handler belongs to one connection; reset for every request so that
        # neither concurrent requests nor a reused connection share a timer.
        self._request_started = time.perf_counter()
        return super().handle_one_request()

    def log_request(self, code="-", size="-"):
        # Werkzeug logs when it starts sending the response headers, before
        # transferring the body. Large downloads must not inflate this time.
        elapsed_ms = (time.perf_counter() - self._request_started) * 1000
        super().log_request(code, f"{size} {elapsed_ms:.1f} ms")


def resolve_web_dir():
    """index.html is looked for in web\\, and next to app.py if it was moved."""
    for d in (os.path.join(APP_DIR, "web"), APP_DIR):
        if os.path.isfile(os.path.join(d, "index.html")):
            return d
    return None


WEB_DIR = resolve_web_dir()
CACHE_DIR = os.path.join(APP_DIR, "cache", "thumbs")
NONE_KEY = "__none__"
NONE_LABEL = "not specified"

app = Flask(__name__, static_folder=None)
DB_PATH = os.path.join(APP_DIR, "photos.db")

# The timeline is built as an unbroken run of months, so one photo with a
# broken date stretches the strip over decades of empty cells. Months outside
# this range stay out of the strip — but the photos themselves are not lost and
# are reachable from a link of their own in the card.
MIN_YEAR = 1990

# Whether the server was started on the loopback address. Set in main(). When it
# was not, the interface is open to the local network, and the file actions
# below are switched off for everyone — see files_here().
LOOPBACK_ONLY = True


def is_loopback(addr):
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return addr == "localhost"


def files_here():
    """May this request open files on the computer the server runs on?

    "Show in explorer" and "Open file" act on this machine, not on the one
    holding the browser: pressed from a tablet on the sofa, they would throw a
    window open on a desk nobody is sitting at — or in front of whoever is. So
    they need both: a server that listens on loopback only, and a request that
    came from loopback. The second alone would do today; the first is there so
    that a server opened to the network never starts a program on request,
    whatever address a request appears to come from.
    """
    return LOOPBACK_ONLY and is_loopback(request.remote_addr or "")


FILES_REFUSED = ("Opening files works only on the computer running photostats, "
                 "and only while network access is off")


def year_range():
    return MIN_YEAR, datetime.date.today().year + 1

MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]

# ordered by the spectrum, not by count: the card reads as a palette
COLOR_ORDER = ["red", "orange", "brown", "yellow", "green",
               "cyan", "blue", "violet", "white", "gray", "black"]
COLOR_UNKNOWN = "unknown"
COLOR_MIXED = "mixed"
# If the largest colour group holds less than this share of the frame, no colour
# dominates — such a photo counts as mixed. The threshold applies in the query,
# so it can be changed without re-analysing the archive.
MIXED_MAX = 0.25
COLOR_SWATCH = {
    "red": "#C8342E", "orange": "#E08A2B", "brown": "#8A5A3B",
    "yellow": "#DFC732",
    "green": "#4A9E4E", "cyan": "#4FB3C8", "blue": "#3A5BC7",
    "violet": "#8A55B8", "white": "#EFEDE8", "gray": "#8B8598",
    "black": "#242229", COLOR_UNKNOWN: "#3A3745",
    COLOR_MIXED: ("linear-gradient(135deg,#C8342E,#E08A2B,#DFC732,#4A9E4E,"
                  "#4FB3C8,#3A5BC7,#8A55B8)"),
}

MB = 1024 * 1024
BIG = 10 ** 12

FOCAL_BUCKETS = [
    (0, 15, "under 15 mm"), (15, 21, "15–20 mm"), (21, 25, "21–24 mm"),
    (25, 29, "25–28 mm"), (29, 36, "29–35 mm"), (36, 51, "36–50 mm"),
    (51, 71, "51–70 mm"), (71, 86, "71–85 mm"), (86, 106, "86–105 mm"),
    (106, 136, "106–135 mm"), (136, 201, "136–200 mm"), (201, 301, "201–300 mm"),
    (301, 401, "301–400 mm"), (401, 601, "401–600 mm"), (601, BIG, "over 600 mm"),
]

APERTURE_BUCKETS = [
    (0, 1.5, "f/1.4 and faster"), (1.5, 2.1, "f/1.6 – f/2.0"),
    (2.1, 3.0, "f/2.2 – f/2.8"), (3.0, 4.3, "f/3.2 – f/4.0"),
    (4.3, 6.0, "f/4.5 – f/5.6"), (6.0, 8.5, "f/6.3 – f/8.0"),
    (8.5, 12, "f/9 – f/11"), (12, 17, "f/13 – f/16"),
    (17, 24, "f/18 – f/22"), (24, BIG, "f/25 and smaller"),
]

# ranges include the left edge: 1/125 falls into "1/125 – 1/60"
EXPOSURE_BUCKETS = [
    (0, 0.00025, "faster than 1/4000"), (0.00025, 0.001, "1/4000 – 1/1000"),
    (0.001, 0.002, "1/1000 – 1/500"), (0.002, 0.004, "1/500 – 1/250"),
    (0.004, 0.008, "1/250 – 1/125"), (0.008, 0.0167, "1/125 – 1/60"),
    (0.0167, 0.0667, "1/60 – 1/15"), (0.0667, 0.25, "1/15 – 1/4"),
    (0.25, 1, "1/4 – 1 s"), (1, 4, "1 – 4 s"), (4, 30, "4 – 30 s"),
    (30, BIG, "30 s and longer"),
]

ISO_BUCKETS = [
    (0, 125, "up to ISO 100"), (125, 250, "ISO 125 – 200"),
    (250, 500, "ISO 250 – 400"), (500, 1000, "ISO 500 – 800"),
    (1000, 2000, "ISO 1000 – 1600"), (2000, 4000, "ISO 2000 – 3200"),
    (4000, 8000, "ISO 4000 – 6400"), (8000, 25601, "ISO 8000 – 25600"),
    (25601, BIG, "over ISO 25600"),
]

SIZE_BUCKETS = [
    (0, MB // 2, "under 0.5 MB"), (MB // 2, MB, "0.5 – 1 MB"),
    (MB, 2 * MB, "1 – 2 MB"), (2 * MB, 4 * MB, "2 – 4 MB"),
    (4 * MB, 8 * MB, "4 – 8 MB"), (8 * MB, 16 * MB, "8 – 16 MB"),
    (16 * MB, 32 * MB, "16 – 32 MB"), (32 * MB, 64 * MB, "32 – 64 MB"),
    (64 * MB, BIG, "over 64 MB"),
]

BRIGHTNESS_BUCKETS = [
    (0, 0.10, "very dark"), (0.10, 0.22, "dark"),
    (0.22, 0.36, "subdued"), (0.36, 0.52, "medium"),
    (0.52, 0.68, "light"), (0.68, 1.01, "very light"),
]

CONTRAST_BUCKETS = [
    (0, 0.08, "flat"), (0.08, 0.14, "low"), (0.14, 0.20, "moderate"),
    (0.20, 0.27, "pronounced"), (0.27, 1.01, "high"),
]

# Sharpness is the RMS of the Laplacian at 1500 px (scan.py:sharpness()), a
# number with no natural scale. These bounds are the quintiles of the centre
# measure over 400 random frames of a real 130 000-photo archive — 12.5, 22.9,
# 37.2 and 55.5, rounded — so each band started out as a fifth of that archive.
# A starting point to be tuned by eye, and tuning needs no re-scan: the bands
# are applied in the query. The same bounds serve the edges, so that "soft"
# means the same thing on both panels and the two can be compared.
SHARP_BUCKETS = [
    (0, 12, "very soft"), (12, 23, "soft"), (23, 37, "medium"),
    (37, 55, "sharp"), (55, BIG, "very sharp"),
]
SHARP_SOFT = 12       # below this, in both regions, a photo is soft throughout
# One region counts as softer than the other when its measure is under this
# share of the other's. Edges under half the centre was a tenth of the sample
# — a subject in focus against a background that is not.
FOCUS_RATIO = 0.5

# The rule itself is in dupkey.py: the folder check in scan.py uses the same one,
# and those two answers must not drift apart. Only the per-request cache is here.


def sig_ready():
    """Is every file signed? Worked out once per request."""
    if not hasattr(g, "sig_ready"):
        with closing(db()) as con, con:
            g.sig_ready = dupkey.sigs_complete(con, visible_only=True)
    return g.sig_ready


def name_key(alias=""):
    return dupkey.name_key(alias)


def dup_key(alias=""):
    return dupkey.dup_key(alias, sig_ready())


def dup_exists():
    k = dup_key()
    return (f"{k} IN (SELECT {k} FROM visible_photos AS photos "
            "GROUP BY 1 HAVING COUNT(*) > 1)")


# One rating, two columns behind it. `rating` is what the scanner read out of
# EXIF and rewrites on every pass; `mark` is what you set here and nothing else
# ever writes. Your mark wins where there is one, so rating a photo in the
# interface replaces what the camera said without destroying it — clear the mark
# and the camera's stars come back.
#
# The two could not be one column. The scanner updates every column it owns from
# the file, so a shared one would lose your marks the first time EXIF_VERSION
# changed and the archive re-read itself.
MARK_BAD = "bad"
MARK_GREAT = "great"
MARKS = {"1", "2", "3", "4", "5", MARK_BAD, MARK_GREAT}
# Best first: this is the order the panel lists them in, not the order of counts.
RATING_ORDER = [MARK_GREAT, "5", "4", "3", "2", "1", MARK_BAD]
RATING_LABELS = {MARK_GREAT: "5+", MARK_BAD: "✗ bad"}


def rating_key():
    return db_schema.rating_key()


def rating_rank():
    """The same rating as a number, so ORDER BY puts 5+ above 5 and bad below 1."""
    k = rating_key()
    return (f"CASE WHEN {k} IS NULL THEN -1 WHEN {k} = '{MARK_GREAT}' THEN 6 "
            f"WHEN {k} = '{MARK_BAD}' THEN 0 ELSE CAST({k} AS INTEGER) END")


def rating_label(v):
    return RATING_LABELS.get(v) or "★" * int(v)


# Tags sit in photo_tags, one row per tag per photo — the rule and the schema are
# in tags.py. The filters below are plain SQL over unqualified column names, so
# they drop into a WHERE built for `photos` without a join or an alias.


def has_tag(n):
    return (f"photos.id IN (SELECT photo_id FROM photo_tags "
            f"WHERE tag IN ({','.join('?' * n)}))")


UNTAGGED = "photos.id NOT IN (SELECT photo_id FROM photo_tags)"

# The tags of one photo, for the list and the card. group_concat has no defined
# order of its own, so the sort happens in a subquery — otherwise the chips
# reshuffle between two requests for no reason the reader can see. A comma is
# safe as the separator: a tag is letters and digits, and can hold neither one.
TAG_COL = ("(SELECT group_concat(tag) FROM (SELECT tag FROM photo_tags"
           " WHERE photo_id = photos.id ORDER BY tag)) AS tags")


# Derived dimensions: the value is worked out from the numbers in the query
# itself, so the thresholds can be changed here without re-analysing the archive.
DERIVED_DIMS = {
    "tone": ("chroma", [
        ("colour", "chroma >= 0.02"),
        ("black & white", "chroma < 0.02"),
    ]),
    "clipping": ("clip_hi", [
        ("no loss", "clip_hi < 0.005 AND clip_lo < 0.02"),
        ("blown highlights", "clip_hi >= 0.005 AND clip_lo < 0.02"),
        ("crushed shadows", "clip_hi < 0.005 AND clip_lo >= 0.02"),
        ("both ends", "clip_hi >= 0.005 AND clip_lo >= 0.02"),
    ]),
    # The centre against the rest of the frame. Every condition stands on its
    # own — a filter uses one at a time, not in order — so they are written to
    # exclude one another rather than relying on the CASE that counts them.
    # "Edges sharper" is usually a missed focus, but not always: a
    # landscape with plain sky in the middle reads the same way.
    "focus": ("sharp_center", [
        ("soft throughout",
         f"MAX(sharp_center, sharp_edge) < {SHARP_SOFT}"),
        ("centre sharper",
         f"MAX(sharp_center, sharp_edge) >= {SHARP_SOFT}"
         f" AND sharp_edge < sharp_center * {FOCUS_RATIO}"),
        ("edges sharper",
         f"MAX(sharp_center, sharp_edge) >= {SHARP_SOFT}"
         f" AND sharp_center < sharp_edge * {FOCUS_RATIO}"),
        ("evenly sharp",
         f"MAX(sharp_center, sharp_edge) >= {SHARP_SOFT}"
         f" AND sharp_edge >= sharp_center * {FOCUS_RATIO}"
         f" AND sharp_center >= sharp_edge * {FOCUS_RATIO}"),
    ]),
}

# dimension -> (column, ranges). The focal column depends on the 35mm-equivalent toggle.
RANGE_DIMS = {
    "focal": (None, FOCAL_BUCKETS),
    "aperture": ("fnumber", APERTURE_BUCKETS),
    "exposure": ("exposure", EXPOSURE_BUCKETS),
    "iso": ("iso", ISO_BUCKETS),
    "size": ("size", SIZE_BUCKETS),
    "brightness": ("brightness", BRIGHTNESS_BUCKETS),
    "contrast": ("contrast", CONTRAST_BUCKETS),
    "sharp_center": ("sharp_center", SHARP_BUCKETS),
    "sharp_edge": ("sharp_edge", SHARP_BUCKETS),
}


def band_label(col, buckets):
    """The name of the band a value falls in, worked out in the query, so that
    the photo card says what the panels say without knowing the bounds."""
    case = " ".join(f"WHEN {col} < {hi} THEN '{lab}'" for _, hi, lab in buckets)
    return f"CASE WHEN {col} IS NULL THEN NULL {case} END"


def focus_label():
    case = " ".join(f"WHEN {cond} THEN '{name}'"
                    for name, cond in DERIVED_DIMS["focus"][1])
    return f"CASE WHEN sharp_center IS NULL OR sharp_edge IS NULL THEN NULL {case} END"


SHARP_LABEL_COLS = (f"{band_label('sharp_center', SHARP_BUCKETS)} AS sharp_center_l,"
                    f" {band_label('sharp_edge', SHARP_BUCKETS)} AS sharp_edge_l")

# What a photo row carries about sharpness, for the list, the strip and the card.
SHARP_COLS = f"sharp_center, sharp_edge, {SHARP_LABEL_COLS}, {focus_label()} AS focus"


def range_col(key):
    col = RANGE_DIMS[key][0]
    return col or focal_col()


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------


# The server reads the database while the scanner writes to it. Without waiting
# on the lock, nearly half the queries fail with "database is locked" — measured.
BUSY_MS = db_schema.BUSY_MS


def db():
    con = db_schema.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


SIMPLE_FILTERS = {
    "year": "year", "ym": "ym", "month": "month", "hour": "hour",
    "brand": "brand", "camera": "camera", "lens": "lens", "ext": "ext",
}


def focal_col():
    return "focal35" if request.args.get("eq") == "1" else "focal"


def day_bounds(value):
    """Half-open timestamp bounds keep day queries on the capture-time index."""
    try:
        day = datetime.date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError
    except (ValueError, TypeError):
        abort(400, description="Expected a date in YYYY-MM-DD format")
    following = ((day + datetime.timedelta(days=1)).isoformat()
                 if day < datetime.date.max else "9999-12-32")
    return value, following


def build_where(skip=None):
    """Assembles the WHERE from the active filters. skip is the dimension (or
    several) left out, so that a chart does not truncate itself."""
    skips = set()
    if isinstance(skip, str):
        skips = {skip}
    elif skip:
        skips = set(skip)

    # Values arrive as repeated parameters (month=9&month=10) rather than a
    # comma-separated list: commas occur in camera and lens names, and in the map
    # bounds a comma separates the coordinates outright.
    clauses, params = [], []
    for key, col in SIMPLE_FILTERS.items():
        vals = request.args.getlist(key)
        if not vals or key in skips:
            continue
        parts = []
        for v in vals:
            if v == NONE_KEY:
                parts.append(f"{col} IS NULL")
            else:
                parts.append(f"{col} = ?")
                params.append(v)
        clauses.append("(" + " OR ".join(parts) + ")")

    day = request.args.get("day")
    if day and "day" not in skips:
        lo, hi = day_bounds(day)
        clauses.append("(taken >= ? AND taken < ?)")
        params += [lo, hi]

    for key in RANGE_DIMS:
        vals = request.args.getlist(key)
        if not vals or key in skips:
            continue
        col = range_col(key)
        parts = []
        for v in vals:
            if v == NONE_KEY:
                parts.append(f"{col} IS NULL")
            else:
                lo, hi = v.split("-")
                parts.append(f"({col} >= ? AND {col} < ?)")
                params += [float(lo), float(hi)]
        clauses.append("(" + " OR ".join(parts) + ")")

    for key in ("color", "color_center"):
        vals = request.args.getlist(key)
        if vals and key not in skips:
            expr = color_expr(key)
            clauses.append("(" + " OR ".join(f"({expr}) = ?" for _ in vals) + ")")
            params += vals

    for key in list(DERIVED_DIMS) + ["dup"]:
        vals = request.args.getlist(key)
        if not vals or key in skips:
            continue
        guard, opts = derived_opts(key)
        conds = [dict(opts)[v] for v in vals if v in dict(opts)]
        if conds:
            joined = " OR ".join(f"({c})" for c in conds)
            clauses.append(f"({guard} IS NOT NULL AND ({joined}))")

    vals = request.args.getlist("rating")
    if vals and "rating" not in skips:
        expr = rating_key()
        parts = []
        for v in vals:
            if v == NONE_KEY:
                parts.append(f"{expr} IS NULL")
            else:
                parts.append(f"{expr} = ?")
                params.append(v)
        clauses.append("(" + " OR ".join(parts) + ")")

    vals = request.args.getlist("tag")
    if vals and "tag" not in skips:
        # Several tags mean "any of these", the way every other panel here reads.
        # Values are normalised on the way in, so a tag typed with a capital or
        # in a decomposed form still matches what is stored; NONE_KEY holds an
        # underscore and cannot survive normalisation, so it never reaches the IN.
        wanted = [t for t in (tags.normalize(v) for v in vals) if t]
        parts = []
        if NONE_KEY in vals:
            parts.append(UNTAGGED)
        if wanted:
            parts.append(has_tag(len(wanted)))
            params += wanted
        if parts:
            clauses.append("(" + " OR ".join(parts) + ")")

    if request.args.get("baddate") and "baddate" not in skips:
        lo, hi = year_range()
        clauses.append("(year < ? OR year > ?)")
        params += [lo, hi]

    box = request.args.get("geo")
    if box and "geo" not in skips:
        try:
            s1, w1, n1, e1 = (float(x) for x in box.split(","))
            clauses.append("lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?")
            params += [min(s1, n1), max(s1, n1), min(w1, e1), max(w1, e1)]
        except ValueError:
            pass

    folder = request.args.get("folder")
    if folder and "folder" not in skips:
        # A directory name can contain SQL wildcards; they must stay literal.
        prefix = os.path.join(folder, "")
        prefix = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        clauses.append("(folder = ? OR folder LIKE ? ESCAPE '!')")
        params += [folder, prefix + "%"]

    q = request.args.get("q")
    if q:
        clauses.append("path LIKE ?")
        params.append("%" + q + "%")

    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def counts(col, skip, labeller=None, order="cnt", limit=None):
    where, params = build_where(skip=skip)
    sql = (f"SELECT {col} AS k, COUNT(*) AS cnt FROM visible_photos AS photos {where} "
           f"GROUP BY {col} ORDER BY " +
           ("cnt DESC" if order == "cnt" else f"{col} ASC"))
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    with closing(db()) as con, con:
        for r in con.execute(sql, params):
            k = r["k"]
            key = NONE_KEY if k is None else str(k)
            label = NONE_LABEL if k is None else (labeller(k) if labeller else str(k))
            out.append({"k": key, "l": label, "v": r["cnt"]})
    return out


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def rating_counts():
    """Best first, and only what carries a rating.

    The unrated are left out of the panel. In any archive they are most of it,
    and a bar for them set the scale for the rest: five stars against forty
    thousand unrated is a line a pixel wide, which is the one thing the panel is
    there to show. The selection is still there for anyone who wants it —
    `rating=__none__` asks for it.
    """
    items = [it for it in counts(rating_key(), skip="rating",
                                 labeller=rating_label)
             if it["k"] != NONE_KEY]
    rank = {k: i for i, k in enumerate(RATING_ORDER)}
    items.sort(key=lambda it: rank.get(it["k"], len(rank)))
    return items


def tag_counts():
    """The tags panel: how many photos of the selection carry each tag.

    The only count in the program that is not a GROUP BY over `photos`: one photo
    holds up to three tags, so the rows being counted are the tags. Photos with
    no tag are not one of them, for the reason rating_counts() gives — they are
    the bulk of an archive being tagged, and a bar for them leaves every real tag
    too short to compare. `tag=__none__` still selects them.
    """
    where, params = build_where(skip="tag")
    # Hidden RAW annotations stay on their files, but must not enter either
    # filtered counts or the unfiltered tag vocabulary.
    counted = (f"SELECT t.tag AS k, COUNT(*) AS cnt FROM photo_tags t"
               f" JOIN visible_photos AS photos ON photos.id = t.photo_id {where}")
    with closing(db()) as con, con:
        rows = con.execute(f"{counted} GROUP BY k ORDER BY cnt DESC, k",
                           params).fetchall()
    return [{"k": r["k"], "l": r["k"], "v": r["cnt"]} for r in rows]


def bucket_counts(key):
    """Sorts values into ranges in one query, without pulling the rows out."""
    col = range_col(key)
    buckets = RANGE_DIMS[key][1]
    case = " ".join([f"WHEN {col} IS NULL THEN '{NONE_KEY}'"] +
                    [f"WHEN {col} < {hi} THEN '{lo}-{hi}'" for lo, hi, _ in buckets])
    expr = f"CASE {case} ELSE '{NONE_KEY}' END"
    where, params = build_where(skip=key)
    with closing(db()) as con, con:
        got = dict(con.execute(f"SELECT {expr} AS k, COUNT(*) FROM visible_photos AS photos {where} "
                               f"GROUP BY k", params).fetchall())
    out = [{"k": f"{lo}-{hi}", "l": lab, "v": got.get(f"{lo}-{hi}", 0)}
           for lo, hi, lab in buckets]
    out = [b for b in out if b["v"]]
    if got.get(NONE_KEY):
        out.append({"k": NONE_KEY, "l": NONE_LABEL, "v": got[NONE_KEY]})
    return out


def derived_opts(key):
    """The duplicate rule depends on whether signatures have been computed, so it
    is assembled per request rather than once at startup."""
    if key == "dup":
        exists = dup_exists()
        return "filename", [("has duplicates", exists), ("no duplicates", "NOT " + exists)]
    return DERIVED_DIMS[key]


def derived_counts(key):
    guard, opts = derived_opts(key)
    case = " ".join(f"WHEN {cond} THEN '{name}'" for name, cond in opts)
    expr = f"CASE WHEN {guard} IS NULL THEN NULL {case} END"
    where, params = build_where(skip=key)
    with closing(db()) as con, con:
        got = dict(con.execute(f"SELECT {expr} AS k, COUNT(*) FROM visible_photos AS photos {where} "
                               f"GROUP BY k", params).fetchall())
    got.pop(None, None)
    return [{"k": n, "l": n, "v": got[n]} for n, _ in opts if got.get(n)]


def color_expr(col="color"):
    """The colour group, corrected for dominance — one definition serving both the
    counting and the filtering. col: color or color_center."""
    return (f"CASE WHEN {col} IS NULL THEN NULL "
            f"WHEN {col}_share < {MIXED_MAX} THEN '{COLOR_MIXED}' "
            f"ELSE {col} END")


def color_counts(col="color"):
    where, params = build_where(skip=col)
    with closing(db()) as con, con:
        got = dict(con.execute(f"SELECT {color_expr(col)} AS k, COUNT(*) FROM visible_photos AS photos "
                               f"{where} GROUP BY k", params).fetchall())
    got.pop(None, None)                    # NULL — the photo has not been analysed yet
    order = COLOR_ORDER + [COLOR_MIXED, COLOR_UNKNOWN]
    return [{"k": n, "l": n, "v": got[n], "c": COLOR_SWATCH[n]}
            for n in order if got.get(n)]


@app.get("/api/db/diagnostics")
def api_db_diagnostics():
    with closing(db()) as con:
        return jsonify(db_schema.diagnostics(con))


@app.get("/api/stats")
def api_stats():
    with closing(db()) as con, con:
        total_all, = con.execute("SELECT COUNT(*) FROM visible_photos AS photos").fetchone()
        if not total_all:
            return jsonify({"empty": True})
        where, params = build_where()
        total, = con.execute(f"SELECT COUNT(*) FROM visible_photos AS photos {where}", params).fetchone()
        dup_groups, = con.execute(
            f"SELECT COUNT(*) FROM (SELECT {dup_key()} FROM visible_photos AS photos "
            f"GROUP BY 1 HAVING COUNT(*) > 1)"
        ).fetchone()
        span = con.execute(
            f"SELECT MIN(taken), MAX(taken), MIN(ym), MAX(ym) FROM visible_photos AS photos {where}",
            params).fetchone()
        sized, = con.execute(f"SELECT COALESCE(SUM(size),0) FROM visible_photos AS photos {where}",
                             params).fetchone()

    # timeline: an unbroken run of months with no gaps
    raw = {i["k"]: i["v"] for i in counts("ym", skip=("ym", "year", "baddate"),
                                          order="key") if i["k"] != NONE_KEY}
    lo_year, hi_year = year_range()
    inside = {k: v for k, v in raw.items() if lo_year <= int(k[:4]) <= hi_year}
    outliers = sum(v for k, v in raw.items() if k not in inside)
    if not inside:                 # the whole archive is outside the range — show it as is
        inside, outliers = raw, 0

    timeline = []
    if inside:
        y0, m0 = (int(x) for x in min(inside).split("-"))
        y1, m1 = (int(x) for x in max(inside).split("-"))
        y, m = y0, m0
        while (y, m) <= (y1, m1):
            key = f"{y:04d}-{m:02d}"
            timeline.append({"k": key, "l": f"{MONTH_NAMES[m-1]} {y}",
                             "v": inside.get(key, 0)})
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    # hours: always 0…23
    hours_raw = {int(i["k"]): i["v"] for i in counts("hour", skip="hour", order="key")
                 if i["k"] != NONE_KEY}
    hours = [{"k": str(h), "l": f"{h:02d}:00", "v": hours_raw.get(h, 0)} for h in range(24)]

    # months of the year: always 12
    mon_raw = {int(i["k"]): i["v"] for i in counts("month", skip="month", order="key")
               if i["k"] != NONE_KEY}
    months = [{"k": str(m), "l": MONTH_NAMES[m - 1], "v": mon_raw.get(m, 0)}
              for m in range(1, 13)]

    return jsonify({
        "empty": False,
        "total": total,
        "totalAll": total_all,
        "bytes": sized,
        "first": span[0], "last": span[1],
        "outliers": outliers,
        "version": VERSION,
        # the page hides the file buttons by this; the server refuses them anyway
        "files": files_here(),
        "mixedMax": MIXED_MAX,
        "dupGroups": dup_groups,
        "dupRule": dupkey.rule_name(sig_ready()),
        "yearRange": [lo_year, hi_year],
        "charts": {
            "timeline": timeline,
            "month": months,
            "hour": hours,
            "brand": counts("brand", skip="brand"),
            "camera": counts("camera", skip="camera"),
            "lens": counts("lens", skip="lens"),
            "ext": counts("ext", skip="ext", labeller=lambda v: str(v).upper()),
            "focal": bucket_counts("focal"),
            "aperture": bucket_counts("aperture"),
            "exposure": bucket_counts("exposure"),
            "iso": bucket_counts("iso"),
            "size": bucket_counts("size"),
            "color": color_counts(),
            "colorCenter": color_counts("color_center"),
            "tone": derived_counts("tone"),
            "brightness": bucket_counts("brightness"),
            "contrast": bucket_counts("contrast"),
            "clipping": derived_counts("clipping"),
            "sharpCenter": bucket_counts("sharp_center"),
            "sharpEdge": bucket_counts("sharp_edge"),
            "focus": derived_counts("focus"),
            "dup": derived_counts("dup"),
        "rating": rating_counts(),
        "tags": tag_counts(),
        },
    })


@app.get("/api/timeline")
def api_timeline():
    """The monthly share of the chosen camera or lens.

    The run of months is taken from the whole archive rather than from the years
    the camera itself lived: that shows where it falls in the overall history and
    what came before and after. The share is counted against every photo of that
    month, so a thin month does not look like a lull in use."""
    dim = request.args.get("dim", "camera")
    if dim not in ("camera", "lens", "brand"):
        abort(400)
    top = max(1, min(int(request.args.get("top", 10)), 60))
    span_mode = request.args.get("span", "active")   # active — the period of use only
    lo_year, hi_year = year_range()
    good = lambda ym: ym and lo_year <= int(ym[:4]) <= hi_year

    # denominator: the whole archive under the other filters, the gear itself aside
    where_all, params_all = build_where(skip=(dim, "ym", "year"))
    # numerator: the same, but the gear filter stays — the panel shows the choice
    where_sel, params_sel = build_where(skip=("ym", "year"))
    with closing(db()) as con, con:
        per_month = {ym: c for ym, c in con.execute(
            f"SELECT ym, COUNT(*) FROM visible_photos AS photos {where_all} GROUP BY ym", params_all)
            if good(ym)}
        raw = [(ym, k, c) for ym, k, c in con.execute(
            f"SELECT ym, {dim} AS k, COUNT(*) FROM visible_photos AS photos {where_sel} "
            f"GROUP BY ym, k", params_sel) if good(ym)]
        # exact dates of the first and last frame — by capture date, not by month
        dates = {(k or NONE_LABEL): (a, b) for k, a, b in con.execute(
            f"SELECT {dim} AS k, MIN(taken), MAX(taken) FROM visible_photos AS photos {where_sel} "
            f"WHERE year BETWEEN ? AND ? GROUP BY k"
            if not where_sel else
            f"SELECT {dim} AS k, MIN(taken), MAX(taken) FROM visible_photos AS photos {where_sel} "
            f"AND year BETWEEN ? AND ? GROUP BY k",
            params_sel + [lo_year, hi_year])}

    if not per_month:
        return jsonify({"months": [], "totals": [], "rows": [],
                        "shown": 0, "available": 0})

    y0, m0 = (int(x) for x in min(per_month).split("-"))
    y1, m1 = (int(x) for x in max(per_month).split("-"))
    months, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    totals = [per_month.get(ym, 0) for ym in months]

    by_key = {}
    for ym, k, cnt in raw:
        by_key.setdefault(k or NONE_LABEL, {})[ym] = cnt
    ranked = sorted(by_key.items(), key=lambda kv: -sum(kv[1].values()))
    chosen = ranked[:top]
    chosen.sort(key=lambda kv: min(kv[1]))          # by when it first appeared
    rows = []
    for name, hits in chosen:
        first, last = dates.get(name, (None, None))
        days = None
        if first and last:
            d0 = datetime.date.fromisoformat(first[:10])
            d1 = datetime.date.fromisoformat(last[:10])
            days = (d1 - d0).days + 1          # both end days count as days in service
        rows.append({
            "k": name,
            "total": sum(hits.values()),
            "first": min(hits), "last": max(hits),
            "firstDate": first, "lastDate": last, "days": days,
            "activeMonths": len(hits),
            "v": [hits.get(ym, 0) for ym in months],
        })
    # By default the strip is cut down to the period of use: for a camera that
    # lived a year, the other two decades are empty cells with nothing to read in
    # them. Some padding is left so the start and end do not sit against the edge.
    archive_months = len(months)
    if span_mode == "active" and rows:
        used = [i for i in range(len(months))
                if any(r["v"][i] for r in rows)]
        if used:
            a = max(0, used[0] - TIMELINE_PAD)
            b = min(len(months), used[-1] + TIMELINE_PAD + 1)
            months, totals = months[a:b], totals[a:b]
            for r in rows:
                r["v"] = r["v"][a:b]

    return jsonify({"months": months, "totals": totals, "rows": rows,
                    "shown": len(rows), "available": len(ranked),
                    "span": span_mode, "archiveMonths": archive_months})


# The clustering grid: the smaller the cell, the finer the split. Longitude is
# squeezed towards the poles, so its cell is wider — otherwise, at the latitude of
# Moscow, clusters would be drawn out into narrow strips.
GEO_CELLS = [10, 4, 2, 1, 0.4, 0.2, 0.08, 0.03, 0.012, 0.005, 0.002]

TIMELINE_PAD = 6      # how many months of padding to leave at the ends of the period of use


@app.get("/api/geo")
def api_geo():
    """Clusters of coordinates. They are worked out on the server: there is no
    point sending a hundred thousand points to the browser, and it would take
    noticeably longer to draw them."""
    level = max(0, min(int(request.args.get("level", 3)), len(GEO_CELLS) - 1))
    cell = GEO_CELLS[level]
    limit = max(1, min(int(request.args.get("limit", 400)), 2000))

    where, params = build_where(skip="geo")
    cond = "lat IS NOT NULL AND lon IS NOT NULL"
    where = f"{where} AND {cond}" if where else f"WHERE {cond}"
    with closing(db()) as con, con:
        total, = con.execute(f"SELECT COUNT(*) FROM visible_photos AS photos {where}", params).fetchone()
        rows = con.execute(
            f"SELECT CAST(lat / ? AS INT) AS gy, CAST(lon / ? AS INT) AS gx,"
            f" COUNT(*) AS c, AVG(lat), AVG(lon),"
            f" MIN(lat), MAX(lat), MIN(lon), MAX(lon),"
            f" MIN(taken), MAX(taken)"
            f" FROM visible_photos AS photos {where} GROUP BY gy, gx ORDER BY c DESC LIMIT ?",
            [cell, cell] + params + [limit]).fetchall()
        with_geo, = con.execute(
            "SELECT COUNT(*) FROM visible_photos AS photos WHERE lat IS NOT NULL").fetchone()
        all_photos, = con.execute("SELECT COUNT(*) FROM visible_photos AS photos").fetchone()

    clusters = [{
        "n": r[2],
        "lat": round(r[3], 6), "lon": round(r[4], 6),
        "box": [round(r[5], 6), round(r[7], 6), round(r[6], 6), round(r[8], 6)],
        "first": r[9], "last": r[10],
    } for r in rows]
    return jsonify({"clusters": clusters, "cell": cell, "level": level,
                    "shown": sum(c["n"] for c in clusters), "total": total,
                    "withGeo": with_geo, "allPhotos": all_photos,
                    "levels": len(GEO_CELLS)})


# A list, not a string: the direction has to be attached to every column. In
# "ORDER BY filename, path DESC" only the path gets the reverse order, the name
# stays ascending — and the sort silently does not work.
SORTS = {
    "taken": ["taken"],
    "name": ["filename COLLATE NOCASE", "path"],
    "size": ["size"],
    "camera": ["camera", "taken"],
    "folder": ["folder COLLATE NOCASE", "filename COLLATE NOCASE"],
    "rating": [rating_rank(), "taken"],
    # By the sharper of the two regions: a portrait with its background out of
    # focus is not a soft photo, and sorting by the edges would put it among
    # them. Unmeasured photos count as -1, so they gather at one end.
    "sharpness": ["COALESCE(MAX(sharp_center, sharp_edge), -1)", "taken"],
    # each region on its own, for when it is the subject or the background
    # that matters — the softest centres are where missed focus is found
    "sharp_center": ["COALESCE(sharp_center, -1)", "taken"],
    "sharp_edge": ["COALESCE(sharp_edge, -1)", "taken"],
}


def photo_records(con, rows, details=True):
    items = [dict(row) for row in rows]
    if details:
        # Full cards opened from lists and Timeline get the same companions.
        # One indexed batch avoids per-photo lookups and keeps paths off thumbnails.
        companions = raw_pairs.files_for(con, [item["id"] for item in items if item["has_raw"]])
        for item in items:
            item["raw_files"] = companions.get(item["id"], [])
    return items


def timeline_columns(details=False):
    if not details:
        # Tooltips and annotation badges need no request per hovered thumbnail.
        # Portfolio uses the whole-frame color and the same sharpness bands as Statistics.
        return ("id, filename, taken, width, height, ext, has_raw, rating, mark, camera, lens, focal, focal35, fnumber,"
                f" iso, shutter, comment, color_hex, {SHARP_LABEL_COLS}, {TAG_COL}")
    return (
        "id, path, filename, folder, taken, year, date_src, brand, camera, lens,"
        " focal, focal35, iso, fnumber, shutter, exposure, width, height, size, ext, has_raw,"
        f" lat, lon, rating, mark, comment, {TAG_COL},"
        " color, color_hex, color_share, color_center, color_center_hex,"
        " color_center_share, brightness, contrast, chroma, clip_hi, clip_lo,"
        f" {SHARP_COLS}")


TIMELINE_EXTRA_FILTERS = {
    "year": ("year", None), "camera": ("camera", None), "lens": ("lens", None),
    "focal": ("focal35", FOCAL_BUCKETS), "aperture": ("fnumber", APERTURE_BUCKETS),
}


def timeline_extra_selection():
    fields, values = request.args.getlist("extra_type"), request.args.getlist("extra_value")
    if len(fields) > 1 or len(values) > 1:
        abort(400, description="Choose one Timeline filter and one option")
    field, value = (fields or [""])[0], (values or [""])[0]
    if not field:
        if value:
            abort(400, description="Choose a Timeline filter before its option")
        return "", ""
    if field not in TIMELINE_EXTRA_FILTERS:
        abort(400, description="Unknown Timeline filter")
    if not value or value == NONE_KEY:
        return field, value
    buckets = TIMELINE_EXTRA_FILTERS[field][1]
    if buckets and value not in {f"{lo}-{hi}" for lo, hi, _ in buckets}:
        abort(400, description="Unknown Timeline range")
    if field == "year" and (not value.isascii() or not value.isdigit() or
                            len(value) > 4 or not 1 <= int(value) <= 9999):
        abort(400, description="Invalid Timeline year")
    if len(value) > 1024 or "\x00" in value:
        abort(400, description="Invalid Timeline option")
    return field, value


def timeline_filter(default_mode="good", include_extra=True):
    """One predicate for dates, counts, highlights, expanded pages and cards."""
    mode = request.args.get("rating_mode", default_mode)
    unrated = request.args.get("unrated", "1")
    if mode not in ("all", "good", "great") or unrated not in ("0", "1"):
        abort(400, description="Unknown Timeline rating options")
    key = rating_key()
    if mode == "all":
        rating = "1" if unrated == "1" else f"{key} IS NOT NULL"
    else:
        rating = f"{key} IN ('great', '5', '4')" if mode == "good" else f"{key} = 'great'"
        if unrated == "1":
            rating = f"({key} IS NULL OR {rating})"
    raw = request.args.getlist("tag")
    wanted = list(dict.fromkeys(tags.normalize(value) for value in raw))
    if None in wanted or len(wanted) > 100:
        abort(400, description="Invalid Timeline tags")
    predicate = f"({rating})"
    if wanted:
        # A global photo_id IN (...) encourages rowid scans and repeated date
        # sorts for every discovered day. Correlating the small per-photo tag
        # lookup lets the outer query retain its capture-time range search.
        predicate += (" AND EXISTS (SELECT 1 FROM photo_tags AS timeline_tags "
                      "WHERE timeline_tags.photo_id = photos.id "
                      f"AND timeline_tags.tag IN ({','.join('?' for _ in wanted)}))")
    field, value = timeline_extra_selection()
    params = list(wanted)
    if include_extra and field and value:
        column, buckets = TIMELINE_EXTRA_FILTERS[field]
        # Keep the outer Timeline scan ordered by taken. A broad focal/aperture
        # range otherwise made SQLite rescan its metric index and sort for
        # each day (about 280 ms for eight days on a 100k-row fixture).
        expression = f"timeline_extra.{column}"
        if value == NONE_KEY:
            condition = f"{expression} IS NULL"
        elif buckets:
            lo, hi = next((lo, hi) for lo, hi, _ in buckets if value == f"{lo}-{hi}")
            condition = f"{expression} >= ? AND {expression} < ?"
            params.extend((lo, hi))
        else:
            condition = f"{expression} = ?"
            params.append(int(value) if field == "year" else value)
        predicate += (" AND EXISTS (SELECT 1 FROM visible_photos AS timeline_extra "
                      f"WHERE timeline_extra.id = photos.id AND {condition})")
    return predicate, params


def timeline_option_search(value):
    return unicodedata.normalize("NFC", str(value)).casefold().replace("–", "-").replace("—", "-")


@app.get("/api/timeline/options")
def api_timeline_options():
    """Bounded, searchable choices under tag/rating filters, excluding their own selection."""
    fields = request.args.getlist("field")
    if len(fields) != 1 or fields[0] not in TIMELINE_EXTRA_FILTERS:
        abort(400, description="Unknown Timeline option list")
    field = fields[0]
    try:
        offset = int(request.args.get("offset", 0))
        limit = int(request.args.get("limit", 30))
        if offset < 0 or not 1 <= limit <= 40:
            raise ValueError
    except ValueError:
        abort(400, description="Invalid Timeline option page")
    query = timeline_option_search(request.args.get("q", "").strip())
    if len(query) > 200:
        abort(400, description="Option search is too long")
    predicate, params = timeline_filter(include_extra=False)
    column, buckets = TIMELINE_EXTRA_FILTERS[field]
    with closing(db()) as con:
        if buckets:
            case = " ".join(f"WHEN {column} >= {lo} AND {column} < {hi} THEN '{lo}-{hi}'"
                            for lo, hi, _ in buckets)
            expression = f"CASE WHEN {column} IS NULL THEN '{NONE_KEY}' {case} END"
            counts = dict(con.execute(f"SELECT {expression} AS k, COUNT(*) FROM visible_photos AS photos "
                                      f"WHERE {predicate} GROUP BY k", params))
            choices = [(f"{lo}-{hi}", label) for lo, hi, label in buckets] + [(NONE_KEY, NONE_LABEL)]
            matches = [{"k": key, "l": label, "n": counts[key]} for key, label in choices
                       if counts.get(key) and query in timeline_option_search(label)]
            items = matches[offset:offset + limit + 1]
        else:
            search = ""
            if query:
                # SQLite's lower() does not fold Cyrillic. A bound substring
                # predicate also treats literal %, _ and ! in lens names safely.
                con.create_function("timeline_contains", 1,
                                    lambda value: query in timeline_option_search(value if value is not None else NONE_LABEL),
                                    deterministic=True)
                search = "WHERE timeline_contains(value)"
            order = "value DESC" if field == "year" else "value COLLATE NOCASE, value"
            # Materialise grouped choices before Unicode search, so a long
            # camera list is searched once per model, not once per photograph.
            # The identity CASE avoids grouping via the model/lens index:
            # its random row lookups cost more than a scan under tag filters.
            group = column if field == "year" else f"CASE WHEN {column} IS NULL THEN NULL ELSE {column} END"
            rows = con.execute(f"WITH choices AS MATERIALIZED (SELECT {column} AS value, COUNT(*) AS n "
                               f"FROM visible_photos AS photos WHERE {predicate} GROUP BY {group}) "
                               f"SELECT value, n FROM choices {search} "
                               f"ORDER BY value IS NULL, {order} LIMIT ? OFFSET ?",
                               [*params, limit + 1, offset]).fetchall()
            items = [{"k": NONE_KEY if key is None else str(key),
                      "l": NONE_LABEL if key is None else str(key), "n": count} for key, count in rows]
    more = len(items) > limit
    return jsonify({"items": items[:limit], "next": offset + limit if more else None})


def timeline_seed(default=None):
    seed = request.args.get("seed") or default or secrets.token_hex(12)
    if len(seed) > 64:
        abort(400, description="Seed is too long")
    return seed


def timeline_order(con, seed):
    con.create_function("timeline_pick", 1, lambda pid: md5(
        f"{seed}:{pid}".encode("utf-8")).hexdigest(), deterministic=True)
    return (f"{rating_rank()} DESC, CASE WHEN {rating_key()} IS NULL "
            "THEN timeline_pick(id) END, taken, id")


@app.get("/api/timeline/day")
def api_timeline_day():
    """Page matching files in rating order, with a stable seeded unrated order.

    An unparameterised request retains the original all-ratings day API.
    The Timeline UI always sends its independent rating/tag options explicitly.
    """
    day = request.args.get("day")
    lo, hi = day_bounds(day)
    predicate, params = timeline_filter(default_mode="all")
    seed = timeline_seed(default="timeline-day")
    try:
        offset = max(0, int(request.args.get("offset", 0)))
        limit = max(1, min(int(request.args.get("limit", 200)), 500))
        photo = int(request.args["photo"]) if "photo" in request.args else None
    except ValueError:
        abort(400, description="Invalid photo page")
    with closing(db()) as con:
        con.execute("BEGIN")
        order = timeline_order(con, seed)
        where = f"taken >= ? AND taken < ? AND {predicate}"
        values = [lo, hi, *params]
        total = con.execute(f"SELECT COUNT(*) FROM visible_photos AS photos WHERE {where}", values).fetchone()[0]
        columns = timeline_columns(details=photo is not None)
        if photo is not None:
            rows = con.execute(f"SELECT {columns} FROM visible_photos AS photos"
                               f" WHERE {where} AND id = ?", [*values, photo]).fetchall()
            if not rows:
                abort(404, description="No photo for this day")
        else:
            rows = con.execute(f"SELECT {columns} FROM visible_photos AS photos"
                               f" WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                               [*values, limit, offset]).fetchall()
        items = photo_records(con, rows, details=photo is not None)
    following = offset + len(rows)
    return jsonify({"day": day, "total": total, "items": items,
                    "next": following if photo is None and rows and following < total else None})


@app.get("/api/timeline/photos")
def api_timeline_photos():
    """A day cursor with bounded highlight candidates (15 by default).

    Discover days through ix_taken rather than grouping the remaining archive
    on every scroll. A seed gives unrated files a stable random order when a day
    is revisited or its full photo cards are requested later.
    """
    direction = request.args.get("dir", "desc")
    if direction not in ("asc", "desc"):
        abort(400, description="Unknown date order")
    try:
        limit = max(1, min(int(request.args.get("limit", 8)), 20))
        photo_limit = max(15, min(int(request.args.get("photo_limit", 15)), 500))
    except ValueError:
        abort(400, description="Invalid day limit")
    seed = timeline_seed()
    cursor = request.args.get("cursor")
    single = request.args.get("day")
    if cursor and single:
        abort(400, description="Choose a day or a cursor")
    lower, upper = "0001-01-01", "9999-12-32"
    if cursor:
        lo, hi = day_bounds(cursor)
        if direction == "asc":
            lower = hi
        else:
            upper = lo
    if single:
        lower, upper = day_bounds(single)
    eligible, filter_params = timeline_filter()
    order = "ASC" if direction == "asc" else "DESC"
    days = []
    with closing(db()) as con:
        # All queries in a page see the same committed scanner/rating state.
        con.execute("BEGIN")
        photo_order = timeline_order(con, seed)
        dates = []
        while len(dates) <= (1 if single else limit):
            row = con.execute(
                f"SELECT taken FROM visible_photos AS photos WHERE taken >= ? AND taken < ?"
                f" AND date(taken) IS NOT NULL AND {eligible}"
                f" ORDER BY taken {order} LIMIT 1", [lower, upper, *filter_params]).fetchone()
            if not row:
                break
            day = row["taken"][:10]
            try:
                datetime.date.fromisoformat(day)
            except ValueError:
                # Some old EXIF dates pass SQLite's permissive date parser but
                # are not calendar days (for example February 30).
                if direction == "asc":
                    lower = day + "\uffff"
                else:
                    upper = day
                continue
            lo, hi = day_bounds(day)
            dates.append(day)
            if single:
                break
            if direction == "asc":
                lower = hi
            else:
                upper = lo
        more = not single and len(dates) > limit
        for day in dates[:limit]:
            lo, hi = day_bounds(day)
            total, count = con.execute(
                f"SELECT COUNT(*), COALESCE(SUM(CASE WHEN {eligible} THEN 1"
                " ELSE 0 END), 0) FROM visible_photos AS photos WHERE taken >= ? AND taken < ?",
                [*filter_params, lo, hi]).fetchone()
            ids = [r[0] for r in con.execute(
                f"SELECT id FROM visible_photos AS photos WHERE taken >= ? AND taken < ? AND {eligible}"
                f" ORDER BY {photo_order} LIMIT ?", [lo, hi, *filter_params, photo_limit])]
            details = request.args.get("details") == "1"
            columns = timeline_columns(details=details)
            rows = con.execute(
                f"SELECT {columns} FROM visible_photos AS photos WHERE id IN ({','.join('?' for _ in ids)})", ids).fetchall()
            records = {item["id"]: item for item in photo_records(con, rows, details=details)}
            days.append({"day": day, "total": count, "archive_total": total, "eligible": count,
                         "items": [records[pid] for pid in ids]})
            if "photo_limit" in request.args:
                # Selection priority is separate from the client's capture-time
                # display order, including when it fills a row beyond 15 photos.
                days[-1]["highlight_ids"] = ids
    return jsonify({"days": days, "next": days[-1]["day"] if more else None,
                    "seed": seed, "dir": direction, "files": files_here(), "version": VERSION})


@app.route("/api/timeline/day/tags", methods=["GET", "POST"])
def api_timeline_day_tags():
    """Preview or atomically edit tags on the filtered day, beyond display limits."""
    operation = request.args.get("operation", "add")
    if operation not in ("add", "remove"):
        return jsonify({"ok": False, "error": "Choose add or remove."}), 400
    removing = operation == "remove"
    day = request.args.get("day")
    lo, hi = day_bounds(day)
    predicate, params = timeline_filter()
    source = request.form if request.method == "POST" else request.args
    wanted = []
    for raw in source.getlist("remove_tag" if removing else "add_tag"):
        tag = tags.normalize(raw)
        if tag is None:
            return jsonify({"ok": False, "error": tags.rule_text()}), 400
        if tag not in wanted:
            wanted.append(tag)
    if not removing and len(wanted) > tags.MAX_TAGS:
        return jsonify({"ok": False, "error": f"Choose up to {tags.MAX_TAGS} tags."}), 400
    expected_revision = None
    if request.method == "POST":
        if not wanted:
            return jsonify({"ok": False, "error": "Choose at least one tag."}), 400
        try:
            expected_revision = int(source["revision"])
            if expected_revision < 0:
                raise ValueError
        except (KeyError, ValueError):
            return jsonify({"ok": False, "error": "Preview the selection before changing tags."}), 400
    with closing(db()) as con, con:
        con.execute("BEGIN IMMEDIATE" if request.method == "POST" else "BEGIN")
        revision = con.execute("SELECT revision FROM db_state WHERE id=1").fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM visible_photos AS photos WHERE taken >= ? AND taken < ?", (lo, hi)).fetchone()[0]
        # Freeze membership before editing: removing a filter tag must not
        # shrink the set halfway through a multi-tag removal.
        con.execute("CREATE TEMP TABLE timeline_tag_targets (photo_id INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO timeline_tag_targets SELECT id FROM visible_photos AS photos "
                    f"WHERE taken >= ? AND taken < ? AND {predicate}", [lo, hi, *params])
        con.execute("CREATE TEMP TABLE timeline_tag_selection (tag TEXT PRIMARY KEY)")
        con.executemany("INSERT INTO timeline_tag_selection VALUES (?)", [(tag,) for tag in wanted])
        counts = (
            "WITH counts AS (SELECT targets.photo_id, COUNT(pt.tag) AS existing, "
            "COALESCE(SUM(pt.tag IN (SELECT tag FROM timeline_tag_selection)),0) AS retained "
            "FROM timeline_tag_targets AS targets LEFT JOIN photo_tags AS pt "
            "ON pt.photo_id=targets.photo_id GROUP BY targets.photo_id) ")
        if removing:
            matched, changed, removals = con.execute(
                counts + "SELECT COUNT(*), COALESCE(SUM(retained > 0),0), COALESCE(SUM(retained),0) FROM counts"
            ).fetchone()
            additions = conflicts = 0
        else:
            matched, changed, additions, conflicts = con.execute(
                counts + "SELECT COUNT(*), COALESCE(SUM(?-retained > 0),0), COALESCE(SUM(?-retained),0), "
                "COALESCE(SUM(existing+?-retained > ?),0) FROM counts",
                [len(wanted), len(wanted), len(wanted), tags.MAX_TAGS]).fetchone()
            removals = 0
        result = {"day": day, "total": total, "matched": matched, "hidden": total - matched,
                  "changed": changed, "additions": additions, "conflicts": conflicts,
                  "max_tags": tags.MAX_TAGS, "tags": wanted, "revision": revision,
                  "operation": operation, "removals": removals}
        if removing:
            result["available"] = [dict(row) for row in con.execute(
                "SELECT pt.tag, COUNT(*) AS n FROM timeline_tag_targets AS targets "
                "JOIN photo_tags AS pt ON pt.photo_id=targets.photo_id GROUP BY pt.tag ORDER BY n DESC, pt.tag")]
        if request.method == "GET":
            return jsonify({"ok": True, **result})
        if revision != expected_revision:
            return jsonify({"ok": False, **result, "reason": "revision",
                            "error": "The archive changed. Review the updated counts and press OK again."}), 409
        if conflicts:
            return jsonify({"ok": False, **result, "reason": "capacity",
                            "error": f"Adding these tags would exceed {tags.MAX_TAGS} tags on {conflicts} photos. "
                                     "No photos were changed. Choose fewer tags."}), 409
        if not matched:
            return jsonify({"ok": False, **result, "reason": "empty",
                            "error": "No photos match these filters. No photos were changed."}), 409
        if removing:
            con.execute("DELETE FROM photo_tags WHERE photo_id IN (SELECT photo_id FROM timeline_tag_targets) "
                        "AND tag IN (SELECT tag FROM timeline_tag_selection)")
        else:
            for tag in wanted:
                con.execute("INSERT OR IGNORE INTO photo_tags(photo_id,tag) "
                            "SELECT photo_id,? FROM timeline_tag_targets", (tag,))
        result["revision"] = con.execute("SELECT revision FROM db_state WHERE id=1").fetchone()[0]
        return jsonify({"ok": True, **result})


@app.get("/api/directories")
def api_directories():
    """The whole indexed archive, independent of the statistics filters.

    Group files once, then roll each directory into its ancestors. A flat result
    avoids JSON nesting limits for deep archives and needs no per-folder query.
    """
    with closing(db()) as con:
        rows = con.execute(
            "SELECT folder, COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes,"
            " SUM(CASE WHEN color IS NOT NULL AND color != ?"
            " AND brightness IS NOT NULL AND sharp_center IS NOT NULL"
            " AND sharp_edge IS NOT NULL THEN 1 ELSE 0 END) AS analyzed"
            " FROM visible_photos AS photos GROUP BY folder", (COLOR_UNKNOWN,)).fetchall()
    nodes = {}
    total = {"n": 0, "analyzed": 0, "bytes": 0}
    for row in rows:
        for key in total:
            total[key] += row[key]
        path = os.path.normpath(row["folder"]) if row["folder"] else ""
        while True:
            parent = os.path.dirname(path)
            parent = parent if parent and parent != path else None
            node = nodes.setdefault(path, {
                "path": path, "parent": parent,
                "name": os.path.basename(path) or path or "Unknown folder",
                "n": 0, "analyzed": 0, "bytes": 0,
            })
            for key in total:
                node[key] += row[key]
            if parent is None:
                break
            path = parent
    return jsonify({"total": total, "items": sorted(
        nodes.values(), key=lambda node: (node["path"].casefold(), node["path"]))})


@app.get("/api/folders")
def api_folders():
    """The folders the photos of the current selection live in."""
    where, params = build_where()
    limit = max(1, min(int(request.args.get("limit", 300)), 2000))
    with closing(db()) as con, con:
        rows = con.execute(
            f"SELECT folder, COUNT(*) AS n, MIN(taken), MAX(taken) FROM visible_photos AS photos"
            f" {where} GROUP BY folder ORDER BY n DESC, folder LIMIT ?",
            params + [limit]).fetchall()
        total, = con.execute(
            f"SELECT COUNT(DISTINCT folder) FROM visible_photos AS photos {where}", params).fetchone()
    return jsonify({"total": total, "items": [
        {"folder": r[0] or "", "n": r[1], "first": r[2], "last": r[3]}
        for r in rows]})


@app.get("/api/photos")
def api_photos():
    where, params = build_where()
    # The order is taken only from this list: the value comes from the client and
    # goes straight into SQL, so it cannot be substituted as it arrives.
    field = request.args.get("sort", "taken")
    if field not in SORTS:
        field = "taken"
    # when filtering by duplicates, sort by name by default so copies sit next to
    # each other and can be compared; an explicit choice still overrides this
    if request.args.get("dup") == "has duplicates" and "sort" not in request.args:
        field = "name"
    order = "DESC" if request.args.get("dir") == "desc" else "ASC"
    limit = min(int(request.args.get("limit", 120)), 500)
    offset = int(request.args.get("offset", 0))
    with closing(db()) as con, con:
        con.execute("BEGIN")
        total, = con.execute(f"SELECT COUNT(*) FROM visible_photos AS photos {where}", params).fetchone()
        rows = con.execute(
            f"SELECT id, path, filename, folder, taken, date_src, brand, camera, lens,"
            f" focal, focal35, iso, fnumber, shutter, exposure, width, height, size, ext, has_raw,"
            f" lat, lon, rating, mark, comment, {TAG_COL},"
            f" (SELECT COUNT(*) FROM visible_photos AS d"
            f"  WHERE {dup_key('d')} = {dup_key('photos')}) AS dup_n,"
            f" color, color_hex, color_share, color_center, color_center_hex,"
            f" color_center_share, brightness, contrast, chroma,"
            f" clip_hi, clip_lo, {SHARP_COLS}"
            f" FROM visible_photos AS photos {where} "
            f"ORDER BY {', '.join(c + ' ' + order for c in SORTS[field])},"
            f" id LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        items = photo_records(con, rows)
    return jsonify({"total": total, "offset": offset,
                    "items": items})


@app.get("/api/random")
def api_random():
    """A few random photos from the current selection, for the preview strip."""
    where, params = build_where()
    n = max(1, min(int(request.args.get("n", 10)), 30))
    with closing(db()) as con, con:
        con.execute("BEGIN")
        rows = con.execute(
            f"SELECT id, path, filename, folder, taken, date_src, brand, camera,"
            f" lens, focal, focal35, iso, fnumber, shutter, exposure, width, height,"
            f" size, ext, has_raw, lat, lon, rating, mark, comment, {TAG_COL},"
            f" color, color_hex, color_share, color_center,"
            f" color_center_hex, color_center_share, brightness, contrast,"
            f" chroma, clip_hi, clip_lo, {SHARP_COLS}"
            f" FROM visible_photos AS photos {where} ORDER BY RANDOM() LIMIT ?", params + [n]).fetchall()
        items = photo_records(con, rows)
    return jsonify({"items": items})


@app.get("/api/thumb/<int:pid>")
def api_thumb(pid):
    # The ceiling is what the whole-window view in the photo card asks for on a
    # dense display. It used to be 1400, which is plenty for the card itself but
    # visibly soft once the picture fills a 2560-pixel screen.
    size = max(64, min(int(request.args.get("s", 260)), 2800))
    with closing(db()) as con, con:
        row = con.execute("SELECT path, mtime FROM visible_photos AS photos "
                          "WHERE id=?", (pid,)).fetchone()
    if not row:
        abort(404)
    path = row["path"]
    if not os.path.isfile(path):
        abort(404)
    tag = md5(f"{path}|{row['mtime']}|{size}".encode("utf-8")).hexdigest()
    cached = os.path.join(CACHE_DIR, tag[:2], tag + ".jpg")
    if os.path.exists(cached):
        return send_file(cached, mimetype="image/jpeg")

    data = None
    try:
        from PIL import Image, ImageOps
        try:
            import pillow_heif  # optional, for HEIC
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        with Image.open(path) as im:
            try:
                im.draft("RGB", (size * 2, size * 2))   # the fast path for JPEG
            except Exception:
                pass
            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size), Image.LANCZOS)
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=82, optimize=True)
            data = buf.getvalue()
    except Exception:
        data = raw_preview(path, size)

    if not data:
        abort(415)
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    with open(cached, "wb") as f:
        f.write(data)
    return Response(data, mimetype="image/jpeg")


def raw_preview(path, size):
    """For RAW, pull the embedded JPEG preview out with ExifTool."""
    import shutil as sh
    exe = sh.which("exiftool") or sh.which("exiftool.exe")
    if not exe:
        local = os.path.join(APP_DIR, "exiftool.exe")
        exe = local if os.path.isfile(local) else None
    if not exe:
        return None
    for tag in ("-JpgFromRaw", "-PreviewImage", "-ThumbnailImage"):
        try:
            out = subprocess.run([exe, "-b", tag, path], capture_output=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if out.stdout and len(out.stdout) > 1000:
                from PIL import Image, ImageOps
                with Image.open(io.BytesIO(out.stdout)) as im:
                    im = ImageOps.exif_transpose(im)
                    im.thumbnail((size, size), Image.LANCZOS)
                    buf = io.BytesIO()
                    im.convert("RGB").save(buf, "JPEG", quality=82)
                    return buf.getvalue()
        except Exception:
            continue
    return None


@app.post("/api/mark/<int:pid>")
def api_mark(pid):
    """Set or clear your own rating. The only write the interface ever makes.

    An empty value clears the mark, which is not the same as rating something
    zero: it hands the photo back to whatever EXIF said.
    """
    value = (request.args.get("value") or "").strip()
    if value and value not in MARKS:
        return jsonify({"ok": False, "error": "Unknown rating"}), 400
    with closing(db()) as con, con:
        changed = con.execute("UPDATE photos SET mark = ? WHERE id = ? AND EXISTS "
                              "(SELECT 1 FROM visible_photos AS v WHERE v.id = photos.id)",
                              (value or None, pid)).rowcount
        con.commit()
        if not changed:
            return jsonify({"ok": False, "error": "No such photo"}), 404
        row = con.execute("SELECT rating, mark FROM visible_photos AS photos WHERE id = ?",
                          (pid,)).fetchone()
    return jsonify({"ok": True, "rating": row["rating"], "mark": row["mark"]})


# Short on purpose: a caption, not a diary. Counted in characters as Python
# counts them, after NFC, so "й" typed either way is one character, and an
# emoji is one even though a browser's `maxlength` would count it as two.
COMMENT_MAX = 256


def clean_comment(raw):
    """The comment as stored, or None if there is nothing left of it.

    One line: the field in the card saves on Enter, and a line break pasted in
    from somewhere else would otherwise survive only to be flattened by every
    place that shows the comment in a single row. Other control characters are
    dropped rather than refused — they are invisible, and a comment refused for
    something the user cannot see is a comment that cannot be fixed.
    """
    s = unicodedata.normalize("NFC", raw or "")
    s = "".join(" " if c in "\r\n\t" else c for c in s
                if c in "\r\n\t" or unicodedata.category(c) != "Cc")
    return " ".join(s.split()) or None


@app.post("/api/comment/<int:pid>")
def api_comment(pid):
    """Set or clear the comment on one photo. Empty text clears it.

    The text travels in the body, not the query string like the rating does:
    256 characters of Cyrillic are over four kilobytes once percent-encoded.
    """
    text = clean_comment(request.form.get("text"))
    if text and len(text) > COMMENT_MAX:
        return jsonify({"ok": False, "error": f"A comment is at most "
                        f"{COMMENT_MAX} characters; this one is {len(text)}"}), 400
    with closing(db()) as con, con:
        changed = con.execute("UPDATE photos SET comment = ? WHERE id = ? AND EXISTS "
                              "(SELECT 1 FROM visible_photos AS v WHERE v.id = photos.id)",
                              (text, pid)).rowcount
        con.commit()
    if not changed:
        return jsonify({"ok": False, "error": "No such photo"}), 404
    return jsonify({"ok": True, "comment": text})


@app.post("/api/tags/<int:pid>")
def api_set_tags(pid):
    """Replace the whole set of tags on one photo: tag=sea&tag=kids.

    The whole set rather than one added or removed at a time. Two windows open on
    the same photo cannot then race each other into a half-applied state, and
    sending no tag at all clears them, which needs no verb of its own.
    """
    wanted = []
    for raw in request.args.getlist("tag"):
        tag = tags.normalize(raw)
        if tag is None:
            return jsonify({"ok": False,
                            "error": f"“{raw}” is not a tag. "
                                     f"{tags.rule_text()}."}), 400
        if tag not in wanted:            # the same tag twice is one tag, not an error
            wanted.append(tag)
    if len(wanted) > tags.MAX_TAGS:
        return jsonify({"ok": False,
                        "error": f"Up to {tags.MAX_TAGS} tags per photo."}), 400
    with closing(db()) as con, con:
        if not con.execute("SELECT 1 FROM visible_photos AS photos WHERE id = ?", (pid,)).fetchone():
            return jsonify({"ok": False, "error": "No such photo"}), 404
        con.execute("DELETE FROM photo_tags WHERE photo_id = ?", (pid,))
        con.executemany("INSERT INTO photo_tags (photo_id, tag) VALUES (?, ?)",
                        [(pid, t) for t in wanted])
        con.commit()
    return jsonify({"ok": True, "tags": wanted})


@app.get("/api/job")
def api_job():
    """The last pass started, for the status line at the top of the page.

    Polled every few seconds, so it is one row by primary key and nothing else.
    The scanner writes it (jobs.py); whether a silent "running" means stopped is
    decided there too, so the page never has to guess from a clock of its own —
    a tablet's clock need not agree with this computer's.
    """
    with closing(db()) as con, con:
        try:
            job = jobs.latest(con)
        except sqlite3.OperationalError:     # the table is not there yet
            job = None
    return jsonify({"job": job})


@app.get("/api/tags")
def api_tags():
    """The tags already in use, for the suggestions under the input.

    Over the whole archive, not the current selection. A tag that exists only
    outside the filter would otherwise not be offered, and the same word would be
    typed in a second time as a tag of its own — the one thing a vocabulary is
    there to prevent.
    """
    q = tags.prefix(request.args.get("q"))
    limit = max(1, min(int(request.args.get("limit", 20)), 100))
    with closing(db()) as con, con:
        rows = con.execute(
            "SELECT tag, COUNT(*) AS n FROM photo_tags "
            "JOIN visible_photos AS photos ON photos.id = photo_tags.photo_id WHERE tag LIKE ?"
            " GROUP BY tag ORDER BY (tag LIKE ?) DESC, n DESC, tag LIMIT ?",
            (f"%{q}%", f"{q}%", limit)).fetchall()
    return jsonify({"items": [{"tag": r["tag"], "n": r["n"]} for r in rows],
                    "max": tags.MAX_TAGS})


@app.post("/api/reveal/<int:pid>")
def api_reveal(pid):
    """Show the file in the Windows file manager."""
    if not files_here():
        return jsonify({"ok": False, "error": FILES_REFUSED}), 403
    with closing(db()) as con, con:
        row = con.execute("SELECT path FROM visible_photos AS photos WHERE id=?", (pid,)).fetchone()
    return reveal_file(row["path"] if row else None)


@app.post("/api/reveal-raw/<int:pid>/<int:raw_id>")
def api_reveal_raw(pid, raw_id):
    """Resolve the RAW through its JPEG, never through a path from the client."""
    if not files_here():
        return jsonify({"ok": False, "error": FILES_REFUSED}), 403
    with closing(db()) as con:
        companions = raw_pairs.files_for(con, [pid]).get(pid, [])
    path = next((item["path"] for item in companions if item["id"] == raw_id), None)
    return reveal_file(path)


def reveal_file(path):
    if not path or not os.path.isfile(path):
        return jsonify({"ok": False, "error": "File not found on disk"}), 404
    path = os.path.normpath(path)
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/open/<int:pid>")
def api_open(pid):
    """Open the file with the default program."""
    if not files_here():
        return jsonify({"ok": False, "error": FILES_REFUSED}), 403
    with closing(db()) as con, con:
        row = con.execute("SELECT path FROM visible_photos AS photos WHERE id=?", (pid,)).fetchone()
    if not row or not os.path.isfile(row["path"]):
        return jsonify({"ok": False, "error": "File not found on disk"}), 404
    try:
        if sys.platform == "win32":
            os.startfile(row["path"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", row["path"]])
        else:
            subprocess.Popen(["xdg-open", row["path"]])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/export")
def api_export():
    """Export the current selection to CSV."""
    import csv
    where, params = build_where()
    with closing(db()) as con, con:
        rows = con.execute(
            f"SELECT taken, brand, camera, lens, focal, focal35, iso, fnumber,"
            f" shutter, {TAG_COL}, color, color_hex, color_center, color_center_hex,"
            f" brightness, contrast, chroma, sharp_center, sharp_edge, lat, lon,"
            f" width, height, size, comment, path FROM visible_photos AS photos {where}"
            f" ORDER BY taken", params).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Taken", "Brand", "Camera", "Lens", "Focal, mm", "Equiv, mm",
                "ISO", "Aperture", "Shutter", "Tags", "Colour", "Hex",
                "Centre colour", "Centre hex", "Brightness",
                "Contrast", "Chroma", "Sharpness centre", "Sharpness edges",
                "Latitude", "Longitude",
                "Width", "Height", "Size", "Comment", "Path"])
    w.writerows([list(r) for r in rows])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=photos.csv"})


@app.get("/api/export/paths")
def api_export_paths():
    """The paths of the selected files, one per line, and nothing else.

    A list to hand to something that does move files about — copy the frames you
    rated 5+ somewhere, delete the ones marked bad. This program does not do that
    and will not: the moment it deletes a file on the strength of its own guess
    about duplicates, one wrong guess costs a photograph. It hands over the list;
    what happens to those files is the decision of whoever runs the next command.

    The header line is there so that PowerShell's Import-Csv can read it, which
    is what a list like this is for on Windows. Sorted by path rather than by the
    order on screen: a list about to be fed to rm is read folder by folder.

    The separator is a comma, not the semicolon the other export uses. That one
    is a table for a spreadsheet, where a semicolon is what a European Excel
    expects; this one is a single column read by a script, and a comma is the
    only separator Import-Csv assumes. It matters for the paths themselves: a
    folder named "Photos, 2019" is quoted when the comma is the separator and
    left bare when it is not, and left bare it would arrive as two columns.
    """
    import csv
    where, params = build_where()
    with closing(db()) as con, con:
        rows = con.execute(f"SELECT path FROM visible_photos AS photos {where} ORDER BY path",
                           params).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Path"])
    w.writerows([[r["path"]] for r in rows])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=photo-paths.csv"})


NO_UI = r"""<!DOCTYPE html><html lang="en"><meta charset="utf-8">
<title>index.html not found</title>
<body style="background:#16151A;color:#EDE8DF;font:15px/1.6 'Segoe UI',sans-serif;padding:40px">
<h1 style="font-size:20px">Interface file not found</h1>
<p>Expected <code>index.html</code> in one of these places:</p>
<pre style="background:#1E1D24;padding:12px;border-radius:4px">{a}\index.html
{a}\web\index.html</pre>
<p>Put <code>index.html</code> into the <code>web</code> subfolder and reload.</p>
</body></html>"""


@app.get("/favicon.ico")
def favicon():
    # The browser asks for the icon on its own, without consulting the markup,
    # and without this route every tab left a 404 in the log. The file may be
    # missing — the icon is built by tools/make_icon.py — and then we answer with
    # nothing, the way we used to.
    if WEB_DIR and os.path.isfile(os.path.join(WEB_DIR, "favicon.ico")):
        return send_from_directory(WEB_DIR, "favicon.ico")
    return Response(status=204)


@app.get("/")
def index():
    if not WEB_DIR:
        return Response(NO_UI.format(a=APP_DIR), mimetype="text/html", status=500)
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/<path:fn>")
def static_files(fn):
    if not WEB_DIR:
        abort(404)
    return send_from_directory(WEB_DIR, fn)


def initialize_database(path):
    with closing(db_schema.open_database(path)):
        pass


def main():
    global DB_PATH, MIN_YEAR, LOOPBACK_ONLY
    ap = argparse.ArgumentParser(description="Photo archive statistics")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--port", type=int, default=5577)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--version", action="version", version=f"photostats {VERSION}")
    ap.add_argument("--min-year", type=int, default=MIN_YEAR,
                    help=f"earliest year shown on the timeline (default {MIN_YEAR})")
    args = ap.parse_args()

    DB_PATH = os.path.abspath(args.db)
    LOOPBACK_ONLY = is_loopback(args.host)
    MIN_YEAR = args.min_year
    if not os.path.exists(DB_PATH):
        print(f"No database at {DB_PATH}. Run this first:\n"
              f'    python scan.py "D:\\Photos"')
        sys.exit(1)
    os.makedirs(CACHE_DIR, exist_ok=True)
    initialize_database(DB_PATH)
    if not WEB_DIR:
        print("WARNING: index.html not found — neither in " + APP_DIR +
              ",\n         nor in " + os.path.join(APP_DIR, "web") +
              ".\n         Put index.html into the web subfolder and restart.")

    url = f"http://{args.host}:{args.port}/"
    print(f"Database: {DB_PATH}\nOpening {url}\nPress Ctrl+C here to stop.")
    if not LOOPBACK_ONLY:
        print("WARNING: open to the local network, with no password. Anyone on it\n"
              "         can see every photo, its path and where it was taken, and\n"
              "         change ratings, tags and comments. Opening files is off.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, threaded=True,
            request_handler=TimedRequestHandler)


if __name__ == "__main__":
    main()
