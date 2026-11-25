# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
# SPDX-License-Identifier: AGPL-3.0-only

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app


class RescanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.archive = self.base / "archive"
        self.archive.mkdir()
        self.db = self.base / "archive #1.db"
        self.sequence = 0

    def file(self, path, color="red"):
        path = self.archive / path
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (12, 12), color).save(path)
        return path

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scan.py"), "--db", str(self.db),
                                 "--min-size", "0", *map(str, args)], cwd=self.base,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result.stdout

    def sql(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as con, con:
            return con.execute(sql, params).fetchall()

    def preview(self, *roots):
        self.sequence += 1
        path = self.base / f"plan{self.sequence}.json"
        self.cli("--rescan-plan", path, *(roots or [self.archive]))
        return path, json.loads(path.read_text(encoding="utf-8"))

    def annotate(self, path):
        pid, = self.sql("SELECT id FROM photos WHERE path=?", (str(path),))[0]
        self.sql("UPDATE photos SET mark='great', comment='keep this' WHERE id=?", (pid,))
        self.sql("INSERT INTO photo_tags VALUES (?, 'review')", (pid,))
        return pid

    def test_move_retains_annotations_and_new_copy_has_own_record(self):
        source = self.file("a/Move.JPG")
        original = self.file("a/Copy.JPG")
        self.cli(self.archive)
        pid = self.annotate(source)
        target = original.parent.parent / "Move.JPG"
        source.rename(target)
        copy = shutil.copy2(original, self.archive)
        before = self.sql("SELECT revision, (SELECT COUNT(*) FROM jobs) FROM db_state")
        path, plan = self.preview(self.archive, target.parent / "a")
        self.assertEqual(self.sql("SELECT revision, (SELECT COUNT(*) FROM jobs) FROM db_state"), before)
        self.assertEqual([(x["kind"], x["path"]) for x in plan["actions"]],
                         [("add", str(copy)), ("move", str(target))])
        self.cli("--apply-rescan", path)
        self.assertEqual(self.sql("SELECT id, path, folder, mark, comment FROM photos WHERE id=?", (pid,)),
                         [(pid, str(target), str(target.parent), "great", "keep this")])
        self.assertEqual(self.sql("SELECT tag FROM photo_tags WHERE photo_id=?", (pid,)), [("review",)])
        self.assertFalse(self.preview()[1]["actions"])

    def test_unsigned_conflicts_do_not_consume_or_relabel_candidates(self):
        a, b = self.file("a/twin.jpg"), self.file("b/twin.jpg")
        self.cli(self.archive)
        a_id, b_id = self.annotate(a), self.annotate(b)
        c = self.archive / "twin.jpg"
        a.rename(c)
        b.unlink()
        path, plan = self.preview()
        self.assertEqual(len(plan["conflicts"]), 1)
        self.assertEqual(set(plan["conflicts"][0]["old"]), {str(a), str(b)})
        self.assertEqual(plan["conflicts"][0]["new"], [str(c)])
        self.assertFalse(plan["actions"])
        self.cli("--apply-rescan", path)
        self.assertEqual(dict(self.sql("SELECT id, missing FROM photos")), {a_id: 0, b_id: 0})

    def test_missing_records_and_restore_are_scoped_to_accessible_folders(self):
        a = self.file("on/twin.jpg")
        b = self.file("off/twin.jpg")
        kept = self.file("on/sub/twin.jpg")
        self.cli(a.parent, b.parent)
        aid, bid = self.annotate(a), self.annotate(b)
        a.unlink()
        b.parent.rename(self.base / "offline")
        path, plan = self.preview(a.parent, b.parent)
        self.assertTrue(plan["errors"])
        self.assertEqual([x["id"] for x in plan["actions"]], [aid])
        self.cli("--apply-rescan", path)
        self.assertEqual(self.sql("SELECT id, missing FROM photos WHERE id IN (?, ?) ORDER BY id", (aid, bid)),
                         [(pid, int(pid == aid)) for pid in sorted((aid, bid))])
        with patch.object(app, "DB_PATH", str(self.db)):
            client = app.app.test_client()
            self.assertEqual(client.get("/api/photos").get_json()["total"], 2)
            active = client.get("/api/photos?dup=has+duplicates").get_json()["items"]
            self.assertEqual({item["path"] for item in active}, {str(b), str(kept)})
        shutil.copy2(kept, a)
        path, plan = self.preview(a.parent)
        self.assertEqual(len(plan["actions"]), 1)
        self.assertEqual(plan["actions"][0]["id"], aid)
        self.cli("--apply-rescan", path)
        self.assertEqual(self.sql("SELECT mark, comment, missing FROM photos WHERE id=?", (aid,)),
                         [("great", "keep this", 0)])

    def test_web_excludes_an_externally_deleted_photo_and_reinstates_it_on_return(self):
        kept = self.file("keep/frame.jpg")
        deleted = self.archive / "remove/frame.jpg"
        deleted.parent.mkdir()
        shutil.copy2(kept, deleted)
        self.cli(self.archive)
        pid = self.annotate(deleted)
        self.sql("UPDATE photos SET lat=55, lon=37, taken='2020-01-02 12:00:00', "
                 "year=2020, ym='2020-01', date_src='exif'")
        checker = app.FileCheck(str(self.db))
        with (patch.object(app, "DB_PATH", str(self.db)), patch.object(app, "FILE_CHECK", checker),
              patch.object(app, "CACHE_DIR", str(self.base / "thumbs")), app.app.test_client() as client):
            revision = client.get("/api/job").get_json()["file_revision"]
            self.assertEqual(client.get("/api/photos?dup=has+duplicates").get_json()["total"], 2)
            thumb = f"/api/thumb/{pid}?s=120"
            with client.get(thumb) as response:
                self.assertEqual(response.status_code, 200)
            deleted.unlink()
            with client.get(thumb) as response:
                self.assertEqual(response.status_code, 404)
            checker.next_check = 0
            stats = client.get("/api/stats").get_json()
            self.assertEqual((stats["totalAll"], stats["total"], stats["bytes"], stats["dupGroups"]),
                             (1, 1, kept.stat().st_size, 0))
            self.assertNotIn("availability", stats["charts"])
            self.assertNotEqual(client.get("/api/job").get_json()["file_revision"], revision)
            for endpoint in ("/api/photos", "/api/random", "/api/timeline/day?day=2020-01-02"):
                items = client.get(endpoint).get_json()["items"]
                self.assertEqual(len(items), 1, endpoint)
                self.assertNotEqual(items[0]["id"], pid, endpoint)
            day, = client.get("/api/timeline/photos?day=2020-01-02").get_json()["days"]
            self.assertEqual(day["archive_total"], 1)
            self.assertEqual(client.get("/api/geo").get_json()["withGeo"], 1)
            self.assertEqual(client.get("/api/folders").get_json()["total"], 1)
            tree = client.get("/api/directories").get_json()
            self.assertEqual(tree["total"]["n"], 1)
            self.assertNotIn(str(deleted.parent), {node["path"] for node in tree["items"]})
            self.assertEqual(client.get("/api/tags?q=review").get_json()["items"], [])
            for endpoint in ("/api/export", "/api/export/paths"):
                csv = client.get(endpoint).get_data(as_text=True)
                self.assertIn(str(kept), csv)
                self.assertNotIn(str(deleted), csv)
            self.assertEqual(client.post(f"/api/comment/{pid}", data={"text": "changed"}).status_code, 404)

            shutil.copy2(kept, deleted)
            checker.next_check = 0
            restored = next(item for item in client.get("/api/photos").get_json()["items"] if item["id"] == pid)
            self.assertEqual((restored["mark"], restored["comment"], restored["tags"]),
                             ("great", "keep this", "review"))

    def test_signed_rename_and_replacement_invalidate_the_right_results(self):
        a, b = self.file("a.jpg"), self.file("b.jpg", "green")
        self.cli(self.archive, "--hash", "--hash-full")
        aid, bid = self.annotate(a), self.annotate(b)
        self.sql("UPDATE photos SET color='red'")
        target = self.archive / "renamed.jpg"
        a.rename(target)
        b.write_bytes(b.read_bytes() + b"new bytes")
        path, plan = self.preview()
        self.assertEqual(plan["rule"], "content signature")
        self.assertEqual({(x["id"], x["kind"]) for x in plan["actions"]}, {(aid, "move"), (bid, "update")})
        self.cli("--apply-rescan", path)
        self.assertEqual(self.sql("SELECT path, color, mark FROM photos WHERE id=?", (aid,)),
                         [(str(target), "red", "great")])
        self.assertEqual(self.sql("SELECT color, mark, sig LIKE 'f1:%' FROM photos WHERE id=?", (bid,)),
                         [(None, "great", 1)])
        b.write_bytes(b.read_bytes() + b"another edit")
        self.cli(self.archive)
        self.assertEqual(self.sql("SELECT sig, mark FROM photos WHERE id=?", (bid,)), [(None, "great")])

    def test_apply_refuses_stale_preview_and_rolls_back_failed_batch(self):
        a = self.file("a.jpg")
        self.cli(self.archive)
        x = self.file("x.jpg")
        y = self.file("y.jpg")
        path, _ = self.preview()
        self.annotate(a)
        self.assertIn("catalogue changed", self.cli("--apply-rescan", path, ok=False))
        path, _ = self.preview()
        x.write_bytes(x.read_bytes() + b"changed")
        self.assertIn("File changed", self.cli("--apply-rescan", path, ok=False))
        path, _ = self.preview()
        self.sql("CREATE TRIGGER injected_failure BEFORE INSERT ON photos "
                 "WHEN NEW.filename='y.jpg' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        self.assertIn("test failure", self.cli("--apply-rescan", path, ok=False))
        self.assertEqual(self.sql("SELECT path FROM photos"), [(str(a),)])
        self.assertTrue(y.exists())

    def test_database_can_live_in_a_scanned_folder(self):
        self.db = self.archive / "catalogue.db"
        self.file("first.jpg")
        self.cli(self.archive)
        new = self.file("second.jpg")
        path, plan = self.preview()
        self.assertEqual([item["path"] for item in plan["actions"]], [str(new)])
        self.cli("--apply-rescan", path)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM photos"), [(2,)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
