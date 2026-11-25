# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
# SPDX-License-Identifier: AGPL-3.0-only
"""Timeline filters and paging over temporary catalogues, with no original photos."""

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import db_schema
import tags


def make_fixture(path):
    con = db_schema.open_database(path)
    records = []
    for i in range(1030):
        day = "2020-01-01" if i < 20 else "2020-01-03"
        mark = ("great", "5", "4", "3", "2", "1", "bad", None)[i % 8]
        rating = 5 if i % 11 == 0 else None
        selected = {"море"} if i % 2 == 0 else {"kids"}
        if i % 5 == 0:
            selected = {"море", "kids"}
        if i % 7 == 0:
            selected = set()
        records.append({"id": i + 1, "day": day, "mark": mark, "rating": rating, "tags": selected})
    records += [{"id": 2001, "day": "2020-01-02", "mark": "bad", "rating": 5, "tags": {"море"}},
                {"id": 2002, "day": "2020-01-04", "mark": None, "rating": None, "tags": {"kids"}},
                {"id": 2003, "day": "2020-01-05", "mark": "great", "rating": 1, "tags": {"other"}}]
    for record in records:
        number = record["id"]
        record.update(year=2020,
                      camera=None if number % 13 == 0 else f"Camera {number % 75:02d}",
                      lens=None if number % 11 == 0 else f"Lens {number % 83:02d}",
                      focal=(None, 14.9, 15, 20.99, 21, 24.99, 25, 28.99, 29, 35.9,
                             36, 50.9, 51, 71, 106, 201, 601, 700)[number % 18],
                      focal35=(None, 14.9, 15, 20.99, 21, 24.99, 25, 28.99, 29, 35.9,
                               36, 50.9, 51, 71, 106, 201, 601, 700)[(number + 5) % 18],
                      aperture=(None, 1.4, 1.5, 2, 2.1, 2.8, 3, 4, 4.3, 5.6, 6, 8,
                                8.5, 11, 12, 16, 17, 22, 24, 32)[number % 20])
        if number % 83 == 82 and record["lens"] is not None:
            record["lens"] = "Юпитер ' %_! 50 mm"
    with con:
        con.executemany("INSERT INTO photos(id,path,filename,ext,size,taken,year,month,ym,rating,mark,camera,lens,focal,focal35,fnumber) "
                        "VALUES (?,?,?,'jpg',100000,?,2020,1,'2020-01',?,?,?,?,?,?,?)",
                        [(r["id"], f"fixture/{r['id']}.jpg", f"fixture-{r['id']}.jpg",
                          r["day"] + " 12:00:00", r["rating"], r["mark"], r["camera"], r["lens"],
                          r["focal"], r["focal35"], r["aperture"]) for r in records])
        con.executemany("INSERT INTO photo_tags VALUES (?,?)",
                        [(r["id"], tag) for r in records for tag in sorted(r["tags"])])
    db_schema.maintain_statistics(con)
    con.close()
    return records


class TimelineTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="photostats-timeline-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "timeline.db"
        self.records = make_fixture(self.path)
        self.patch = patch.object(app, "DB_PATH", str(self.path))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = app.app.test_client()

    def get(self, endpoint, **params):
        response = self.client.get(endpoint, query_string=params)
        self.assertEqual(response.status_code, 200, response.data[:200])
        return response.get_json()

    def expected(self, day, mode, unrated, selected, seed):
        allowed = {"all": {"great", "5", "4", "3", "2", "1", "bad"},
                   "good": {"great", "5", "4"}, "great": {"great"}}[mode]
        ranks = {"great": 6, "5": 5, "4": 4, "3": 3, "2": 2, "1": 1, "bad": 0, None: -1}
        def effective(record):
            return record["mark"] if record["mark"] is not None else (
                str(record["rating"]) if record["rating"] is not None else None)
        matches = [r for r in self.records if r["day"] == day and
                   (effective(r) in allowed or (effective(r) is None and unrated)) and
                   (not selected or r["tags"] & set(selected))]
        return sorted(matches, key=lambda r: (-ranks[effective(r)],
                      hashlib.md5(f"{seed}:{r['id']}".encode()).hexdigest() if effective(r) is None else "",
                      r["id"]))

    def test_all_combinations_match_reference_and_page_without_duplicates(self):
        for mode in ("all", "good", "great"):
            for unrated in (0, 1):
                for selected in ([], ["kids"], ["море"], ["kids", "море"]):
                    with self.subTest(mode=mode, unrated=unrated, tags=selected):
                        query = dict(day="2020-01-03", rating_mode=mode, unrated=unrated, tag=selected, seed="fixed")
                        expected = [r["id"] for r in self.expected(query["day"], mode, unrated, selected, "fixed")]
                        preview = self.get("/api/timeline/photos", **query)["days"][0]
                        self.assertEqual([r["id"] for r in preview["items"]], expected[:15])
                        self.assertEqual(preview["total"], len(expected))
                        self.assertEqual(preview["archive_total"], 1010)
                        result, offset = [], 0
                        while True:
                            page = self.get("/api/timeline/day", **query, offset=offset, limit=137)
                            self.assertEqual(page["total"], len(expected))
                            result.extend(r["id"] for r in page["items"])
                            if page["next"] is None:
                                break
                            self.assertGreater(page["next"], offset)
                            offset = page["next"]
                        self.assertEqual(result, expected)
                        self.assertEqual(len(result), len(set(result)))
                        details = self.get("/api/timeline/photos", **query, details=1)["days"][0]["items"]
                        self.assertEqual([r["id"] for r in details], expected[:15])
                        card = self.get("/api/timeline/day", **query, photo=expected[-1])["items"][0]
                        self.assertEqual(card["id"], expected[-1])
                        self.assertIn("tags", card)

    def test_tags_match_any_and_normalise_case_and_unicode(self):
        query = dict(day="2020-01-03", rating_mode="all", unrated=1, seed="fixed", limit=500)
        combined = self.get("/api/timeline/day", **query, tag=["KIDS", "МОРЕ", "море"])
        normal = self.get("/api/timeline/day", **query, tag=["kids", "море"])
        self.assertEqual(combined, normal)
        self.assertEqual(combined["total"], len(self.expected(query["day"], "all", True, ["kids", "море"], "fixed")))
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("INSERT INTO photo_tags VALUES (2003,'й')")
        a = self.get("/api/timeline/photos", tag="Й", seed="fixed")
        b = self.get("/api/timeline/photos", tag="и\u0306", seed="fixed")
        self.assertEqual(a, b)

    def test_portfolio_candidates_keep_highlight_priority_filters_and_bounds(self):
        query = dict(day="2020-01-03", rating_mode="all", unrated=1, tag="kids", seed="portfolio")
        expected = [r["id"] for r in self.expected(query["day"], "all", True, ["kids"], "portfolio")]
        normal = self.get("/api/timeline/photos", **query)["days"][0]
        self.assertNotIn("highlight_ids", normal)
        for requested, count in ((1, 15), (37, 37), (10000, 500)):
            result = self.get("/api/timeline/photos", **query, photo_limit=requested)["days"][0]
            self.assertEqual(result["highlight_ids"], expected[:count])
            self.assertEqual([it["id"] for it in result["items"]], expected[:count])
            self.assertEqual(result["total"], len(expected))
            details = self.get("/api/timeline/photos", **query, photo_limit=requested, details=1)["days"][0]
            self.assertEqual(details["highlight_ids"], result["highlight_ids"])
        self.assertEqual(self.client.get("/api/timeline/photos", query_string={**query, "photo_limit":"bad"}).status_code, 400)

    def test_date_cursors_skip_nonmatching_days_in_both_directions(self):
        for mode, unrated, selected in (("all", 0, ["море"]), ("good", 0, ["kids"]),
                                       ("great", 1, ["kids"]), ("good", 1, ["other"])):
            expected = sorted({r["day"] for r in self.records if
                self.expected(r["day"], mode, unrated, selected, "fixed")})
            for direction in ("asc", "desc"):
                found, cursor = [], None
                for _ in range(10):
                    extra = {"cursor": cursor} if cursor else {}
                    result = self.get("/api/timeline/photos", rating_mode=mode, unrated=unrated,
                                      tag=selected, seed="fixed", limit=1, dir=direction, **extra)
                    found.extend(day["day"] for day in result["days"])
                    cursor = result["next"]
                    if cursor is None:
                        break
                self.assertEqual(found, expected if direction == "asc" else expected[::-1])

    def test_filtered_cards_cannot_escape_filters_and_analyser_gets_whole_day(self):
        query = dict(day="2020-01-02", rating_mode="great", unrated=0, tag="море", seed="fixed")
        self.assertEqual(self.get("/api/timeline/photos", **query)["days"], [])
        self.assertEqual(self.get("/api/timeline/day", **query)["total"], 0)
        self.assertEqual(self.client.get("/api/timeline/day", query_string={**query, "photo": 2001}).status_code, 404)
        self.assertEqual(self.get("/api/stats", day="2020-01-02")["total"], 1)
        self.assertEqual(self.get("/api/photos", day="2020-01-02")["items"][0]["mark"], "bad")

    def test_editing_ratings_and_tags_changes_membership(self):
        query = dict(day="2020-01-05", rating_mode="great", unrated=0, tag="other", seed="fixed")
        self.assertEqual(self.get("/api/timeline/photos", **query)["days"][0]["total"], 1)
        self.assertEqual(self.client.post("/api/mark/2003?value=bad").status_code, 200)
        self.assertEqual(self.get("/api/timeline/photos", **query)["days"], [])
        self.client.post("/api/mark/2003?value=great")
        self.client.post("/api/tags/2003?tag=kids")
        self.assertEqual(self.get("/api/timeline/photos", **query)["days"], [])
        query["tag"] = "kids"
        self.assertEqual(self.get("/api/timeline/photos", **query)["days"][0]["total"], 1)

    def test_invalid_options_and_statistics_independence(self):
        for endpoint in ("/api/timeline/photos", "/api/timeline/day"):
            for extra in ({"rating_mode": "bad"}, {"unrated": "yes"}, {"tag": "bad tag"},
                          {"tag": "' OR 1=1"}, {"tag": [f"tag{i}" for i in range(101)]},
                          {"seed": "x" * 65}):
                with self.subTest(endpoint=endpoint, extra=extra):
                    response = self.client.get(endpoint, query_string={"day": "2020-01-03", **extra})
                    self.assertEqual(response.status_code, 400)
        query = dict(day="2020-01-03", seed="fixed", rating_mode="great", unrated=0, tag="kids")
        self.assertEqual(self.get("/api/timeline/photos", **query), self.get("/api/timeline/photos", **query,
                         camera="missing", rating="1", folder="missing", q="missing"))

    def test_extra_filters_apply_to_dates_counts_highlights_pages_and_cards(self):
        selections = [("year", "2020"), ("camera", "Camera 02"), ("lens", "Lens 02"),
                      ("lens", "Юпитер ' %_! 50 mm")]
        selections += [(field, f"{lo}-{hi}") for field, buckets in
                       (("focal", app.FOCAL_BUCKETS), ("aperture", app.APERTURE_BUCKETS)) for lo, hi, _ in buckets]
        selections += [(field, app.NONE_KEY) for field in ("camera", "lens", "focal", "aperture")]
        for field, value in selections:
            with self.subTest(field=field, value=value):
                query = dict(extra_type=field, extra_value=value, rating_mode="all", unrated=1,
                             tag=["kids", "море"], seed="extra", day="2020-01-03")
                source = self.expected(query["day"], "all", True, query["tag"], "extra")
                column = "focal35" if field == "focal" else field
                if value == app.NONE_KEY:
                    expected = [r["id"] for r in source if r[column] is None]
                elif field in ("focal", "aperture"):
                    lo, hi = map(float, value.split("-"))
                    expected = [r["id"] for r in source if r[column] is not None and lo <= r[column] < hi]
                else:
                    expected = [r["id"] for r in source if str(r[field]) == value]
                result = self.get("/api/timeline/photos", **query)
                self.assertEqual(bool(result["days"]), bool(expected))
                if expected:
                    self.assertEqual(result["days"][0]["total"], len(expected))
                    self.assertEqual([r["id"] for r in result["days"][0]["items"]], expected[:15])
                    result_ids, offset = [], 0
                    while True:
                        page = self.get("/api/timeline/day", **query, offset=offset, limit=7)
                        result_ids.extend(r["id"] for r in page["items"])
                        if page["next"] is None:
                            break
                        offset = page["next"]
                    self.assertEqual(result_ids, expected)
                    card = self.get("/api/timeline/day", **query, photo=expected[-1])["items"][0]
                    self.assertEqual(card["id"], expected[-1])
                excluded = next(r["id"] for r in source if r["id"] not in expected) if len(expected) < len(source) else None
                if excluded:
                    self.assertEqual(self.client.get("/api/timeline/day", query_string={**query, "photo": excluded}).status_code, 404)
        self.assertEqual(self.get("/api/timeline/photos", extra_type="camera", extra_value="missing")["days"], [])

    def test_focal_filter_uses_equivalent_values_in_lists_cards_and_bulk_tags(self):
        # Equivalent and actual values deliberately disagree, including missing
        # metadata on either side and both half-open range boundaries.
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE photos SET focal35=NULL WHERE id <= 20")
            con.executemany("UPDATE photos SET focal=?, focal35=? WHERE id=?",
                            [(24, 36, 1), (50, 75, 2), (200, 50.999, 3), (50, None, 4),
                             (None, 40, 5), (40, 51, 6), (100, 35.999, 7)])
            con.executemany("INSERT INTO photo_tags VALUES (?,'focaltest')", [(pid,) for pid in range(1, 21)])
        scope = dict(rating_mode="all", unrated=1, tag="focaltest", seed="equivalent")
        options = self.get("/api/timeline/options", **scope, field="focal")
        self.assertEqual({item["k"]: item["n"] for item in options["items"]},
                         {"29-36": 1, "36-51": 3, "51-71": 1, "71-86": 1, app.NONE_KEY: 14})
        self.assertEqual(options, self.get("/api/timeline/options", **scope, field="focal", eq=0))
        self.assertEqual(options, self.get("/api/timeline/options", **scope, field="focal", eq=1))
        query = dict(**scope, extra_type="focal", extra_value="36-51")
        discovery = self.get("/api/timeline/photos", **query)
        self.assertEqual([day["day"] for day in discovery["days"]], ["2020-01-01"])
        day = discovery["days"][0]
        self.assertEqual(day["total"], 3)
        self.assertEqual(day["archive_total"], 20)
        self.assertEqual({item["id"] for item in day["items"]}, {1, 3, 5})
        self.assertTrue(all(36 <= item["focal35"] < 51 for item in day["items"]))
        query["day"] = day["day"]
        self.assertEqual(self.get("/api/timeline/photos", **query, eq=0),
                         self.get("/api/timeline/photos", **query, eq=1))
        result, offset = [], 0
        while True:
            page = self.get("/api/timeline/day", **query, limit=1, offset=offset)
            result.extend(item["id"] for item in page["items"])
            if page["next"] is None:
                break
            offset = page["next"]
        self.assertEqual(set(result), {1, 3, 5})
        self.assertEqual(len(result), 3)
        card = self.get("/api/timeline/day", **query, photo=5)["items"][0]
        self.assertIsNone(card["focal"])
        self.assertEqual(card["focal35"], 40)
        for excluded in (2, 4, 6, 7):
            self.assertEqual(self.client.get("/api/timeline/day", query_string={**query, "photo": excluded}).status_code, 404)
        missing = self.get("/api/timeline/day", **{**query, "extra_value": app.NONE_KEY})
        self.assertEqual(missing["total"], 14)
        self.assertIn(4, {item["id"] for item in missing["items"]})
        self.assertNotIn(5, {item["id"] for item in missing["items"]})
        for operation, field in (("add", "add_tag"), ("remove", "remove_tag")):
            endpoint = "/api/timeline/day/tags"
            preview = self.get(endpoint, **query, operation=operation, **{field: "selected"})
            self.assertEqual(preview["matched"], 3)
            self.assertEqual(preview["hidden"], 17)
            self.assertEqual(preview["changed"], 3)
            response = self.client.post(endpoint, query_string={**query, "operation": operation},
                                        data={field: "selected", "revision": preview["revision"]})
            self.assertEqual(response.status_code, 200, response.data)
            with closing(sqlite3.connect(self.path)) as con:
                actual = {pid for pid, in con.execute("SELECT photo_id FROM photo_tags WHERE tag='selected'")}
            self.assertEqual(actual, {1, 3, 5} if operation == "add" else set())

    def test_extra_option_search_paging_and_single_selection(self):
        query = dict(field="camera", rating_mode="all", unrated=1, limit=7)
        options, offset = [], 0
        while True:
            page = self.get("/api/timeline/options", **query, offset=offset)
            self.assertLessEqual(len(page["items"]), 7)
            options.extend(page["items"])
            if page["next"] is None:
                break
            offset = page["next"]
        expected = {r["camera"] if r["camera"] is not None else app.NONE_KEY for r in self.records}
        self.assertEqual({r["k"] for r in options}, expected)
        self.assertEqual(len(options), len(expected))
        selected = self.get("/api/timeline/options", **query, extra_type="camera", extra_value="Camera 02")
        self.assertEqual(selected, self.get("/api/timeline/options", **query))
        for item in options:
            count = sum(1 for record in self.records if (record["camera"] or app.NONE_KEY) == item["k"])
            self.assertEqual(item["n"], count)
        for needle in ("ЮПИТЕР", "%_!", "'"):
            items = self.get("/api/timeline/options", field="lens", rating_mode="all", q=needle)["items"]
            self.assertEqual([item["k"] for item in items], ["Юпитер ' %_! 50 mm"])
        self.assertEqual(self.get("/api/timeline/options", **query, q="does not exist")["items"], [])
        self.assertEqual(self.get("/api/timeline/options", **query, q="02")["items"][0]["k"], "Camera 02")
        # Choices follow rating and tag filters, but ignore the current extra value.
        other = self.get("/api/timeline/options", field="camera", rating_mode="great", unrated=0, tag="other")["items"]
        self.assertEqual(len(other), 1)
        self.assertEqual(other[0]["k"], self.records[-1]["camera"])
        self.assertEqual(other[0]["n"], 1)

    def test_extra_ranges_match_statistics_and_years_sort_newest_first(self):
        for field, buckets in (("focal", app.FOCAL_BUCKETS), ("aperture", app.APERTURE_BUCKETS)):
            options = self.get("/api/timeline/options", field=field, rating_mode="all")["items"]
            column = "focal35" if field == "focal" else field
            expected = [{"k": f"{lo}-{hi}", "l": label,
                         "n": sum(1 for r in self.records if r[column] is not None and lo <= r[column] < hi)}
                        for lo, hi, label in buckets]
            expected = [item for item in expected if item["n"]]
            missing = sum(1 for r in self.records if r[column] is None)
            if missing:
                expected.append({"k": app.NONE_KEY, "l": app.NONE_LABEL, "n": missing})
            self.assertEqual(options, expected)
            if field == "aperture":
                self.assertTrue(self.get("/api/timeline/options", field=field, q="f/2.2 - f/2.8")["items"])
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE photos SET year = 2019 WHERE id = 1")
            con.execute("UPDATE photos SET year = 2021 WHERE id = 2")
            con.execute("UPDATE photos SET year = NULL WHERE id = 3")
        self.assertEqual([r["k"] for r in self.get("/api/timeline/options", field="year", rating_mode="all")["items"]],
                         ["2021", "2020", "2019", app.NONE_KEY])
        page = self.get("/api/timeline/day", day="2020-01-01", rating_mode="all", extra_type="year", extra_value="2019")
        self.assertEqual([r["id"] for r in page["items"]], [1])

    def test_extra_filter_validation_and_no_unintended_statistics_filtering(self):
        invalid = [{"extra_type": "path", "extra_value": "anything"}, {"extra_value": "2020"},
                   {"extra_type": ["year", "camera"], "extra_value": "2020"},
                   {"extra_type": "camera", "extra_value": ["Camera 01", "Camera 02"]},
                   {"extra_type": "focal", "extra_value": "0-999999"},
                   {"extra_type": "aperture", "extra_value": "NaN"},
                   {"extra_type": "year", "extra_value": "2020 OR 1=1"}]
        for endpoint in ("/api/timeline/photos", "/api/timeline/day"):
            for query in invalid:
                self.assertEqual(self.client.get(endpoint, query_string={"day": "2020-01-03", **query}).status_code, 400)
        for query in ({}, {"field": "path"}, {"field": ["camera", "year"]},
                      {"field": "lens", "limit": 1000}, {"field": "lens", "offset": -1}):
            self.assertEqual(self.client.get("/api/timeline/options", query_string=query).status_code, 400)
        query = dict(day="2020-01-03", seed="fixed", extra_type="camera", extra_value="Camera 02")
        self.assertEqual(self.get("/api/timeline/photos", **query),
                         self.get("/api/timeline/photos", **query, camera="missing", year="1900", eq=1))
        self.assertEqual(self.get("/api/photos", day="2020-01-03")["total"], 1010)

    def test_thumbnail_metadata_in_highlights_and_day_pages(self):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE photos SET camera=?, lens=?, focal=50, fnumber=2.8, iso=800, width=4000, height=6000, "
                        "shutter='1/125', comment=?, color_hex='#135a7c', sharp_center=55, sharp_edge=12 WHERE id=1",
                        ("Camera <model>", "Lens '50' & 85", "Заметка <b> & \"text\""))
            con.execute("UPDATE photos SET sharp_center=54.9, sharp_edge=23 WHERE id=2")
            con.execute("UPDATE photos SET sharp_center=60 WHERE id=9")
            con.executemany("INSERT INTO photo_tags VALUES (1,?)", [("море",), ("тест",)])
        query = dict(day="2020-01-01", seed="metadata", rating_mode="all", unrated=1)
        highlights = self.get("/api/timeline/photos", **query)["days"][0]["items"]
        page = self.get("/api/timeline/day", **query, limit=500)["items"]
        for records in (highlights, page):
            record = next(record for record in records if record["id"] == 1)
            self.assertEqual((record["width"], record["height"]), (4000, 6000))
            self.assertEqual({key: record[key] for key in ("camera", "lens", "focal", "fnumber", "iso", "shutter", "tags", "comment")},
                             {"camera": "Camera <model>", "lens": "Lens '50' & 85", "focal": 50,
                              "fnumber": 2.8, "iso": 800, "shutter": "1/125", "tags": "море,тест",
                              "comment": "Заметка <b> & \"text\""})
            self.assertEqual(record["color_hex"], "#135a7c")
            self.assertEqual((record["sharp_center_l"], record["sharp_edge_l"]), ("very sharp", "soft"))
            by_id = {photo["id"]: photo for photo in records}
            self.assertEqual((by_id[2]["sharp_center_l"], by_id[2]["sharp_edge_l"]), ("sharp", "medium"))
            self.assertEqual((by_id[9]["sharp_center_l"], by_id[9]["sharp_edge_l"]), ("very sharp", None))
            self.assertTrue(any(other["color_hex"] is None for other in records))
            self.assertTrue(all(key not in record for key in
                                ("path", "folder", "lat", "lon", "sharp_center", "sharp_edge", "color", "color_center_hex")))
        self.client.post("/api/tags/1")
        self.client.post("/api/comment/1", data={"text": ""})
        cleared = next(record for record in self.get("/api/timeline/photos", **query)["days"][0]["items"] if record["id"] == 1)
        self.assertIsNone(cleared["tags"])
        self.assertFalse(cleared["comment"])

    def test_bulk_tags_include_hidden_matches_keep_existing_and_ignore_duplicates(self):
        endpoint = "/api/timeline/day/tags"
        query = dict(day="2020-01-03", rating_mode="all", unrated=1, tag=["kids", "море"])
        expected = {r["id"] for r in self.expected(query["day"], "all", True, query["tag"], "bulk")}
        preview = self.get(endpoint, **query, add_tag=["BULK", "bulk"])
        self.assertGreater(preview["matched"], 500)
        self.assertEqual(preview["matched"], len(expected))
        self.assertEqual(preview["hidden"], 1010 - len(expected))
        self.assertEqual(preview["tags"], ["bulk"])
        self.assertEqual(preview["conflicts"], 0)
        with closing(sqlite3.connect(self.path)) as con:
            before = set(con.execute("SELECT photo_id, tag FROM photo_tags"))
        response = self.client.post(endpoint, query_string={**query, "limit": 15},
                                    data={"add_tag": ["BULK", "bulk"], "revision": preview["revision"]})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json["changed"], len(expected))
        self.assertEqual(response.json["additions"], len(expected))
        with closing(sqlite3.connect(self.path)) as con:
            after = set(con.execute("SELECT photo_id, tag FROM photo_tags"))
        self.assertEqual(after - before, {(pid, "bulk") for pid in expected})
        self.assertTrue(before <= after)
        repeated = self.get(endpoint, **query, add_tag="bulk")
        self.assertEqual(repeated["changed"], 0)
        response = self.client.post(endpoint, query_string=query,
                                    data={"add_tag": "bulk", "revision": repeated["revision"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["revision"], repeated["revision"])

    def test_bulk_tags_use_rating_unrated_and_every_extra_filter(self):
        endpoint = "/api/timeline/day/tags"
        for field, value in (("year", "2020"), ("camera", "Camera 02"), ("lens", "Lens 03"),
                             ("focal", "36-51"), ("aperture", "2.1-3")):
            query = dict(day="2020-01-03", rating_mode="good", unrated=0,
                         tag="kids", extra_type=field, extra_value=value)
            # Use actual whitelist keys rather than assuming bucket boundaries.
            if field in ("focal", "aperture"):
                buckets = app.FOCAL_BUCKETS if field == "focal" else app.APERTURE_BUCKETS
                query["extra_value"] = f"{buckets[2][0]}-{buckets[2][1]}"
            expected = {r["id"] for r in self.get("/api/timeline/day", **query, limit=500)["items"]}
            preview = self.get(endpoint, **query, add_tag="bulk")
            self.assertEqual(preview["matched"], len(expected))
            self.assertEqual(preview["total"], 1010)
            response = self.client.post(endpoint, query_string=query,
                                        data={"add_tag": "bulk", "revision": preview["revision"]})
            self.assertEqual(response.status_code, 200, response.data)
            with closing(sqlite3.connect(self.path)) as con, con:
                actual = {r[0] for r in con.execute("SELECT photo_id FROM photo_tags WHERE tag='bulk'")}
                self.assertEqual(actual, expected)
                con.execute("DELETE FROM photo_tags WHERE tag='bulk'")

    def test_bulk_remove_lists_all_scoped_tags_and_freezes_filter_membership(self):
        endpoint = "/api/timeline/day/tags"
        query = dict(day="2020-01-03", operation="remove", rating_mode="all", unrated=1, tag="kids")
        targets = {r["id"] for r in self.expected(query["day"], "all", True, ["kids"], "remove")}
        self.assertGreater(len(targets), 500)
        extras = [(pid, f"extra{number}") for number, pid in enumerate(sorted(targets)[-6:])]
        excluded = next(r["id"] for r in self.records if r["day"] == query["day"] and r["id"] not in targets)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.executemany("INSERT INTO photo_tags VALUES (?,?)", extras + [(excluded, "excluded")])
            before = set(con.execute("SELECT photo_id,tag FROM photo_tags"))
            revision = con.execute("SELECT revision FROM db_state WHERE id=1").fetchone()[0]
        preview = self.get(endpoint, **query)
        expected_options = {}
        for pid, tag in before:
            if pid in targets:
                expected_options[tag] = expected_options.get(tag, 0) + 1
        self.assertEqual({item["tag"]: item["n"] for item in preview["available"]}, expected_options)
        self.assertGreater(len(preview["available"]), 3)
        self.assertNotIn("excluded", expected_options)
        self.assertNotIn("other", expected_options)
        self.assertEqual(preview["revision"], revision)  # Preview is read-only.
        self.assertEqual(preview["matched"], len(targets))
        self.assertEqual(preview["hidden"], 1010 - len(targets))
        chosen = ["KIDS", "kids", "МОРЕ", *[tag for _, tag in extras[:4]]]
        normal = {tags.normalize(tag) for tag in chosen}
        preview = self.get(endpoint, **query, remove_tag=chosen)
        expected_deleted = {(pid, tag) for pid, tag in before if pid in targets and tag in normal}
        self.assertEqual(preview["changed"], len(targets))
        self.assertEqual(preview["removals"], len(expected_deleted))
        response = self.client.post(endpoint, query_string={**query, "limit": 15},
                                    data={"remove_tag": chosen, "revision": preview["revision"]})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json["removals"], len(expected_deleted))
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(set(con.execute("SELECT photo_id,tag FROM photo_tags")), before - expected_deleted)
        # All matching kids photos disappeared only after both kids and море
        # had been removed; unrelated tags on those same photos survived.
        self.assertEqual(self.get("/api/timeline/photos", **query)["days"], [])

    def test_bulk_remove_respects_ratings_unrated_and_extra_filters(self):
        endpoint = "/api/timeline/day/tags"
        for mode, unrated in (("all", 1), ("good", 0), ("great", 1)):
            for field, value in (("year", "2020"), ("camera", "Camera 02"), ("lens", "Lens 03"),
                                 ("focal", "36-51"), ("aperture", "2.1-3.0")):
                with self.subTest(mode=mode, unrated=unrated, field=field):
                    query = dict(day="2020-01-03", operation="remove", rating_mode=mode, unrated=unrated,
                                 tag="kids", extra_type=field, extra_value=value)
                    expected = set()
                    offset = 0
                    while True:
                        page = self.get("/api/timeline/day", **query, limit=500, offset=offset)
                        expected.update(it["id"] for it in page["items"])
                        if page["next"] is None:
                            break
                        offset = page["next"]
                    with closing(sqlite3.connect(self.path)) as con:
                        before = set(con.execute("SELECT photo_id,tag FROM photo_tags"))
                    preview = self.get(endpoint, **query, remove_tag=["kids", "море"])
                    self.assertEqual(preview["matched"], len(expected))
                    self.assertEqual({item["tag"]: item["n"] for item in preview["available"]},
                                     {tag: sum(1 for pid, t in before if pid in expected and t == tag)
                                      for tag in {t for pid, t in before if pid in expected}})
                    response = self.client.post(endpoint, query_string=query,
                                                data={"remove_tag": ["kids", "море"], "revision": preview["revision"]})
                    self.assertEqual(response.status_code, 200 if expected else 409, response.data)
                    with closing(sqlite3.connect(self.path)) as con, con:
                        self.assertEqual(set(con.execute("SELECT photo_id,tag FROM photo_tags")),
                                         {(pid, tag) for pid, tag in before if pid not in expected})
                        con.executemany("INSERT OR IGNORE INTO photo_tags VALUES (?,?)", list(before))

    def test_bulk_remove_revision_rollback_empty_and_invalid_requests(self):
        endpoint = "/api/timeline/day/tags"
        query = dict(day="2020-01-01", operation="remove", rating_mode="all", unrated=1)
        preview = self.get(endpoint, **query, remove_tag="kids")
        self.client.post("/api/mark/1?value=bad")
        response = self.client.post(endpoint, query_string=query,
                                    data={"remove_tag": "kids", "revision": preview["revision"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["reason"], "revision")
        with closing(sqlite3.connect(self.path)) as con, con:
            before = set(con.execute("SELECT photo_id,tag FROM photo_tags"))
            con.execute("CREATE TRIGGER reject_remove BEFORE DELETE ON photo_tags WHEN OLD.photo_id=20 "
                        "BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        preview = self.get(endpoint, **query, remove_tag=["kids", "море"])
        with patch.dict(app.app.config, TESTING=True), self.assertRaises(sqlite3.IntegrityError):
            self.client.post(endpoint, query_string=query,
                             data={"remove_tag": ["kids", "море"], "revision": preview["revision"]})
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(set(con.execute("SELECT photo_id,tag FROM photo_tags")), before)
            self.assertEqual(con.execute("SELECT revision FROM db_state WHERE id=1").fetchone()[0], preview["revision"])
        # Removing an absent tag is an idempotent no-op, with no revision bump.
        preview = self.get(endpoint, **query, remove_tag="absent")
        response = self.client.post(endpoint, query_string=query,
                                    data={"remove_tag": "absent", "revision": preview["revision"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["changed"], 0)
        self.assertEqual(response.json["revision"], preview["revision"])
        for body in ({}, {"remove_tag": "bad tag", "revision": 0}, {"remove_tag": "kids"},
                     {"remove_tag": "kids", "revision": -1}, {"add_tag": "kids", "revision": 0}):
            self.assertEqual(self.client.post(endpoint, query_string=query, data=body).status_code, 400)
        self.assertEqual(self.client.get(endpoint, query_string={**query, "operation": "invalid"}).status_code, 400)
        self.client.post("/api/tags/2003")
        untagged = self.get(endpoint, **{**query, "day": "2020-01-05"})
        self.assertEqual(untagged["matched"], 1)
        self.assertEqual(untagged["available"], [])
        empty = self.get(endpoint, **{**query, "day": "1900-01-01"})
        self.assertEqual(empty["matched"], 0)
        self.assertEqual(empty["available"], [])

    def test_bulk_tags_capacity_revision_validation_and_transaction_rollback(self):
        endpoint = "/api/timeline/day/tags"
        query = dict(day="2020-01-01", rating_mode="all", unrated=1)
        with closing(sqlite3.connect(self.path)) as con:
            before = set(con.execute("SELECT photo_id,tag FROM photo_tags"))
        preview = self.get(endpoint, **query, add_tag=["first", "second"])
        self.assertGreater(preview["conflicts"], 0)
        response = self.client.post(endpoint, query_string=query,
                                    data={"add_tag": ["first", "second"], "revision": preview["revision"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["reason"], "capacity")
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(set(con.execute("SELECT photo_id,tag FROM photo_tags")), before)
        preview = self.get(endpoint, **query, add_tag="first")
        self.client.post("/api/mark/1?value=bad")
        response = self.client.post(endpoint, query_string=query,
                                    data={"add_tag": "first", "revision": preview["revision"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["reason"], "revision")
        # A late SQLite error rolls back earlier target inserts and revision bumps.
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("CREATE TRIGGER reject_bulk BEFORE INSERT ON photo_tags WHEN NEW.photo_id=20 "
                        "AND NEW.tag='first' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        preview = self.get(endpoint, **query, add_tag="first")
        with patch.dict(app.app.config, TESTING=True), self.assertRaises(sqlite3.IntegrityError):
            self.client.post(endpoint, query_string=query,
                             data={"add_tag": "first", "revision": preview["revision"]})
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(set(con.execute("SELECT photo_id,tag FROM photo_tags")), before)
            self.assertEqual(con.execute("SELECT revision FROM db_state WHERE id=1").fetchone()[0], preview["revision"])
        for data in ({}, {"add_tag": "bad tag", "revision": 0}, {"add_tag": "ok"},
                     {"add_tag": "ok", "revision": "invalid"},
                     {"add_tag": ["one", "two", "three", "four"], "revision": 0}):
            self.assertEqual(self.client.post(endpoint, query_string=query, data=data).status_code, 400)
        self.assertEqual(self.client.get(endpoint, query_string={"day": "invalid"}).status_code, 400)
        self.assertEqual(self.client.get(endpoint, query_string={**query, "extra_type": "path"}).status_code, 400)
        empty = dict(day="1900-01-01", rating_mode="all")
        preview = self.get(endpoint, **empty, add_tag="bulk")
        self.assertEqual(preview["matched"], 0)
        response = self.client.post(endpoint, query_string=empty,
                                    data={"add_tag": "bulk", "revision": preview["revision"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["reason"], "empty")


if __name__ == "__main__":
    unittest.main(verbosity=2)
