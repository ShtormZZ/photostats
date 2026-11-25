# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
# SPDX-License-Identifier: AGPL-3.0-only
"""RAW/JPEG visibility, scanner lifecycle and API consistency on temporary data."""

import argparse
import csv
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import db_schema
import raw_pairs
import scan

TAKEN = "2020:01:02 12:34:56"
DAY = "2020-01-02"


class RawPairTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="photostats-raw-pairs-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "photos.db"
        self.con = db_schema.open_database(self.path, maintenance=False)
        self.addCleanup(self.con.close)
        change = patch.object(app, "DB_PATH", str(self.path))
        change.start()
        self.addCleanup(change.stop)
        self.client = app.app.test_client()

    def insert(self, name, folder="jpg", taken=TAKEN, **extra):
        path = str(self.root / folder / name)
        metadata = {"Make": "Fixture", "Model": folder, "DateTimeOriginal": taken,
                    "GPSLatitude": 55, "GPSLongitude": 37}
        row = scan.make_row(path, SimpleNamespace(st_size=100000, st_mtime=1577968496), metadata)
        row.update(extra)
        with self.con:
            columns = list(row)
            cursor = self.con.execute(f"INSERT INTO photos({','.join(columns)}) "
                                      f"VALUES ({','.join('?' for _ in columns)})", list(row.values()))
        return cursor.lastrowid

    def get(self, endpoint, **params):
        response = self.client.get(endpoint, query_string=params)
        self.assertEqual(response.status_code, 200, response.data[:400])
        return response.get_json()

    def visible(self):
        return dict(self.con.execute("SELECT id, has_raw FROM visible_photos"))

    def downgrade(self, version):
        with self.con:
            self.con.execute("DROP VIEW visible_photos")
            self.con.execute("DROP INDEX ix_raw_pair")
            for name, in self.con.execute("SELECT name FROM sqlite_schema WHERE type='index' "
                                           "AND sql LIKE '%WHERE raw_hidden%'").fetchall():
                self.con.execute(f"DROP INDEX {name}")
            for event in ("insert", "update", "delete"):
                self.con.execute(f"DROP TRIGGER raw_pairs_{event}")
            self.con.execute("DROP TRIGGER revision_photos_update")
            for name in ("pair_stem", "has_raw", "raw_hidden"):
                self.con.execute(f"ALTER TABLE photos DROP COLUMN {name}")
            if version >= 3:
                self.con.execute("CREATE TRIGGER revision_photos_update AFTER UPDATE ON photos "
                                 "BEGIN UPDATE db_state SET revision=revision+1 WHERE id=1; END")
            else:
                triggers = self.con.execute("SELECT name FROM sqlite_schema WHERE type='trigger' "
                                             "AND name LIKE 'revision_%'").fetchall()
                for name, in triggers:
                    self.con.execute(f"DROP TRIGGER {name}")
                self.con.execute("DROP TABLE db_state")
            self.con.execute("DELETE FROM schema_migrations WHERE version>?", (version,))
            self.con.execute(f"PRAGMA user_version={version}")

    def pair(self):
        jpeg = self.insert("Фото.001.JPG", mark="great", comment="JPEG note")
        raw = self.insert("фото.001.NEF", folder="raw", mark="bad", comment="RAW note")
        return jpeg, raw

    def test_global_unicode_pairing_with_multiple_jpegs_and_raws(self):
        jpeg, raw = self.pair()
        copy = self.insert("ФОТО.001.jpeg", folder="copies")
        dng = self.insert("Фото.001.dng", folder="dng")
        composed = self.insert("Й.jpg")
        decomposed = self.insert("и\u0306.cr3", folder="raw")
        self.assertEqual(self.visible(), {jpeg: 1, copy: 1, composed: 1})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 6)
        self.assertEqual(self.con.execute("SELECT mark, comment FROM photos WHERE id=?", (raw,)).fetchone(),
                         ("bad", "RAW note"))
        self.assertNotIn(dng, self.visible())
        self.assertNotIn(decomposed, self.visible())

    def test_only_matching_names_and_exif_seconds_pair(self):
        jpeg = self.insert("frame.jpg")
        later = self.insert("frame.nef", taken="2020:01:02 12:34:57", folder="later")
        renamed = self.insert("another.nef")
        no_time = self.insert("frame.arw", taken=None)
        file_jpeg = self.insert("file.jpg", taken=None)
        file_raw = self.insert("file.cr2", taken=None)
        non_jpeg = self.insert("frame.png")
        standalone = self.insert("alone.dng")
        self.assertEqual(self.visible(), dict.fromkeys(
            (jpeg, later, renamed, no_time, file_jpeg, file_raw, non_jpeg, standalone), 0))
        paired = self.insert("frame.cr3")
        self.assertEqual(self.visible()[jpeg], 1)
        self.assertEqual(self.visible()[non_jpeg], 0)
        self.assertNotIn(paired, self.visible())

    def test_selection_statistics_map_folders_tags_and_exports_share_visibility(self):
        jpeg, raw = self.pair()
        solo = self.insert("solo.nef", folder="solo")
        with self.con:
            self.con.executemany("INSERT INTO photo_tags VALUES (?,?)", [(jpeg, "visible"), (raw, "hidden")])
        photos = self.get("/api/photos", limit=1)
        second = self.get("/api/photos", limit=1, offset=1)
        self.assertEqual(photos["total"], 2)
        self.assertEqual({photos["items"][0]["id"], second["items"][0]["id"]}, {jpeg, solo})
        self.assertEqual(self.get("/api/photos", q="Фото.001")["items"][0]["has_raw"], 1)
        self.assertEqual(self.get("/api/photos", folder=str(self.root / "raw"))["total"], 0)
        self.assertEqual(self.get("/api/photos", tag="hidden")["total"], 0)
        self.assertEqual(self.get("/api/photos", ext="nef")["items"][0]["id"], solo)
        random = self.get("/api/random", n=30)["items"]
        self.assertEqual({row["id"] for row in random}, {jpeg, solo})
        self.assertEqual(next(row for row in random if row["id"] == jpeg)["has_raw"], 1)
        stats = self.get("/api/stats")
        self.assertEqual((stats["totalAll"], stats["total"], stats["bytes"]), (2, 2, 200000))
        self.assertEqual(stats["charts"]["tags"], [{"k": "visible", "l": "visible", "v": 1}])
        self.assertEqual(self.get("/api/tags")["items"], [{"tag": "visible", "n": 1}])
        self.assertEqual(sum(x["v"] for x in stats["charts"]["ext"]), 2)
        geo = self.get("/api/geo")
        self.assertEqual((geo["allPhotos"], geo["withGeo"], geo["total"]), (2, 2, 2))
        self.assertEqual(self.get("/api/directories")["total"]["n"], 2)
        folders = self.get("/api/folders")["items"]
        self.assertEqual(sum(row["n"] for row in folders), 2)
        self.assertNotIn(str(self.root / "raw"), {row["folder"] for row in folders})
        for endpoint, separator in (("/api/export", ";"), ("/api/export/paths", ",")):
            response = self.client.get(endpoint)
            rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig")), delimiter=separator))
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["Path"] for row in rows}, {self.con.execute(
                "SELECT path FROM photos WHERE id=?", (pid,)).fetchone()[0] for pid in (jpeg, solo)})

    def test_companion_paths_are_available_in_all_full_cards_only(self):
        jpeg, raw = self.pair()
        dng = self.insert("Фото.001.dng", folder="Other RAW & photos")
        plain = self.insert("no-raw.jpg")
        solo = self.insert("solo.nef")
        self.insert("Фото.001.cr3", taken="2020:01:02 12:34:57")
        expected = {pid: path for pid, path in self.con.execute("SELECT id,path FROM photos") if pid in (raw, dng)}
        query = {"day": DAY, "rating_mode": "all", "unrated": 1}
        responses = [self.get("/api/photos"), self.get("/api/random", n=30),
                     self.get("/api/timeline/day", **query, photo=jpeg),
                     self.get("/api/timeline/photos", **query, details=1)["days"][0]]
        for response in responses:
            with self.subTest(items=[item["id"] for item in response["items"]]):
                item = next(row for row in response["items"] if row["id"] == jpeg)
                self.assertEqual({file["id"]: file["path"] for file in item["raw_files"]}, expected)
                for row in response["items"]:
                    if row["id"] in (plain, solo):
                        self.assertEqual(row["raw_files"], [])
        for response in (self.get("/api/timeline/day", **query),
                         self.get("/api/timeline/photos", **query)["days"][0]):
            self.assertTrue(all("raw_files" not in row for row in response["items"]))
        with self.con:
            self.con.execute("DELETE FROM photos WHERE id=?", (dng,))
        self.assertEqual(self.get("/api/timeline/day", **query, photo=jpeg)["items"][0]["raw_files"],
                         [{"id": raw, "path": expected[raw]}])

    def test_reveal_raw_selects_the_requested_companion_in_the_file_manager(self):
        jpeg, raw = self.pair()
        dng = self.insert("Фото.001.dng", folder="Other RAW & photos")
        for raw_id in (raw, dng):
            path = self.con.execute("SELECT path FROM photos WHERE id=?", (raw_id,)).fetchone()[0]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"fixture RAW")
            path = os.path.normpath(path)
            for platform, command in (("win32", ["explorer", "/select,", path]),
                                      ("darwin", ["open", "-R", path]),
                                      ("linux", ["xdg-open", os.path.dirname(path)])):
                with self.subTest(raw_id=raw_id, platform=platform), \
                     patch.object(app.sys, "platform", platform), patch.object(app.subprocess, "Popen") as launch:
                    response = self.client.post(f"/api/reveal-raw/{jpeg}/{raw_id}")
                    self.assertEqual(response.status_code, 200, response.data)
                    self.assertEqual(response.get_json(), {"ok": True})
                    launch.assert_called_once_with(command)

    def test_reveal_raw_requires_a_current_pair_a_file_and_local_access(self):
        jpeg, raw = self.pair()
        unrelated = self.insert("another.nef")
        path = self.con.execute("SELECT path FROM photos WHERE id=?", (raw,)).fetchone()[0]
        with patch.object(app.subprocess, "Popen") as launch:
            # Indexed metadata can outlive a file; no process should start for it.
            self.assertEqual(self.client.post(f"/api/reveal-raw/{jpeg}/{raw}").status_code, 404)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"fixture RAW")
            for source, target in ((jpeg, unrelated), (raw, jpeg), (jpeg, jpeg), (99999, raw)):
                self.assertEqual(self.client.post(f"/api/reveal-raw/{source}/{target}").status_code, 404)
            for loopback_only, addr in ((True, "192.168.1.7"), (False, "127.0.0.1"), (False, "::1")):
                with patch.object(app, "LOOPBACK_ONLY", loopback_only):
                    response = self.client.post(f"/api/reveal-raw/{jpeg}/{raw}", environ_base={"REMOTE_ADDR": addr})
                    self.assertEqual(response.status_code, 403)
            with self.con:
                self.con.execute("UPDATE photos SET taken='2020-01-02 12:34:57' WHERE id=?", (raw,))
            self.assertEqual(self.client.post(f"/api/reveal-raw/{jpeg}/{raw}").status_code, 404)
            launch.assert_not_called()

    def test_timeline_paging_options_cards_and_bulk_tags_ignore_paired_raw(self):
        jpeg, raw = self.pair()
        query = {"day": DAY, "rating_mode": "all", "unrated": 1}
        for i in range(19):
            self.insert(f"extra-{i}.jpg")
            self.insert(f"extra-{i}.arw", folder="raw")
        summary = self.get("/api/timeline/photos", **query)["days"][0]
        self.assertEqual((summary["total"], summary["archive_total"], len(summary["items"])), (20, 20, 15))
        self.assertTrue(all(row["has_raw"] for row in summary["items"]))
        self.assertFalse(any("path" in row for row in summary["items"]))
        ids, offset = [], 0
        while True:
            page = self.get("/api/timeline/day", **query, limit=7, offset=offset)
            ids.extend(row["id"] for row in page["items"])
            if page["next"] is None:
                break
            offset = page["next"]
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), 20)
        self.assertNotIn(raw, ids)
        card = self.get("/api/timeline/day", **query, photo=jpeg)["items"][0]
        self.assertEqual((card["has_raw"], card["mark"], card["comment"]), (1, "great", "JPEG note"))
        self.assertEqual(self.client.get("/api/timeline/day", query_string={**query, "photo": raw}).status_code, 404)
        options = self.get("/api/timeline/options", field="camera", rating_mode="all", unrated=1)
        self.assertNotIn("Fixture raw", {row["k"] for row in options["items"]})
        preview = self.get("/api/timeline/day/tags", **query, add_tag="newtag")
        self.assertEqual((preview["total"], preview["matched"], preview["hidden"], preview["additions"]), (20, 20, 0, 20))
        response = self.client.post("/api/timeline/day/tags", query_string=query,
                                    data={"add_tag": "newtag", "revision": preview["revision"]})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM photo_tags WHERE tag='newtag'").fetchone()[0], 20)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM photo_tags WHERE photo_id=?", (raw,)).fetchone()[0], 0)

    def test_hidden_raw_is_not_editable_and_duplicate_groups_only_count_visible_files(self):
        jpeg, raw = self.pair()
        self.insert("фото.001.NEF", folder="raw-copy")
        self.assertEqual(self.get("/api/stats")["dupGroups"], 0)
        self.assertEqual(self.get("/api/photos")["items"][0]["dup_n"], 1)
        self.assertEqual(self.get("/api/photos", dup="has duplicates")["total"], 0)
        for endpoint, params in ((f"/api/mark/{raw}", {"value": "great"}),
                                 (f"/api/tags/{raw}", {"tag": "newtag"})):
            self.assertEqual(self.client.post(endpoint, query_string=params).status_code, 404)
        self.assertEqual(self.client.post(f"/api/comment/{raw}", data={"text": "Changed"}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/thumb/{raw}").status_code, 404)
        self.assertEqual(self.con.execute("SELECT mark, comment FROM photos WHERE id=?", (raw,)).fetchone(), ("bad", "RAW note"))

    def test_unsigned_paired_raw_does_not_change_visible_duplicate_rule(self):
        jpeg, raw = self.pair()
        with self.con:
            self.con.execute("UPDATE photos SET sig='same-content' WHERE id=?", (jpeg,))
        copy = self.insert("renamed.jpg", folder="copies", sig="same-content")
        stats = self.get("/api/stats")
        self.assertEqual(stats["dupRule"], "content signature")
        self.assertEqual(stats["dupGroups"], 1)
        self.assertEqual({row["id"] for row in self.get("/api/photos", dup="has duplicates")["items"]}, {jpeg, copy})

    def test_analysis_only_visits_visible_photos(self):
        jpeg, raw = self.pair()
        solo = self.insert("alone.nef")
        with patch.object(scan, "ProcessPoolExecutor") as executor, \
             patch.object(scan, "sharp_ready", return_value=False), redirect_stdout(io.StringIO()):
            pool = executor.return_value
            pool.map.side_effect = lambda fn, tasks, **kw: ((pid, scan.FAILED, None) for pid, *_ in tasks)
            scan.analyze_colors(self.con, None, 1, job=SimpleNamespace(total=0, done=0))
        tasks = pool.map.call_args.args[1]
        self.assertEqual({task[0] for task in tasks}, {jpeg, solo})
        self.assertIsNone(self.con.execute("SELECT color FROM photos WHERE id=?", (raw,)).fetchone()[0])

    def test_links_change_immediately_with_catalogue_updates(self):
        jpeg, raw = self.pair()
        with self.con:
            self.con.execute("DELETE FROM photos WHERE id=?", (raw,))
        self.assertEqual(self.visible(), {jpeg: 0})
        raw = self.insert("фото.001.nef", folder="raw")
        self.assertEqual(self.visible(), {jpeg: 1})
        with self.con:
            self.con.execute("DELETE FROM photos WHERE id=?", (jpeg,))
        self.assertEqual(self.visible(), {raw: 0})
        jpeg = self.insert("Фото.001.jpeg")
        with self.con:
            self.con.execute("UPDATE photos SET taken='2020-01-02 12:34:57' WHERE id=?", (raw,))
        self.assertEqual(self.visible(), {jpeg: 0, raw: 0})

    def test_scanner_adds_raw_to_existing_jpeg_and_rescans_without_duplicate_rows(self):
        photos = self.root / "files"
        photos.mkdir()
        jpeg = photos / "frame.JPG"
        raw = photos / "frame.NEF"
        jpeg.write_bytes(b"j" * 60000)
        raw.write_bytes(b"r" * 60000)
        args = argparse.Namespace(db=str(self.path), roots=[str(photos)], full=False,
                                  min_kb=50, ext=None, all_formats=False, raw=False,
                                  workers=1, hash=False, colors=False)
        metadata = {"DateTimeOriginal": TAKEN, "Make": "Fixture", "Model": "Camera"}
        def run():
            with patch.object(scan, "find_exiftool", return_value=(None, None)), \
                 patch.object(scan, "read_one_fallback", return_value=metadata), redirect_stdout(io.StringIO()):
                scan.exif_pass(args, SimpleNamespace(done=0, total=0, phase="", note=""))
        def reconcile():
            plan = scan.rescan.plan_changes(
                self.con, args.db, args, scan.active_exts(args), scan.SKIP_DIRS,
                lambda todo: (scan.make_row(path, st, metadata) for path, st in todo),
                lambda todo, full: scan.candidate_signatures(todo, full, args.workers))
            scan.rescan.apply_plan(self.con, plan, scan.COLUMNS, scan.FILE_RESULTS)
        run()
        jpg_id = self.con.execute("SELECT id FROM photos").fetchone()[0]
        self.assertEqual(self.visible(), {jpg_id: 0})
        args.raw = True
        run()
        self.assertEqual(self.visible(), {jpg_id: 1})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 2)
        run()
        args.raw = False
        run()
        self.assertEqual(self.visible(), {jpg_id: 1})
        raw.unlink()
        run()
        self.assertEqual(self.visible(), {jpg_id: 1})
        reconcile()
        rows = self.get("/api/photos")["items"]
        self.assertEqual([(row["id"], row["has_raw"]) for row in rows], [(jpg_id, 0)])
        raw_id, = self.con.execute("SELECT id FROM photos WHERE missing=1").fetchone()
        raw.write_bytes(b"r" * 60000)
        args.raw = True
        run()
        self.assertEqual(self.visible(), {jpg_id: 1})
        self.assertEqual(self.get("/api/photos")["items"][0]["raw_files"][0]["id"], raw_id)
        jpeg.unlink()
        args.raw = True
        args.full = True
        run()
        reconcile()
        rows = self.get("/api/photos")["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["ext"], rows[0]["has_raw"]), ("nef", 0))

    def test_upgrade_excludes_already_missing_photos_without_losing_records(self):
        jpeg, raw = self.pair()
        with self.con:
            self.con.execute("UPDATE photos SET missing=1 WHERE id=?", (jpeg,))
            self.con.execute("DROP VIEW visible_photos")
            self.con.execute("CREATE VIEW visible_photos AS SELECT * FROM photos WHERE raw_hidden=0")
            self.con.execute("DROP INDEX ix_taken")
            self.con.execute("CREATE INDEX ix_taken ON photos(taken) WHERE raw_hidden=0")
            self.con.execute("DELETE FROM schema_migrations WHERE version=7")
            self.con.execute("PRAGMA user_version=6")
        self.assertEqual(self.visible(), {jpeg: 0, raw: 0})
        db_schema.ensure_schema(self.con)
        self.assertEqual(self.visible(), {raw: 0})
        self.assertEqual(db_schema._index_changes(self.con), ([], False))
        self.assertEqual(self.con.execute("SELECT missing, comment FROM photos WHERE id=?", (jpeg,)).fetchone(),
                         (1, "JPEG note"))

    def test_upgrade_backfills_pairs_without_rereading_files_or_losing_annotations(self):
        jpeg, raw = self.pair()
        with self.con:
            self.con.execute("INSERT INTO photo_tags VALUES (?,'keep')", (jpeg,))
        self.downgrade(4)
        revision = db_schema.diagnostics(self.con)["revision"]
        db_schema.ensure_schema(self.con)
        self.assertEqual(self.visible(), {jpeg: 1})
        self.assertGreater(db_schema.diagnostics(self.con)["revision"], revision)
        self.assertEqual(self.con.execute("SELECT mark, comment FROM photos WHERE id=?", (jpeg,)).fetchone(), ("great", "JPEG note"))
        self.assertEqual(self.con.execute("SELECT tag FROM photo_tags WHERE photo_id=?", (jpeg,)).fetchone(), ("keep",))
        with closing(sqlite3.connect(db_schema._get(self.con, "schema_backup"))) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 2)

    def test_upgrades_from_versions_one_to_three_build_pairs_and_revision_triggers(self):
        jpeg, raw = self.pair()
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self.downgrade(version)
                db_schema.ensure_schema(self.con)
                self.assertEqual(self.visible(), {jpeg: 1})
                self.assertEqual(db_schema._index_changes(self.con), ([], False))
                revision = db_schema.diagnostics(self.con)["revision"]
                with self.con:
                    self.con.execute("UPDATE photos SET pair_stem='changed' WHERE id=?", (raw,))
                self.assertGreater(db_schema.diagnostics(self.con)["revision"], revision)
                self.assertEqual(self.visible(), {jpeg: 0, raw: 0})
                with self.con:
                    self.con.execute("UPDATE photos SET pair_stem='фото.001' WHERE id=?", (raw,))

    def test_pair_lookup_and_timeline_keep_their_indexes(self):
        self.pair()
        plan = [row[3] for row in self.con.execute(
            "EXPLAIN QUERY PLAN SELECT id, has_raw FROM visible_photos "
            "WHERE taken>=? AND taken<? ORDER BY taken", (DAY, "2020-01-03"))]
        self.assertTrue(any("ix_taken" in row for row in plan), plan)
        refresh = [row[3] for row in self.con.execute(
            "EXPLAIN QUERY PLAN UPDATE photos SET " + raw_pairs.pair_values_sql() +
            " WHERE pair_stem=? AND taken=?", ("фото.001", "2020-01-02 12:34:56"))]
        self.assertTrue(any("paired_jpeg" in row and "ix_raw_pair" in row for row in refresh), refresh)
        self.assertTrue(any("paired_raw" in row and "ix_raw_pair" in row for row in refresh), refresh)


if __name__ == "__main__":
    unittest.main(verbosity=2)
