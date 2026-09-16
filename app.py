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
import os
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from hashlib import md5

from flask import (Flask, Response, abort, g, jsonify, request, send_file,
                   send_from_directory)

import dupkey

try:
    from version import VERSION
except ImportError:          # the file was copied without version.py
    VERSION = "unknown"

APP_DIR = os.path.dirname(os.path.abspath(__file__))


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

# The rule itself is in dupkey.py: the folder check in scan.py uses the same one,
# and those two answers must not drift apart. Only the per-request cache is here.


def sig_ready():
    """Is every file signed? Worked out once per request."""
    if not hasattr(g, "sig_ready"):
        with db() as con:
            g.sig_ready = dupkey.sigs_complete(con)
    return g.sig_ready


def name_key(alias=""):
    return dupkey.name_key(alias)


def dup_key(alias=""):
    return dupkey.dup_key(alias, sig_ready())


def dup_exists():
    k = dup_key()
    return f"{k} IN (SELECT {k} FROM photos GROUP BY 1 HAVING COUNT(*) > 1)"


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
}


def range_col(key):
    col = RANGE_DIMS[key][0]
    return col or focal_col()


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------


# The server reads the database while the scanner writes to it. Without waiting
# on the lock, nearly half the queries fail with "database is locked" — measured.
BUSY_MS = 15000


def db():
    con = sqlite3.connect(DB_PATH, timeout=BUSY_MS / 1000)
    con.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
    try:
        con.execute("PRAGMA journal_mode = WAL")   # unavailable on network drives
    except sqlite3.Error:
        pass
    con.row_factory = sqlite3.Row
    return con


SIMPLE_FILTERS = {
    "year": "year", "ym": "ym", "month": "month", "hour": "hour",
    "brand": "brand", "camera": "camera", "lens": "lens", "ext": "ext",
}


