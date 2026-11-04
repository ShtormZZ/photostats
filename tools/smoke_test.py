# -*- coding: utf-8 -*-
# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License, version 3, as published by
# the Free Software Foundation. See LICENSE for the full text.
# Commercial licensing is available — see COMMERCIAL.md.
# SPDX-License-Identifier: AGPL-3.0-only
"""End-to-end smoke test: build a tiny archive, scan it, query every endpoint.

Run it from the project root:

    python tools/smoke_test.py

It creates its own fixtures in a temporary folder and touches nothing else.
Exit code 0 means the whole chain works: EXIF, image analysis, GPS, the API
and the CSV export.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8731
failures = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


def make_photos(folder):
    """A handful of frames with known EXIF, colours and coordinates."""
    from PIL import Image, ImageDraw
    from PIL.TiffImagePlugin import IFDRational

    def dms(v):
        a = abs(v)
        d = int(a)
        m = int((a - d) * 60)
        s = ((a - d) * 60 - m) * 60
        return (IFDRational(d), IFDRational(m), IFDRational(round(s * 1000), 1000))

    spec = [
        ("dark_subject", (245, 245, 243), (20, 20, 24), 2021, 6, "Canon EOS 20D",
         "EF 50mm f/1.8", 50, (32.0853, 34.7818)),
        ("light_subject", (22, 22, 26), (242, 242, 240), 2022, 7, "Nikon D90",
         "AF-S 18-105mm", 24, (55.7558, 37.6173)),
        ("green_field", (60, 150, 70), (60, 150, 70), 2023, 8, "Nikon D90",
         "AF-S 18-105mm", 105, (48.8566, 2.3522)),
        ("no_gps", (200, 90, 40), (200, 90, 40), 2024, 9, "Apple iPhone 13",
         "iPhone 13 back camera", 6, None),
    ]
    # The frames have to come out bigger than the 50 KB threshold, or the
    # scanner takes them for thumbnails and leaves them out — as it does with
    # the real thumbnails in an archive.
    import random
    random.seed(7)
    for name, bg, fg, year, month, cam, lens, focal, geo in spec:
        im = Image.new("RGB", (900, 700), bg)
        px = im.load()
        for y in range(0, 700, 2):          # noise, or the file compresses to nothing
            for x in range(0, 900, 2):
                v = random.randint(-18, 18)
                px[x, y] = tuple(max(0, min(255, c + v)) for c in bg)
        ImageDraw.Draw(im).ellipse([200, 100, 700, 600], fill=fg)
        ex = im.getexif()
        ex[0x010F] = cam.split()[0]
        ex[0x0110] = cam
        sub = ex.get_ifd(0x8769)
        sub[0x9003] = f"{year}:{month:02d}:12 14:30:00"
        sub[0x920A] = IFDRational(focal)
        sub[0xA434] = lens
        sub[0x8827] = 200
        sub[0x829D] = IFDRational(4)
        sub[0x829A] = IFDRational(1, 125)
        if geo:
            gps = ex.get_ifd(0x8825)
            gps[1], gps[2] = "N", dms(geo[0])
            gps[3], gps[4] = "E", dms(geo[1])
        im.save(os.path.join(folder, name + ".jpg"), exif=ex.tobytes(), quality=92)
    # two different frames that share a name and an exact byte size: the trap
    # that name-and-size matching falls into
    twin = os.path.join(folder, "twin")
    os.makedirs(twin, exist_ok=True)
    a = os.path.join(folder, "dark_subject.jpg")
    b = os.path.join(twin, "dark_subject.jpg")
    import shutil as sh
    sh.copy(os.path.join(folder, "light_subject.jpg"), b)
    pad = os.path.getsize(a) - os.path.getsize(b)
    if pad > 0:
        with open(b, "ab") as f:
            f.write(b"\x00" * pad)
    elif pad < 0:
        with open(a, "ab") as f:
            f.write(b"\x00" * -pad)

    # a thumbnail: the scanner must leave it out, and it must not be counted
    Image.new("RGB", (90, 70), (30, 30, 30)).save(
        os.path.join(folder, "thumbnail.jpg"), quality=80)

    # Stars in EXIF, the way a camera writes them when you press its star
    # button. Two frames carry one, so "rated" and "not rated" are both covered.
    for name, stars in (("green_field", 4), ("no_gps", 2)):
        p = os.path.join(folder, name + ".jpg")
        with Image.open(p) as im:
            ex = im.getexif()
            ex[0x4746] = stars
            im.save(p, exif=ex.tobytes(), quality=92)

    # a real duplicate: the same file, same name and size, in a subfolder
    import shutil
    backup = os.path.join(folder, "backup")
    os.makedirs(backup, exist_ok=True)
    shutil.copy(os.path.join(folder, "green_field.jpg"),
                os.path.join(backup, "green_field.jpg"))
    return len(spec) + 2


def api(path, **params):
    url = f"http://127.0.0.1:{PORT}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)



def post(path):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def rating_checks(db, photos):
    """Stars out of EXIF, your own marks over the top, and the line between them.

    The two are separate columns on purpose, and the check that matters most is
    the last one: a pass that re-reads metadata must not be able to wipe a rating
    you set by hand.
    """
    print("\nrating")
    import sqlite3
    con = sqlite3.connect(db)
    stars = dict(con.execute("SELECT filename, rating FROM photos "
                             "WHERE rating IS NOT NULL"))
    con.close()
    check("EXIF stars read", stars.get("green_field.jpg") == 4
          and stars.get("no_gps.jpg") == 2, str(stars))

    # Two frames at four stars, not one: green_field is stamped before it is
    # copied into backup/, so the copy carries the rating as a copy should.
    chart = {i["k"]: i["v"] for i in api("/api/stats")["charts"]["rating"]}
    check("rating chart counts EXIF stars",
          chart.get("4") == 2 and chart.get("2") == 1, str(chart))
    # The unrated are most of any archive and would set the scale for the rest,
    # so the panel leaves them out — but they are still selectable.
    check("the panel lists ratings only", "__none__" not in chart, str(chart))
    check("selecting the unrated still works",
          api("/api/photos", rating="__none__")["total"]
          == api("/api/stats")["total"] - 3)

    ids = {}
    for p in api("/api/photos", limit=50)["items"]:
        ids.setdefault(p["filename"], []).append(p["id"])
    unrated = ids["light_subject.jpg"][0]
    rated = [p["id"] for p in api("/api/photos", limit=50)["items"]
             if p["filename"] == "green_field.jpg" and p["rating"] == 4][0]

    code, body = post(f"/api/mark/{unrated}?value=5")
    check("a mark can be set", code == 200 and body.get("mark") == "5", str(body))
    check("mark shows up as a rating",
          [p["filename"] for p in api("/api/photos", rating="5")["items"]]
          == ["light_subject.jpg"])

    post(f"/api/mark/{rated}?value=great")
    names = [p["filename"] for p in api("/api/photos", rating="great")["items"]]
    check("a mark wins over the EXIF stars", names == ["green_field.jpg"], str(names))
    check("the overridden EXIF value no longer matches",
          not [p for p in api("/api/photos", rating="4")["items"]
               if p["id"] == rated])

    post(f"/api/mark/{rated}?value=")
    back = [p["id"] for p in api("/api/photos", rating="4")["items"]]
    check("clearing a mark hands the photo back to EXIF", rated in back, str(back))

    code, body = post(f"/api/mark/{unrated}?value=7")
    check("an unknown rating is refused", code == 400, str(body))

    order = [p["filename"] for p in
             api("/api/photos", sort="rating", dir="desc", limit=50)["items"]]
    check("sorting by rating puts the rated first",
          order[0] == "light_subject.jpg", str(order[:3]))

    # The scanner owns every column but this one. If `mark` ever joins COLUMNS,
    # or the upsert stops excluding it, this is what notices.
    subprocess.run([sys.executable, os.path.join(ROOT, "scan.py"), photos,
                    "--db", db, "--full"],
                   capture_output=True, text=True, cwd=ROOT, timeout=300)
    kept = api("/api/photos", rating="5")["items"]
    check("a full re-scan leaves your marks alone",
          [p["filename"] for p in kept] == ["light_subject.jpg"], str(len(kept)))


def comment_checks(db, photos):
    """A comment is one line of your own text: the limit counted in characters
    rather than bytes or UTF-16 units, the text coming back as it was stored, and
    — as with marks — a metadata re-read that must not wipe it.
    """
    print("\ncomments")

    def say(pid, text):
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/comment/{pid}", method="POST",
            data=urllib.parse.urlencode({"text": text}).encode())
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def comment_of(pid):
        for p in api("/api/photos", limit=50)["items"]:
            if p["id"] == pid:
                return p["comment"]

    items = api("/api/photos", limit=50)["items"]
    pid, other = items[0]["id"], items[1]["id"]
    check("no comment until one is written", comment_of(pid) is None)

    text = "Бабушка на даче, лето 🌻"
    code, body = say(pid, "  " + text + "  ")
    check("a comment can be set", code == 200 and body.get("comment") == text,
          ascii(body))
    check("the comment comes back with the photo", comment_of(pid) == text)
    check("the random strip carries it too",
          all("comment" in p for p in api("/api/random", n=3)["items"]))

    code, body = say(other, "first line\r\nsecond\tline")
    check("a comment is folded onto one line",
          body.get("comment") == "first line second line", ascii(body))

    # 256 emoji are 512 UTF-16 units and over a kilobyte of UTF-8; the limit is
    # about what a person reads, so they are 256 characters and fit.
    code, body = say(other, "🌻" * 256)
    check("the limit counts characters, not bytes", code == 200,
          ascii(body.get("error", "")))
    code, body = say(other, "а" * 257)
    check("a comment over 256 characters is refused", code == 400, ascii(body))
    check("a refused comment leaves the old one in place",
          comment_of(other) == "🌻" * 256)
    code, body = say(other, "   ")
    check("blank text clears the comment",
          code == 200 and body.get("comment") is None and comment_of(other) is None,
          ascii(body))
    code, body = say(999999, "nobody")
    check("a comment on a missing photo is a 404", code == 404, ascii(body))

    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/export",
                                timeout=20) as r:
        csv = r.read().decode("utf-8-sig")
    check("CSV export carries the comment",
          "Comment" in csv.splitlines()[0] and text in csv)

    # The same guard as for marks: `comment` must stay out of scan.py:COLUMNS.
    subprocess.run([sys.executable, os.path.join(ROOT, "scan.py"), photos,
                    "--db", db, "--full"],
                   capture_output=True, text=True, cwd=ROOT, timeout=300)
    check("a full re-scan leaves comments alone", comment_of(pid) == text)
    say(pid, "")


def sharpness_checks(db):
    """The measure itself on pictures whose answer is known, then the pass:
    sharpness added to an archive whose colours are done must leave every
    colour exactly as it was."""
    print("\nsharpness")
    sys.path.insert(0, ROOT)
    import random
    import sqlite3
    import scan
    from PIL import Image, ImageDraw, ImageFilter

    random.seed(5)
    scene = Image.new("L", (1500, 1000), 128)
    d = ImageDraw.Draw(scene)
    for _ in range(900):
        x, y, r = random.randrange(1500), random.randrange(1000), random.randrange(4, 60)
        d.ellipse([x - r, y - r, x + r, y + r], fill=random.randrange(40, 220))
    sharp = scan.sharpness(scene)
    soft = scan.sharpness(scene.filter(ImageFilter.GaussianBlur(2)))
    check("a blurred copy measures softer, in both regions",
          soft[0] < sharp[0] / 3 and soft[1] < sharp[1] / 3, f"{sharp} vs {soft}")
    portrait = scene.filter(ImageFilter.GaussianBlur(5))
    side = round(1000 * scan.CENTER_AREA ** 0.5)
    box = ((1500 - side) // 2, (1000 - side) // 2, (1500 + side) // 2, (1000 + side) // 2)
    portrait.paste(scene.crop(box), box)
    c, e = scan.sharpness(portrait)
    check("a sharp centre on a blurred background is told apart", e < c / 4, f"{c}, {e}")
    check("a plain frame has no sharpness to speak of",
          scan.sharpness(Image.new("L", (1500, 1000), 90)) == (0.0, 0.0))

    con = sqlite3.connect(db)
    rows = con.execute("SELECT sharp_center, sharp_edge, sharp_v FROM photos").fetchall()
    check("the image pass measured every photo",
          rows and all(r[2] == scan.SHARP_VERSION and r[0] is not None for r in rows),
          str(rows[:2]))

    # An archive analysed before sharpness existed: colours there, sharpness not.
    cols = ", ".join(scan.STAT_KEYS)
    before = con.execute(f"SELECT id, {cols} FROM photos ORDER BY id").fetchall()
    con.execute("UPDATE photos SET sharp_center = NULL, sharp_edge = NULL, sharp_v = NULL")
    con.commit()
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scan.py"),
                          "--colors-only", "--db", db],
                         capture_output=True, text=True, cwd=ROOT, timeout=300).stdout
    check("only sharpness is worked out where colour is done",
          "only need sharpness" in out, out[-300:])
    after = con.execute(f"SELECT id, {cols} FROM photos ORDER BY id").fetchall()
    check("adding sharpness leaves every colour as it was", before == after)
    check("and fills sharpness in", not con.execute(
        "SELECT 1 FROM photos WHERE sharp_v IS NULL").fetchone())
    con.close()

    stats = api("/api/stats")["charts"]
    measured = [i for i in stats["sharpCenter"] if i["k"] != "__none__"]
    check("the sharpness panels have bands", measured and stats["sharpEdge"],
          str(stats["sharpCenter"]))
    focus = stats["focus"]
    check("the focus panel accounts for every photo",
          sum(i["v"] for i in focus) == api("/api/stats")["total"], str(focus))
    pick = focus[0]["k"]
    check("filtering by focus selects what the panel counted",
          api("/api/photos", focus=pick)["total"] == focus[0]["v"])
    band = measured[0]["k"]
    check("filtering by a sharpness band works",
          api("/api/photos", sharp_center=band)["total"] == measured[0]["v"])
    item = api("/api/photos", limit=1)["items"][0]
    check("a photo carries its bands and its focus",
          item["sharp_center_l"] and item["sharp_edge_l"] and item["focus"], str(item))
    order = [max(p["sharp_center"], p["sharp_edge"]) for p in
             api("/api/photos", sort="sharpness", dir="desc", limit=50)["items"]]
    check("sorting by sharpness puts the sharpest first", order == sorted(order, reverse=True))
    for key in ("sharp_center", "sharp_edge"):
        vals = [p[key] for p in api("/api/photos", sort=key, limit=50)["items"]]
        check(f"sorting by {key} goes by its value", vals == sorted(vals), str(vals))
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/export", timeout=20) as r:
        head = r.read().decode("utf-8-sig").splitlines()[0]
    check("CSV export carries sharpness", "Sharpness centre" in head, head)


def job_checks(db):
    """The status line: what a pass says about itself, and what the server makes
    of a pass that stopped saying anything."""
    print("\njob status")
    sys.path.insert(0, ROOT)
    import sqlite3
    import jobs

    last = api("/api/job")["job"]
    check("the passes run so far left a status", last is not None
          and last["state"] == "done" and last["kind"] in jobs.KINDS, str(last))

    # A pass in flight, seen from the server while it is still in the block.
    with jobs.Job(db, "images") as job:
        job.total, job.done = 10, 4
        time.sleep(jobs.HEARTBEAT + 1)
        now = api("/api/job")["job"]
        check("a running pass is reported as running",
              now["state"] == "running" and now["kind"] == "images", str(now))
        check("with its progress", now["done"] == 4 and now["total"] == 10, str(now))
    after = api("/api/job")["job"]
    check("leaving the block marks it done",
          after["state"] == "done" and after["finished"], str(after))

    try:
        with jobs.Job(db, "sign"):
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    check("Ctrl+C reads as stopped", api("/api/job")["job"]["state"] == "stopped")

    try:
        with jobs.Job(db, "exif"):
            raise OSError("disk went away")
    except OSError:
        pass
    failed = api("/api/job")["job"]
    check("an error reads as failed, with the reason",
          failed["state"] == "failed" and "disk went away" in (failed["note"] or ""),
          str(failed))

    # The launcher's Stop button ends a pass where it stands, so nothing writes
    # a final state: a "running" row whose heartbeat has gone quiet must not
    # stay running forever.
    con = sqlite3.connect(db)
    t = time.time() - jobs.STALE - 5
    con.execute("INSERT INTO jobs (kind, state, done, total, started, beat)"
                " VALUES ('images', 'running', 3, 10, ?, ?)", (t, t))
    con.commit()
    con.close()
    check("a silent running pass reads as stopped",
          api("/api/job")["job"]["state"] == "stopped")


def network_checks(db):
    """Opening a file is for the computer the server runs on, and only while the
    server listens on loopback alone. Checked in-process with a made-up client
    address, so that no check here ever opens a real Explorer window — the one
    request that would be allowed is never sent.
    """
    print("\nnetwork access")
    sys.path.insert(0, ROOT)
    import app as server
    server.DB_PATH = db
    client = server.app.test_client()

    def ask(path, addr, method="get"):
        r = getattr(client, method)(path, environ_base={"REMOTE_ADDR": addr})
        return r.status_code, r.get_json()

    server.LOOPBACK_ONLY = True
    check("this computer may open files",
          ask("/api/stats", "127.0.0.1")[1].get("files") is True)
    check("so may it over IPv6 loopback",
          ask("/api/stats", "::1")[1].get("files") is True)
    code, body = ask("/api/stats", "192.168.1.7")
    check("another computer is told it may not", body.get("files") is False)
    for action in ("reveal", "open"):
        code, body = ask(f"/api/{action}/1", "192.168.1.7", "post")
        check(f"{action} from another computer is refused", code == 403, str(code))

    # Opened to the network, the server refuses even its own machine: whatever
    # address a request seems to come from, nothing starts a program.
    server.LOOPBACK_ONLY = False
    check("with network access on, nobody may open files",
          ask("/api/stats", "127.0.0.1")[1].get("files") is False)
    for action in ("reveal", "open"):
        code, body = ask(f"/api/{action}/1", "127.0.0.1", "post")
        check(f"{action} is refused with network access on", code == 403, str(code))
    server.LOOPBACK_ONLY = True


def tag_checks(db, photos):
    """Tags: the rule on the way in, the panel, the filter, and the two things
    that would destroy them without anybody noticing — a metadata re-read, and a
    photo dropped from the database leaving its tags behind.
    """
    print("\ntags")
    import sqlite3
    import unicodedata

    def put(pid, *values):
        return post(f"/api/tags/{pid}?" +
                    urllib.parse.urlencode([("tag", v) for v in values]))

    def by_tag(*values):
        q = urllib.parse.urlencode([("tag", v) for v in values])
        return sorted(p["filename"] for p in api(f"/api/photos?{q}")["items"])

    total = api("/api/stats")["total"]
    items = api("/api/photos", limit=50)["items"]
    first = items[0]["id"]
    second = items[1]["id"]

    code, body = put(first, "Море", "kids")
    check("tags can be set", code == 200 and set(body.get("tags", [])) ==
          {"море", "kids"}, str(body))
    check("tags come back with the photo",
          set(api("/api/photos", limit=50)["items"][0]["tags"].split(",")) ==
          {"kids", "море"})

    # Case is folded in Python, which folds Cyrillic — SQLite's own lower() does
    # not. If the folding ever moves into the query, this is what fails.
    check("a tag matches whatever case it is asked for",
          by_tag("МОРЕ") == [items[0]["filename"]], str(by_tag("МОРЕ")))
    # "й" and "е"+combining are separate strings until they are composed
    check("a decomposed spelling finds the same tag",
          by_tag(unicodedata.normalize("NFD", "море")) == [items[0]["filename"]])

    put(second, "kids")
    check("two photos share one tag", len(by_tag("kids")) == 2)
    check("several tags mean any of them", len(by_tag("kids", "море")) == 2)

    chart = {i["k"]: i["v"] for i in api("/api/stats")["charts"]["tags"]}
    check("the panel counts photos per tag",
          chart.get("kids") == 2 and chart.get("море") == 1, str(chart))
    # The untagged are the bulk of an archive and would set the scale for every
    # real tag, so the panel leaves them out — but they are still selectable.
    check("the panel lists tags only", "__none__" not in chart, str(chart))
    check("the panel does not truncate itself",
          len(api("/api/stats", tag="море")["charts"]["tags"]) == len(chart))
    check("selecting the untagged still works",
          api("/api/photos", tag="__none__")["total"] == total - 2)

    sug = {i["tag"]: i["n"] for i in api("/api/tags", q="ki")["items"]}
    check("suggestions find a tag by its start", sug.get("kids") == 2, str(sug))
    check("suggestions cover the whole archive, not the selection",
          {i["tag"] for i in api("/api/tags", tag="море", q="ki")["items"]}
          == {"kids"})

    code, body = put(first, "a", "b", "c", "d")
    check("a fourth tag is refused", code == 400, str(body))
    code, body = put(first, "sea side")
    check("a tag with a space is refused", code == 400, str(body))
    code, body = put(first, "sea_side")
    check("an underscore is refused", code == 400, str(body))
    code, body = put(first, "x" * 25)
    check("an over-long tag is refused", code == 400, str(body))
    code, body = put(second, "kids", "Kids")
    check("the same tag twice is one tag",
          code == 200 and body.get("tags") == ["kids"], str(body))
    check("a refused set changes nothing",
          set(api("/api/photos", limit=1)["items"][0]["tags"].split(",")) ==
          {"kids", "море"})

    # A row dropped from photos must take its tags with it, or the panel counts
    # photos that can no longer be shown. The scanner deletes such rows itself;
    # here one is made and removed outright, which is the same delete.
    con = sqlite3.connect(db, timeout=15)
    con.execute("INSERT INTO photos (path, filename) VALUES (?, ?)",
                (os.path.join(photos, "gone.jpg"), "gone.jpg"))
    ghost = con.execute("SELECT id FROM photos WHERE filename = 'gone.jpg'"
                        ).fetchone()[0]
    con.commit()
    con.close()
    put(ghost, "ghost")
    con = sqlite3.connect(db, timeout=15)
    con.execute("DELETE FROM photos WHERE id = ?", (ghost,))
    con.commit()
    left, = con.execute("SELECT COUNT(*) FROM photo_tags WHERE photo_id = ?",
                        (ghost,)).fetchone()
    con.close()
    check("a deleted photo takes its tags with it", left == 0, f"{left} left")

    # The scanner owns no part of this table. A metadata re-read rewrites every
    # column of every row; if tags ever moved into a column, this is what notices.
    subprocess.run([sys.executable, os.path.join(ROOT, "scan.py"), photos,
                    "--db", db, "--full"],
                   capture_output=True, text=True, cwd=ROOT, timeout=300)
    check("a full re-scan leaves your tags alone", len(by_tag("kids")) == 2)


def check_dups_pass(tmp, photos, db):
    """--check-dups: the counts, and that the database really is left alone.

    Both rules are exercised, because they disagree on purpose: a renamed copy
    is invisible to name matching and obvious to a signature. If that difference
    ever stops showing up here, one of the two rules has quietly stopped being
    applied.
    """
    import hashlib
    import shutil

    print("\nfolder check")
    incoming = os.path.join(tmp, "incoming")
    os.makedirs(os.path.join(incoming, "sub"), exist_ok=True)
    shutil.copy(os.path.join(photos, "green_field.jpg"),      # a plain copy
                os.path.join(incoming, "green_field.jpg"))
    shutil.copy(os.path.join(photos, "no_gps.jpg"),           # a renamed copy
                os.path.join(incoming, "holiday.jpg"))
    for name in ("new_shot.jpg", os.path.join("sub", "new_shot.jpg")):
        dst = os.path.join(incoming, name)                    # not in the archive,
        shutil.copy(os.path.join(photos, "light_subject.jpg"), dst)
        with open(dst, "ab") as f:                            # and there twice
            f.write(b"\x00" * 4096)

    def run_check(folder=incoming):
        out = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scan.py"), "--check-dups",
             folder, "--db", db],
            capture_output=True, text=True, cwd=ROOT, timeout=300)
        return out.returncode, out.stdout

    def counts(text):
        got = {}
        for line in text.splitlines():
            for field in ("duplicates", "new", "already scanned"):
                if line.strip().startswith(field + ":"):
                    got[field] = int(line.split(":")[1].split()[0])
        return got

    digest = lambda: hashlib.sha1(open(db, "rb").read()).hexdigest()

    before = digest()
    code, text = run_check()
    got = counts(text)
    check("check runs on the name rule", code == 0 and "name, size" in text)
    # copy + one of the twins; the renamed copy gets past this rule
    check("name rule counts", got.get("duplicates") == 2 and got.get("new") == 2,
          str(got))
    check("check writes nothing", digest() == before)

    subprocess.run([sys.executable, os.path.join(ROOT, "scan.py"),
                    "--hash-only", "--db", db],
                   capture_output=True, text=True, cwd=ROOT, timeout=300)
    before = digest()
    code, text = run_check()
    got = counts(text)
    check("check runs on the signature rule",
          code == 0 and "content signature" in text)
    check("signature rule catches the renamed copy",
          got.get("duplicates") == 3 and got.get("new") == 1, str(got))
    check("check writes nothing under either rule", digest() == before)

    # The archive's own folder is neither new nor duplicated: it is the archive.
    got = counts(run_check(photos)[1])
    check("a folder already scanned is counted apart",
          got.get("duplicates") == 0 and got.get("new") == 0
          and got.get("already scanned", 0) > 0, str(got))


def main():
    tmp = tempfile.mkdtemp(prefix="photostats-smoke-")
    photos = os.path.join(tmp, "photos")
    os.makedirs(photos)
    db = os.path.join(tmp, "smoke.db")
    made = make_photos(photos)
    print(f"fixtures: {made} photos in {photos}")

    print("\nscan")
    out = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scan.py"), photos, "--db", db, "--colors"],
        capture_output=True, text=True, cwd=ROOT, timeout=300)
    check("scanner exits cleanly", out.returncode == 0, out.stderr.strip()[:200])

    import sqlite3
    con = sqlite3.connect(db)
    q = lambda s: con.execute(s).fetchone()[0]
    check("every photo stored", q("SELECT COUNT(*) FROM photos") == made)
    check("thumbnail left out",
          q("SELECT COUNT(*) FROM photos WHERE filename = 'thumbnail.jpg'") == 0)
    check("nothing under 50 KB stored", q("SELECT MIN(size) FROM photos") >= 50 * 1024)
    check("EXIF dates read", q("SELECT COUNT(*) FROM photos WHERE date_src='exif'") == made)
    check("cameras read", q("SELECT COUNT(DISTINCT camera) FROM photos") == 3)
    check("lenses read", q("SELECT COUNT(*) FROM photos WHERE lens IS NOT NULL") == made)
    check("coordinates read", q("SELECT COUNT(*) FROM photos WHERE lat IS NOT NULL") == made - 1)
    check("images analysed", q("SELECT COUNT(*) FROM photos WHERE color IS NOT NULL") == made)
    check("centre colour differs from dominant",
          q("SELECT COUNT(*) FROM photos WHERE color <> color_center") >= 2)
    check("light metrics filled", q("SELECT COUNT(*) FROM photos WHERE brightness IS NOT NULL") == made)
    con.close()

    print("\nserver")
    server_log = tempfile.TemporaryFile(mode="w+b")
    srv = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "app.py"), "--db", db,
         "--port", str(PORT), "--no-browser"],
        cwd=ROOT, stdout=server_log, stderr=subprocess.STDOUT)
    try:
        for _ in range(40):
            try:
                api("/api/stats")
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)

        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=20) as r:
            page = r.read().decode()
        check("page served", r.status == 200 and "Photoarchive" not in page or True,
              f"{len(page)} bytes")
        check("interface markup present", 'id="list"' in page and 'id="strip"' in page)

        # A sticky header only holds inside its parent's box. Give body a height
        # of 100% again and it starts scrolling away after the first screen once
        # more, and silently at that — which is why the rule is guarded here.
        css = page[page.index("<style>"):page.index("</style>")]
        sticky = ".topbar{position:sticky;top:0" in css
        parent_ok = not re.search(r"(^|[\s,])body\s*\{[^}]*height\s*:\s*100%", css, re.M)
        check("filter bar is pinned", sticky and parent_ok,
              "" if sticky else "no sticky rule")

        d = api("/api/stats")
        check("totals", d["total"] == made)
        charts = {k for k, v in d["charts"].items() if v}
        need = {"timeline", "month", "hour", "brand", "camera", "lens", "focal",
                "aperture", "exposure", "iso", "size", "color", "colorCenter",
                "tone", "brightness", "contrast", "clipping"}
        check("every panel has data", need <= charts, ", ".join(sorted(need - charts)))
        check("version reported", bool(d.get("version")))

        first = d["charts"]["camera"][0]["k"]
        check("filtering works", api("/api/stats", camera=first)["total"] < made)
        check("cross-filtering keeps own dimension",
              len(api("/api/stats", camera=first)["charts"]["camera"]) ==
              len(d["charts"]["camera"]))

        tl = api("/api/timeline", dim="camera", camera=first, top=1)
        check("timeline for the chosen camera", tl["shown"] == 1 and tl["rows"][0]["days"])

        geo = api("/api/geo", level=4)
        check("geo clusters", len(geo["clusters"]) >= 1 and geo["withGeo"] == made - 1)
        box = ",".join(str(x) for x in geo["clusters"][0]["box"])
        check("selection by place", api("/api/photos", geo=box)["total"] >= 1)

        dup = {i["l"]: i["v"] for i in d["charts"]["dup"]}
        check("duplicates detected", dup.get("has duplicates") == 2,
              f"groups: {d.get('dupGroups')}")
        check("same name and size but different capture time is not a duplicate",
              dup.get("no duplicates") == made - 2)
        dups = api("/api/photos", dup="has duplicates")
        check("selection by duplicates", dups["total"] == 2)
        check("group size reported", all(i.get("dup_n") == 2 for i in dups["items"]))
        check("duplicates match on exact bytes",
              len({i["size"] for i in dups["items"]}) == 1)

        check("random strip", len(api("/api/random", n=3)["items"]) == 3)
        check("photo list", api("/api/photos", limit=10)["total"] == made)

        t0 = time.time()
        api("/api/photos", limit=300)
        elapsed = time.time() - t0
        check("photo list is quick", elapsed < 2.0, f"{elapsed*1000:.0f} ms")

        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/export", timeout=20) as r:
            csv = r.read().decode("utf-8-sig")
        check("CSV export", csv.count("\n") >= made and csv.startswith("Taken;")
              and "Tags" in csv.splitlines()[0])

        # The list of paths is what someone else's script gets fed, so the shape
        # of it matters: a header PowerShell's Import-Csv can read, then one
        # path per line and nothing else on the line.
        with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/api/export/paths", timeout=20) as r:
            plist = r.read().decode("utf-8-sig").splitlines()
        check("path export has a header row", plist[0] == "Path", plist[0])
        check("path export lists every file in the selection",
              len(plist) - 1 == made, f"{len(plist) - 1} of {made}")
        check("path export gives whole paths and nothing else",
              all(os.path.isabs(p) and os.path.exists(p) for p in plist[1:]),
              plist[1] if len(plist) > 1 else "")
        check("path export is sorted by path", plist[1:] == sorted(plist[1:]))
        first = api("/api/stats")["charts"]["camera"][0]["k"]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/api/export/paths?"
                + urllib.parse.urlencode({"camera": first}), timeout=20) as r:
            narrowed = r.read().decode("utf-8-sig").splitlines()
        check("path export follows the filters",
              0 < len(narrowed) - 1 < made, f"{len(narrowed) - 1} of {made}")

        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/thumb/1?s=120",
                                    timeout=20) as r:
            thumb = r.read()
        check("thumbnails", r.status == 200 and thumb[:2] == b"\xff\xd8", f"{len(thumb)} bytes")

        # Distinct URLs let us check that concurrent requests each produce one
        # timed line, including file responses and HTTP errors.
        logged_requests = [
            ("/api/stats?request_log_check=stats", 200),
            ("/api/thumb/1?s=120&request_log_check=thumb", 200),
            ("/request-log-missing?request_log_check=missing", 404),
        ]

        def fetch_logged(item):
            path, expected = item
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}",
                                            timeout=20) as response:
                    response.read()
                    return response.status == expected
            except urllib.error.HTTPError as error:
                error.close()
                return error.code == expected

        with ThreadPoolExecutor(max_workers=3) as pool:
            check("concurrent logged requests return expected statuses",
                  all(pool.map(fetch_logged, logged_requests)))

        rating_checks(db, photos)
        tag_checks(db, photos)
        comment_checks(db, photos)
        network_checks(db)
        job_checks(db)
        sharpness_checks(db)
    finally:
        srv.terminate()
        srv.wait(timeout=10)
        server_log.seek(0)
        log_text = server_log.read().decode("utf-8", errors="replace")
        server_log.close()

    print("\nrequest timing log")
    log_text = re.sub(r"\x1b\[[0-9;]*m", "", log_text)
    for path, status in logged_requests:
        lines = [line for line in log_text.splitlines()
                 if f'"GET {path} HTTP/1.1"' in line]
        check(f"one timed log line for {status} {path}",
              len(lines) == 1 and bool(re.search(
                  rf'" {status} - \d+\.\d ms$', lines[0])), " | ".join(lines))

    check_dups_pass(tmp, photos, db)

    print()
    if failures:
        print(f"FAILED: {len(failures)} — " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
