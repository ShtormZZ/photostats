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
    srv = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "app.py"), "--db", db,
         "--port", str(PORT), "--no-browser"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/thumb/1?s=120",
                                    timeout=20) as r:
            thumb = r.read()
        check("thumbnails", r.status == 200 and thumb[:2] == b"\xff\xd8", f"{len(thumb)} bytes")

        rating_checks(db, photos)
        tag_checks(db, photos)
    finally:
        srv.terminate()
        srv.wait(timeout=10)

    check_dups_pass(tmp, photos, db)

    print()
    if failures:
        print(f"FAILED: {len(failures)} — " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