def focal_col():
    return "focal35" if request.args.get("eq") == "1" else "focal"


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
        clauses.append("(folder = ? OR folder LIKE ?)")
        params += [folder, folder.rstrip("\\/") + os.sep + "%"]

    q = request.args.get("q")
    if q:
        clauses.append("path LIKE ?")
        params.append("%" + q + "%")

    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def counts(col, skip, labeller=None, order="cnt", limit=None):
    where, params = build_where(skip=skip)
    sql = (f"SELECT {col} AS k, COUNT(*) AS cnt FROM photos {where} "
           f"GROUP BY {col} ORDER BY " +
           ("cnt DESC" if order == "cnt" else f"{col} ASC"))
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    with db() as con:
        for r in con.execute(sql, params):
            k = r["k"]
            key = NONE_KEY if k is None else str(k)
            label = NONE_LABEL if k is None else (labeller(k) if labeller else str(k))
            out.append({"k": key, "l": label, "v": r["cnt"]})
    return out


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def bucket_counts(key):
    """Sorts values into ranges in one query, without pulling the rows out."""
    col = range_col(key)
    buckets = RANGE_DIMS[key][1]
    case = " ".join([f"WHEN {col} IS NULL THEN '{NONE_KEY}'"] +
                    [f"WHEN {col} < {hi} THEN '{lo}-{hi}'" for lo, hi, _ in buckets])
    expr = f"CASE {case} ELSE '{NONE_KEY}' END"
    where, params = build_where(skip=key)
    with db() as con:
        got = dict(con.execute(f"SELECT {expr} AS k, COUNT(*) FROM photos {where} "
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
        return "filename", [("has duplicates", dup_exists()),
                            ("no duplicates", "NOT " + dup_exists())]
    return DERIVED_DIMS[key]


def derived_counts(key):
    guard, opts = derived_opts(key)
    case = " ".join(f"WHEN {cond} THEN '{name}'" for name, cond in opts)
    expr = f"CASE WHEN {guard} IS NULL THEN NULL {case} END"
    where, params = build_where(skip=key)
    with db() as con:
        got = dict(con.execute(f"SELECT {expr} AS k, COUNT(*) FROM photos {where} "
                               f"GROUP BY k", params).fetchall())
    got.pop(None, None)
    return [{"k": n, "l": n, "v": got[n]} for n, _ in opts if got.get(n)]


# Colour values in databases from earlier versions are written in Russian. They
# are translated in one query at startup: there is no need to recount pixels just
# to rename something.
COLOR_RENAME = {
    "красный": "red", "оранжевый": "orange", "коричневый": "brown",
    "жёлтый": "yellow", "зелёный": "green", "голубой": "cyan", "синий": "blue",
    "фиолетовый": "violet", "белый": "white", "серый": "gray",
    "чёрный": "black", "не определён": "unknown",
}


def rename_colors(con):
    old = tuple(COLOR_RENAME)
    marks = ",".join("?" * len(old))
    hit = con.execute(f"SELECT 1 FROM photos WHERE color IN ({marks}) "
                      f"OR color_center IN ({marks}) LIMIT 1",
                      old + old).fetchone()
    if not hit:
        return
    for src, dst in COLOR_RENAME.items():
        con.execute("UPDATE photos SET color = ? WHERE color = ?", (dst, src))
        con.execute("UPDATE photos SET color_center = ? WHERE color_center = ?",
                    (dst, src))
    con.commit()
    print("Colour names in the database translated to English.")


def color_expr(col="color"):
    """The colour group, corrected for dominance — one definition serving both the
    counting and the filtering. col: color or color_center."""
    return (f"CASE WHEN {col} IS NULL THEN NULL "
            f"WHEN {col}_share < {MIXED_MAX} THEN '{COLOR_MIXED}' "
            f"ELSE {col} END")


def color_counts(col="color"):
    where, params = build_where(skip=col)
    with db() as con:
        got = dict(con.execute(f"SELECT {color_expr(col)} AS k, COUNT(*) FROM photos "
                               f"{where} GROUP BY k", params).fetchall())
    got.pop(None, None)                    # NULL — the photo has not been analysed yet
    order = COLOR_ORDER + [COLOR_MIXED, COLOR_UNKNOWN]
    return [{"k": n, "l": n, "v": got[n], "c": COLOR_SWATCH[n]}
            for n in order if got.get(n)]


@app.get("/api/stats")
def api_stats():
    with db() as con:
        total_all, = con.execute("SELECT COUNT(*) FROM photos").fetchone()
        if not total_all:
            return jsonify({"empty": True})
        where, params = build_where()
        total, = con.execute(f"SELECT COUNT(*) FROM photos {where}", params).fetchone()
        dup_groups, = con.execute(
            f"SELECT COUNT(*) FROM (SELECT {dup_key()} FROM photos "
            f"GROUP BY 1 HAVING COUNT(*) > 1)"
        ).fetchone()
        span = con.execute(
            f"SELECT MIN(taken), MAX(taken), MIN(ym), MAX(ym) FROM photos {where}",
            params).fetchone()
        sized, = con.execute(f"SELECT COALESCE(SUM(size),0) FROM photos {where}",
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
            "dup": derived_counts("dup"),
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
    with db() as con:
        per_month = {ym: c for ym, c in con.execute(
            f"SELECT ym, COUNT(*) FROM photos {where_all} GROUP BY ym", params_all)
            if good(ym)}
        raw = [(ym, k, c) for ym, k, c in con.execute(
            f"SELECT ym, {dim} AS k, COUNT(*) FROM photos {where_sel} "
            f"GROUP BY ym, k", params_sel) if good(ym)]
        # exact dates of the first and last frame — by capture date, not by month
        dates = {(k or NONE_LABEL): (a, b) for k, a, b in con.execute(
            f"SELECT {dim} AS k, MIN(taken), MAX(taken) FROM photos {where_sel} "
            f"WHERE year BETWEEN ? AND ? GROUP BY k"
            if not where_sel else
            f"SELECT {dim} AS k, MIN(taken), MAX(taken) FROM photos {where_sel} "
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
    with db() as con:
        total, = con.execute(f"SELECT COUNT(*) FROM photos {where}", params).fetchone()
        rows = con.execute(
            f"SELECT CAST(lat / ? AS INT) AS gy, CAST(lon / ? AS INT) AS gx,"
            f" COUNT(*) AS c, AVG(lat), AVG(lon),"
            f" MIN(lat), MAX(lat), MIN(lon), MAX(lon),"
            f" MIN(taken), MAX(taken)"
            f" FROM photos {where} GROUP BY gy, gx ORDER BY c DESC LIMIT ?",
            [cell, cell] + params + [limit]).fetchall()
        with_geo, = con.execute(
            "SELECT COUNT(*) FROM photos WHERE lat IS NOT NULL").fetchone()
        all_photos, = con.execute("SELECT COUNT(*) FROM photos").fetchone()

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
}


@app.get("/api/folders")
def api_folders():
    """The folders the photos of the current selection live in."""
    where, params = build_where()
    limit = max(1, min(int(request.args.get("limit", 300)), 2000))
    with db() as con:
        rows = con.execute(
            f"SELECT folder, COUNT(*) AS n, MIN(taken), MAX(taken) FROM photos"
            f" {where} GROUP BY folder ORDER BY n DESC, folder LIMIT ?",
            params + [limit]).fetchall()
        total, = con.execute(
            f"SELECT COUNT(DISTINCT folder) FROM photos {where}", params).fetchone()
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
    with db() as con:
        total, = con.execute(f"SELECT COUNT(*) FROM photos {where}", params).fetchone()
        rows = con.execute(
            f"SELECT id, path, filename, folder, taken, date_src, brand, camera, lens,"
            f" focal, focal35, iso, fnumber, shutter, exposure, width, height, size, ext,"
            f" lat, lon,"
            f" (SELECT COUNT(*) FROM photos d"
            f"  WHERE {dup_key('d')} = {dup_key('photos')}) AS dup_n,"
            f" color, color_hex, color_share, color_center, color_center_hex,"
            f" color_center_share, brightness, contrast, chroma,"
            f" clip_hi, clip_lo"
            f" FROM photos {where} "
            f"ORDER BY {', '.join(c + ' ' + order for c in SORTS[field])},"
            f" id LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
    return jsonify({"total": total, "offset": offset,
                    "items": [dict(r) for r in rows]})


@app.get("/api/random")
def api_random():
    """A few random photos from the current selection, for the preview strip."""
    where, params = build_where()
    n = max(1, min(int(request.args.get("n", 10)), 30))
    with db() as con:
        rows = con.execute(
            f"SELECT id, path, filename, folder, taken, date_src, brand, camera,"
            f" lens, focal, focal35, iso, fnumber, shutter, exposure, width, height,"
            f" size, ext, lat, lon, color, color_hex, color_share, color_center,"
            f" color_center_hex, color_center_share, brightness, contrast,"
            f" chroma, clip_hi, clip_lo"
            f" FROM photos {where} ORDER BY RANDOM() LIMIT ?", params + [n]).fetchall()
    return jsonify({"items": [dict(r) for r in rows]})


@app.get("/api/thumb/<int:pid>")
def api_thumb(pid):
    size = max(64, min(int(request.args.get("s", 260)), 1400))
    with db() as con:
        row = con.execute("SELECT path, mtime FROM photos WHERE id=?", (pid,)).fetchone()
    if not row:
        abort(404)
    path = row["path"]
    tag = md5(f"{path}|{row['mtime']}|{size}".encode("utf-8")).hexdigest()
    cached = os.path.join(CACHE_DIR, tag[:2], tag + ".jpg")
    if os.path.exists(cached):
        return send_file(cached, mimetype="image/jpeg", max_age=86400)
    if not os.path.exists(path):
        abort(404)

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


@app.post("/api/reveal/<int:pid>")
def api_reveal(pid):
    """Show the file in the Windows file manager."""
    with db() as con:
        row = con.execute("SELECT path FROM photos WHERE id=?", (pid,)).fetchone()
    if not row or not os.path.exists(row["path"]):
        return jsonify({"ok": False, "error": "File not found on disk"}), 404
    path = os.path.normpath(row["path"])
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
    with db() as con:
        row = con.execute("SELECT path FROM photos WHERE id=?", (pid,)).fetchone()
    if not row or not os.path.exists(row["path"]):
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
    with db() as con:
        rows = con.execute(
            f"SELECT taken, brand, camera, lens, focal, focal35, iso, fnumber,"
            f" shutter, color, color_hex, color_center, color_center_hex,"
            f" brightness, contrast, chroma, lat, lon,"
            f" width, height, size, path FROM photos {where}"
            f" ORDER BY taken", params).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Taken", "Brand", "Camera", "Lens", "Focal, mm", "Equiv, mm",
                "ISO", "Aperture", "Shutter", "Colour", "Hex",
                "Centre colour", "Centre hex", "Brightness",
                "Contrast", "Chroma", "Latitude", "Longitude",
                "Width", "Height", "Size", "Path"])
    w.writerows([list(r) for r in rows])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=photos.csv"})


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


def main():
    global DB_PATH, MIN_YEAR
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
    MIN_YEAR = args.min_year
    if not os.path.exists(DB_PATH):
        print(f"No database at {DB_PATH}. Run this first:\n"
              f'    python scan.py "D:\\Photos"')
        sys.exit(1)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with db() as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(photos)")}
        # columns first, then indexes: an index on a column that does not exist
        # yet cannot be created, and the database may be from an earlier version
        for name, decl in (("lat", "REAL"), ("lon", "REAL"), ("sig", "TEXT"),
                           ("color", "TEXT"), ("color_hex", "TEXT"),
                           ("color_share", "REAL"), ("color_center", "TEXT"),
                           ("color_center_hex", "TEXT"),
                           ("color_center_share", "REAL"), ("brightness", "REAL"),
                           ("contrast", "REAL"), ("chroma", "REAL"),
                           ("clip_hi", "REAL"), ("clip_lo", "REAL")):
            if name not in cols:
                con.execute(f"ALTER TABLE photos ADD COLUMN {name} {decl}")
                con.commit()
        con.execute("DROP INDEX IF EXISTS ix_dup")      # the key changed
        con.execute(f"CREATE INDEX IF NOT EXISTS ix_dupkey ON photos({name_key()})")
        con.execute("CREATE INDEX IF NOT EXISTS ix_sig ON photos(sig)")
        con.commit()
        if "exposure" not in cols:
            print("Upgrading the database for new metrics…")
            con.execute("ALTER TABLE photos ADD COLUMN exposure REAL")
            upd = []
            for pid, txt in con.execute("SELECT id, shutter FROM photos "
                                        "WHERE shutter IS NOT NULL"):
                t = str(txt).strip().rstrip("sс")
                try:
                    sec = (float(t.split("/")[0]) / float(t.split("/")[1])
                           if "/" in t else float(t))
                except (ValueError, ZeroDivisionError, IndexError):
                    continue
                upd.append((sec, pid))
            con.executemany("UPDATE photos SET exposure = ? WHERE id = ?", upd)
            con.commit()
            print(f"  shutter speed converted to seconds: {len(upd)} rows")
    with db() as con:
        rename_colors(con)
    if not WEB_DIR:
        print("WARNING: index.html not found — neither in " + APP_DIR +
              ",\n         nor in " + os.path.join(APP_DIR, "web") +
              ".\n         Put index.html into the web subfolder and restart.")

    url = f"http://{args.host}:{args.port}/"
    print(f"Database: {DB_PATH}\nOpening {url}\nPress Ctrl+C here to stop.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
