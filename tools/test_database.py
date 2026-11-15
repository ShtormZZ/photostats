# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
# SPDX-License-Identifier: AGPL-3.0-only
"""Database regressions over disposable fixtures: python tools/test_database.py."""

import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import db_schema as schema
import jobs
import scan
from tools.db_benchmark import DISTRIBUTIONS, synthetic


def structure(con):
    # ALTER TABLE keeps the physical order of legacy columns; the logical
    # definitions, indexes and triggers must still match a fresh database.
    tables = [row[0] for row in con.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    columns = {table: sorted(tuple(row[1:]) for row in con.execute(f"PRAGMA table_info({table})"))
               for table in tables}
    objects = sorted((kind, name, schema._normal_sql(sql)) for kind, name, sql in con.execute(
        "SELECT type, name, sql FROM sqlite_schema WHERE type IN ('index', 'trigger') AND sql IS NOT NULL"))
    return columns, objects


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="photostats-db-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fixture.db"

    def connect(self):
        con = schema.connect(self.path)
        self.addCleanup(con.close)
        return con

    def legacy(self, era):
        con = self.connect()
        if era == "early":
            missing = {"lat", "lon", "sig", "rating", "mark", "comment", "exposure",
                       "color", "color_hex", "color_share", "color_center", "color_center_hex",
                       "color_center_share", "brightness", "contrast", "chroma", "clip_hi", "clip_lo",
                       "sharp_center", "sharp_edge", "sharp_v"}
        elif era == "partial":
            missing = {"exposure", "sharp_center", "sharp_edge", "sharp_v"}
        else:
            missing = set()
        columns = [f"{name} {kind}" + (" PRIMARY KEY" if name == "id" else
                   " UNIQUE NOT NULL" if name == "path" else "")
                   for name, kind in schema.PHOTO_COLUMNS if name not in missing]
        con.execute("CREATE TABLE photos (" + ",".join(columns) + ")")
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        con.execute("INSERT INTO photos(id,path,shutter,filename,size,taken,date_src) "
                    "VALUES (42, 'fixture/Фото.jpg', '1/125с', 'Фото.jpg', 12345, '2020-01-01 12:00:00', 'exif')")
        if "mark" not in missing:
            con.execute("UPDATE photos SET mark='great', comment='Keep me', rating=2, color='красный'")
            for stmt in schema.TAG_SQL:
                con.execute(stmt)
            con.execute("INSERT INTO photo_tags VALUES (42, 'тест')")
        con.execute("CREATE INDEX ix_dup ON photos(filename)")
        con.execute("CREATE INDEX ix_dupkey ON photos(filename)")
        con.commit()
        return con

    def test_legacy_schemas_and_entry_point_order(self):
        with closing(schema.open_database(":memory:", maintenance=False)) as fresh:
            target = structure(fresh)
        for era in ("early", "partial", "current"):
            for order in (("app", "scan", "jobs"), ("jobs", "scan", "app"), ("scan", "app", "jobs")):
                with self.subTest(era=era, order=order):
                    self.path = Path(self.tmp.name) / (era + "-" + "-".join(order) + ".db")
                    con = self.legacy(era)
                    before = tuple(con.execute("SELECT * FROM photos").fetchone())
                    for entry in order:
                        if entry == "app":
                            app.initialize_database(self.path)
                        elif entry == "scan":
                            scan.open_db(self.path).close()
                        else:
                            with jobs.Job(self.path, "exif"):
                                pass
                    self.assertEqual(structure(con), target)
                    self.assertEqual(con.execute("SELECT id, path FROM photos").fetchone(), (42, 'fixture/Фото.jpg'))
                    if era != "early":
                        self.assertEqual(con.execute("SELECT mark, comment, rating, color FROM photos").fetchone(),
                                         ("great", "Keep me", 2, "red"))
                        self.assertEqual(con.execute("SELECT tag FROM photo_tags").fetchone()[0], "тест")
                    if era != "current":
                        self.assertEqual(con.execute("SELECT exposure FROM photos").fetchone()[0], 1 / 125)
                    backup_path = schema._get(con, "schema_backup")
                    self.assertTrue(backup_path)
                    # Recover into a separate file with all writers closed on that
                    # destination; the restored file is exactly the legacy snapshot.
                    with closing(sqlite3.connect(backup_path)) as backup:
                        self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
                        with closing(sqlite3.connect(Path(self.tmp.name) / "restored.db")) as restored:
                            backup.backup(restored)
                            self.assertEqual(tuple(restored.execute("SELECT * FROM photos").fetchone()), before)
                    changes = con.total_changes
                    schema.ensure_schema(con)
                    self.assertEqual(con.total_changes, changes)

    def test_failure_rolls_back_backfill_indexes_and_version(self):
        con = self.legacy("partial")
        before = structure(con)
        def crash(connection):
            schema._indexes(connection)
            raise KeyboardInterrupt("simulated interruption after backfill")
        migrations = ((1, "tables", schema._tables), (2, "interrupt", crash))
        with patch.object(schema, "MIGRATIONS", migrations):
            with self.assertRaises(KeyboardInterrupt):
                schema.ensure_schema(con)
        self.assertEqual(structure(con), before)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 0)
        schema.ensure_schema(con)
        self.assertEqual(con.execute("SELECT exposure FROM photos").fetchone()[0], 1 / 125)

    def test_simultaneous_initialisation(self):
        con = self.legacy("partial")
        def initialise(_):
            schema.open_database(self.path).close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(initialise, range(2)))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], schema.SCHEMA_VERSION)
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(len(list(Path(str(self.path) + ".backups").glob("*.db"))), 1)

    def test_expression_change_is_rebuilt_and_analyzed(self):
        con = self.connect()
        schema.ensure_schema(con)
        con.execute("INSERT INTO photos(path) VALUES ('fixture.jpg')")
        con.commit()
        schema.maintain_statistics(con)
        with con:
            con.execute("DROP INDEX ix_rating")
            con.execute("CREATE INDEX ix_rating ON photos(rating)")
        schema.ensure_schema(con)
        self.assertEqual(schema._index_changes(con), ([], False))
        self.assertEqual(schema._get(con, "planner_pending"), "indexes")
        self.assertEqual(schema.maintain_statistics(con)["reason"], "indexes")

    def test_revision_is_transactional_and_ignores_jobs_and_maintenance(self):
        con = self.connect()
        schema.ensure_schema(con)
        revision = lambda: schema.diagnostics(con)["revision"]
        self.assertEqual(revision(), 0)
        con.execute("INSERT INTO photos(id,path) VALUES (42,'fixture.jpg')")
        self.assertGreater(revision(), 0)
        con.rollback()
        self.assertEqual(revision(), 0)
        with con:
            con.execute("INSERT INTO photos(id,path) VALUES (42,'fixture.jpg')")
        for sql in ("UPDATE photos SET mark='great'", "UPDATE photos SET comment='note'",
                    "UPDATE photos SET sig='signature'", "INSERT INTO photo_tags VALUES (42,'тег')",
                    "UPDATE photo_tags SET tag='tag'", "INSERT INTO meta VALUES ('sig_mode','full')"):
            previous = revision()
            with con:
                con.execute(sql)
            self.assertGreater(revision(), previous)
        previous = revision()
        con.execute("UPDATE photos SET mark='bad'")
        con.execute("DELETE FROM photo_tags")
        con.rollback()
        self.assertEqual(revision(), previous)
        with con:
            con.execute("UPDATE photos SET mark=mark")
        with jobs.Job(self.path, "images"):
            pass
        schema.maintain_statistics(con)
        self.assertEqual(revision(), previous)
        with con:
            con.execute("DELETE FROM photos")
        self.assertGreater(revision(), previous)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM photo_tags").fetchone()[0], 0)

    def test_statistics_bootstrap_growth_updates_and_read_only_diagnostics(self):
        con = self.connect()
        schema.ensure_schema(con)
        with con:
            con.executemany("INSERT INTO photos(path,taken) VALUES (?,?)",
                            [(f"fixture-{i}.jpg", "2020-01-01 12:00:00") for i in range(1200)])
        con.execute("PRAGMA analysis_limit=17")
        first = schema.maintain_statistics(con)
        self.assertEqual(first["result"], "ok")
        self.assertEqual(con.execute("PRAGMA analysis_limit").fetchone()[0], 17)
        self.assertIsNone(schema.maintain_statistics(con))
        with con:
            con.execute("UPDATE photos SET mark='bad'")
        self.assertEqual(schema.maintain_statistics(con)["reason"], "archive changed")
        with con:
            con.execute("DELETE FROM sqlite_stat1 WHERE tbl='photos'")
        self.assertEqual(schema.maintain_statistics(con)["reason"], "missing statistics")
        with patch.object(app, "DB_PATH", str(self.path)):
            client = app.app.test_client()
            before = schema._get(con, "planner_maintenance")
            with patch.object(schema, "maintain_statistics", side_effect=AssertionError("GET must not maintain")):
                response = client.get("/api/db/diagnostics")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["maintenance"]["result"], "ok")
            self.assertEqual(schema._get(con, "planner_maintenance"), before)

    def test_maintenance_failure_keeps_previous_statistics_and_retries(self):
        con = self.connect()
        schema.ensure_schema(con)
        with con:
            con.execute("INSERT INTO photos(path) VALUES ('fixture.jpg')")
        schema.maintain_statistics(con)
        before = schema._get(con, "planner_maintenance")
        con.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ANALYZE else sqlite3.SQLITE_OK)
        try:
            self.assertEqual(schema.maintain_statistics(con, force=True)["result"], "error")
        finally:
            # Python 3.10 cannot disable an authorizer with None (added in 3.11).
            con.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
        self.assertEqual(schema._get(con, "planner_maintenance"), before)
        self.assertIsNotNone(schema._get(con, "planner_error"))
        self.assertEqual(schema.maintain_statistics(con, force=True)["result"], "ok")
        self.assertIsNone(schema._get(con, "planner_error"))

    def test_newer_schema_is_rejected(self):
        con = self.connect()
        con.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(RuntimeError, "newer"):
            schema.ensure_schema(con)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()[0], 0)

    def test_backup_failure_prevents_migration(self):
        con = self.legacy("partial")
        before = structure(con)
        with patch.object(schema, "migration_backup", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                schema.ensure_schema(con)
        self.assertEqual(structure(con), before)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_request_connections_close_on_success_and_abort(self):
        schema.open_database(self.path).close()
        opened = []
        def connect():
            con = schema.connect(self.path)
            con.row_factory = sqlite3.Row
            opened.append(con)
            return con
        with patch.object(app, "db", connect):
            client = app.app.test_client()
            for url, status in (("/api/stats", 200), ("/api/db/diagnostics", 200),
                                ("/api/timeline/day?day=2020-01-01&photo=42", 404)):
                self.assertEqual(client.get(url).status_code, status)
        self.assertGreaterEqual(len(opened), 3)
        for con in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                con.execute("SELECT 1")

    def test_timeline_distributions_preserve_seed_and_date_cursor(self):
        for distribution in DISTRIBUTIONS:
            with self.subTest(distribution=distribution):
                self.path = Path(self.tmp.name) / (distribution + ".db")
                con = self.connect()
                synthetic(con, 3000, distribution, 42)
                with patch.object(app, "DB_PATH", str(self.path)):
                    client = app.app.test_client()
                    urls = [f"/api/timeline/photos?seed=repeatable&dir={direction}" for direction in ("asc", "desc")]
                    before = [client.get(url).json for url in urls]
                    revision = schema.diagnostics(con)["revision"]
                    schema.maintain_statistics(con)
                    self.assertEqual(before, [client.get(url).json for url in urls])
                    self.assertEqual(schema.diagnostics(con)["revision"], revision)
                    for page in before:
                        for day in page["days"]:
                            self.assertLessEqual(len(day["items"]), 15)
                            for photo in day["items"]:
                                self.assertIn(photo["mark"] or str(photo["rating"]),
                                              ("None", "great", "5", "4"))
                    if distribution == "large-day":
                        self.assertGreaterEqual(max(day["archive_total"] for page in before for day in page["days"]), 1500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
